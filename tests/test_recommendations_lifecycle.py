import asyncio,json
import pytest
from app.recommendations import sources,store
from app.recommendations.contracts import Settings
from app.tools.recommendation import dispatch
from app.tools.base import ToolRuntimeContext
from app.webhooks.repository import one,many
from tests.test_recommendations import base_env,rec_env,OWNER,configure,call,step,text,wait_runs
from tests.test_web_admin import _login_cookie,FakeRunFactory,FakeStreamBackend

async def test_concurrent_settings_save_creates_one_managed_folder(rec_env,monkeypatch):
 e=rec_env
 cookie={'openbear_web_session':await _login_cookie(e)}
 real=e.server._conversation_tree_lock
 entered=0;both=asyncio.Event();release=asyncio.Event()
 class Gate:
  async def __aenter__(self):
   nonlocal entered
   entered+=1
   if entered==2:both.set()
   await release.wait()
   return await real.__aenter__()
  async def __aexit__(self,*args):return await real.__aexit__(*args)
 monkeypatch.setattr(e.server,'_conversation_tree_lock',Gate())
 async def save():
  r=await e.client.put('/api/recommendations/settings',cookies=cookie,json={'settings':{'enabled':True,'models':['openai/gpt']}})
  assert r.status==200,await r.text()
 a,b=asyncio.create_task(save()),asyncio.create_task(save())
 try:
  await asyncio.wait_for(both.wait(),3);release.set();await asyncio.gather(a,b)
 finally:
  release.set();await asyncio.gather(a,b,return_exceptions=True)
 rows=await many(e.db.conn,"SELECT folder_uuid,name FROM web_conversation_folders WHERE name LIKE '%推荐%'")
 assert len(rows)==1

async def test_startup_preserves_results_and_interrupts_old_runs(rec_env):
 e=rec_env
 async with e.db.write_transaction() as tx:
  await tx.execute("INSERT INTO rec_runs(run_id,owner_chat_id,kind,trigger,root_turn_uuid,local_date,started_at_ms,status) VALUES('orphan',?,'daily','scheduled','old-root','2026-10-10',1,'running')",(OWNER,))
  cid=await store.card_save(tx,OWNER,{'run_id':'orphan'},{'title':'已保存的结果','body':'重启后保留'},1)
 await e.rec.start()
 row=await one(e.db.conn,"SELECT status,error FROM rec_runs WHERE run_id='orphan'")
 assert row['status']=='interrupted'
 assert (await store.card_list(e.db.conn,OWNER))['items'][0]['id']==cid
 assert not e.rec.tasks

async def test_resolve_requires_the_observed_follow_version(rec_env):
 e=rec_env;now=e.rec.clock()
 async with e.db.write_transaction() as tx:
  await tx.execute("INSERT INTO rec_runs(run_id,owner_chat_id,kind,trigger,root_turn_uuid,local_date,started_at_ms,status) VALUES('r',?,'daily','scheduled','root','2026-10-11',?,'running')",(OWNER,now))
  i=await store.follow_add(tx,OWNER,'','版本','npm','audit-fixture',now)
  await tx.execute('UPDATE rec_follows SET pending_change=? WHERE follow_id=?',('{"to":"2.0"}',i))
 old=await store.follows(e.db.conn,OWNER)
 assert json.loads(old[0]['pendingChange'])['to']=='2.0'
 # Another scripted check finishes while the model researches its old snapshot.
 async with e.db.write_transaction() as tx:
  await tx.execute('UPDATE rec_follows SET pending_change=?,last_changed_ms=? WHERE follow_id=?',('{"to":"3.0"}',now+1000,i))
 from app.recommendations.contracts import RecError
 ctx=ToolRuntimeContext(recommendation_run_id='r')
 with pytest.raises(RecError):
  await dispatch(e.rec,'follow.resolve',{'id':i,'changeToken':old[0]['changeToken'],'update':'处理过 2.0'},ctx)
 row=await one(e.db.conn,'SELECT pending_change,last_update FROM rec_follows WHERE follow_id=?',(i,))
 assert row=={'pending_change':'{"to":"3.0"}','last_update':''}
 current=(await dispatch(e.rec,'follow.list',{},ctx))['items'][0]
 assert (await dispatch(e.rec,'follow.resolve',{'id':i,'changeToken':current['changeToken'],'update':'处理过 3.0'},ctx))['resolved']
 row=await one(e.db.conn,'SELECT pending_change,last_update FROM rec_follows WHERE follow_id=?',(i,))
 assert row=={'pending_change':'','last_update':'处理过 3.0'}

async def test_reference_trigger_idempotence_and_rec_column_backfill(rec_env):
 # No production or released database touched: shrink only the fixture's rec tables
 # to the earlier development shape, reconnect through the real DB.connect.
 e=rec_env;db=e.db
 async with db.write_transaction() as tx:
  for table,names in [('rec_cursors',['daily_page_json','follow_page_json']),('rec_follows',['note','last_update','last_update_ms']),('rec_cards',['to_check_json','review','review_issue','reviewed_at_ms'])]:
   for name in names:await tx.execute(f'ALTER TABLE {table} DROP COLUMN {name}')
 await db.close();await db.connect()
 for table,expected in [('rec_cursors',{'daily_page_json','follow_page_json'}),('rec_follows',{'note','last_update','last_update_ms'}),('rec_cards',{'to_check_json','review','review_issue','reviewed_at_ms'})]:
  rows=await many(db.conn,f'PRAGMA table_info({table})')
  assert expected<={r['name'] for r in rows}
 async with db.write_transaction() as tx:
  cid=await store.card_save(tx,OWNER,{'run_id':'column-test'},{'title':'卡片','body':'内容'},1)
 rows=await many(db.conn,"SELECT * FROM web_catalog_changes WHERE kind='rec' AND entity_id=?",(cid,))
 assert len(rows)==1
 assert (await store.card_list(db.conn,OWNER))['items'][0]['toCheck']==[]
