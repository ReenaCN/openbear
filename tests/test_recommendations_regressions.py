"""Regression tests for the recommendation lifecycle and physical spend contract.
Run with temporary databases and scripted upstreams, never real services.
"""
import asyncio
import json
from types import SimpleNamespace
import pytest
from app.recommendations import sources, store, insights, follows
from app.recommendations.contracts import Settings, RecError
from app.recommendations.service import DailyBudget
from app.tools.recommendation import dispatch
from app.tools.base import ToolRuntimeContext
from app.webhooks.repository import one, many
from app.llm.events import StreamEvent, Usage
from app.llm.base import OpenBearLLMError
from tests.test_recommendations import (base_env, rec_env, OWNER, configure, human_conversation, wait_runs, card_args, call, step, text)
from tests.test_web_admin import FakeStreamBackend, FakeRunFactory, _login_cookie

async def test_user_deleted_profile_cannot_be_recreated(rec_env):
    e=rec_env; now=e.rec.clock(); p={'kind':'interest','topic':'用户明确删除的兴趣'}
    async with e.db.write_transaction() as tx:
        old,_=await store.profile_upsert(tx,OWNER,p,now)
        await store.profile_remove(tx,OWNER,old,now+1,by_user=True)
        with pytest.raises(RecError, match='该条目已被移除'):
            await store.profile_upsert(tx,OWNER,p,now+2)
    assert not await store.profile_list(e.db.conn,OWNER)
    async with e.db.write_transaction() as tx:
        restored,result=await store.profile_upsert(tx,OWNER,p,now+3,by_user=True)
    assert restored==old and result=='updated'

async def test_profile_http_patch_preserves_omitted_fields(rec_env):
    e=rec_env; now=e.rec.clock()
    async with e.db.write_transaction() as tx:
        i,_=await store.profile_upsert(tx,OWNER,{'kind':'ongoing','topic':'事项','status':'active','signal':'explicit','detail':'原说明','evidence':['原话依据'],'location':{'folderId':'folder','model':'openai/gpt'}},now)
    cookie={'openbear_web_session':await _login_cookie(e)}
    r=await e.client.patch('/api/recommendations/profile/'+i,cookies=cookie,json={'kind':'ongoing','topic':'事项','status':'done','signal':'explicit','detail':'原说明','category':'','expectedRevision':1})
    assert r.status==200
    item=(await r.json())['item']
    assert item['location']=={'folderId':'folder','model':'openai/gpt'} and item['evidence']==['原话依据']

async def test_topic_follow_schedules_daily_work(rec_env):
    e=rec_env; await configure(e,dailyTime='00:00')
    cookie={'openbear_web_session':await _login_cookie(e)}
    r=await e.client.post('/api/recommendations/follows',cookies=cookie,json={'kind':'topic','target':'测试项目后续','topic':'测试项目后续'})
    assert r.status==200
    e.server.llm_factory=FakeRunFactory(FakeStreamBackend([text('已处理关注')]),context_window=128000)
    await e.rec.tick(); await wait_runs(e)
    rows=await many(e.db.conn,'SELECT status,error FROM rec_runs')
    assert [r['status'] for r in rows]==['completed']

async def test_unfollow_page_updates_card_state(rec_env):
    e=rec_env
    async with e.db.write_transaction() as tx:
        cid=await store.card_save(tx,OWNER,{'run_id':'audit'},card_args()['params'],e.rec.clock())
    cookie={'openbear_web_session':await _login_cookie(e)}
    assert (await e.client.post(f'/api/recommendations/cards/{cid}/feedback',cookies=cookie,json={'action':'follow'})).status==200
    f=(await store.follows(e.db.conn,OWNER))[0]
    assert (await e.client.delete('/api/recommendations/follows/'+f['id'],cookies=cookie)).status==200
    assert not await store.follows(e.db.conn,OWNER)
    assert (await store.card_row(e.db.conn,OWNER,cid))['state']=='engaged'

async def test_deleting_conversation_keeps_durable_daily_spend(rec_env):
    e=rec_env; await configure(e,dailyBudgetUsd=0.0001)
    e.server.llm_factory=FakeRunFactory(FakeStreamBackend([step(call('Recommendation',{'action':'status'}))]),context_window=128000)
    r=await e.rec.start_run(OWNER,'daily'); await wait_runs(e)
    row=await one(e.db.conn,'SELECT * FROM rec_runs WHERE run_id=?',(r['id'],))
    settings,_=await e.rec.settings_row(OWNER)
    before=await e.rec.budget_left(OWNER,settings)
    assert before['exhausted'] and row['cost_usd']>0
    cookie={'openbear_web_session':await _login_cookie(e)}
    response=await e.client.delete('/api/conversations/'+row['conversation_uuid'],cookies=cookie)
    assert response.status==200,await response.text()
    after=await e.rec.budget_left(OWNER,settings)
    assert after==before
    with pytest.raises(RecError):
        await e.rec.start_run(OWNER,'daily')
    assert (await one(e.db.conn,'SELECT cost_usd FROM rec_runs WHERE run_id=?',(r['id'],)))['cost_usd']>0

async def test_follow_reuse_refreshes_removed_model(rec_env):
    e=rec_env; await configure(e,models=['openai/gpt','openai/cheap'])
    c=await human_conversation(e,['讨论内容'])
    backend=FakeStreamBackend([text('完成')]*3)
    class ResolvingFactory(FakeRunFactory):
        def backend_for(self,label):
            if not e.server.config.models.resolve(label): raise OpenBearLLMError('模型不存在: '+label)
            return super().backend_for(label)
    e.server.llm_factory=ResolvingFactory(backend,context_window=128000)
    a=await e.rec.start_run(OWNER,'follow',source_conversation=c['conversation_uuid']);await wait_runs(e)
    assert (await one(e.db.conn,'SELECT status FROM rec_runs WHERE run_id=?',(a['id'],)))['status']=='completed'
    e.server.config.models.providers['openai'].models=e.server.config.models.providers['openai'].models[1:]
    b=await e.rec.start_run(OWNER,'follow',source_conversation=c['conversation_uuid']);await wait_runs(e)
    row=await one(e.db.conn,'SELECT status,error FROM rec_runs WHERE run_id=?',(b['id'],))
    assert row['status']=='completed',row
    assert backend.calls==2
    calls=await many(e.db.conn,'SELECT model FROM model_calls ORDER BY id')
    assert [r['model'] for r in calls]==['openai/gpt','openai/cheap']

async def test_follow_reuse_refreshes_thinking(rec_env):
    e=rec_env; await configure(e,thinkingLevel='low')
    c=await human_conversation(e,['讨论'])
    b=FakeStreamBackend([text('完成')]*2);e.server.llm_factory=FakeRunFactory(b,context_window=128000)
    await e.rec.start_run(OWNER,'follow',source_conversation=c['conversation_uuid']);await wait_runs(e)
    s=await e.rec.get_settings(OWNER)
    await e.rec.save_settings(OWNER,{'settings':{**s['settings'],'thinkingLevel':'high'}})
    await e.rec.start_run(OWNER,'follow',source_conversation=c['conversation_uuid']);await wait_runs(e)
    assert [x['think_level'] for x in b.seen_opts]==['low','high']

async def test_fallback_settles_each_physical_request_at_own_price(rec_env):
    e=rec_env;await configure(e,models=['openai/gpt','openai/cheap'])
    a=FakeStreamBackend([[StreamEvent(kind='usage',usage=Usage(input_tokens=100,output_tokens=0)),StreamEvent(kind='error',error='insufficient_quota',status=402)]])
    b=FakeStreamBackend([text('完成')])
    class F(FakeRunFactory):
        def backend_for(self,label):return (a,'gpt',8192) if label=='openai/gpt' else (b,'cheap',8192)
    e.server.llm_factory=F(a,context_window=128000)
    await e.rec.start_run(OWNER,'daily');await wait_runs(e)
    calls=await many(e.db.conn,'SELECT model,status,input_tokens,output_tokens,cost_usd FROM model_calls')
    assert a.calls==b.calls==1 and len(calls)==2
    assert [(x['model'],x['status'],x['input_tokens']) for x in calls]==[('openai/gpt','error',100),('openai/cheap','ok',100)]
    assert sum(x['cost_usd'] for x in calls)==pytest.approx(.000112)
    ledger=await many(e.db.conn,'SELECT * FROM rec_usage')
    assert len(ledger)==2 and sum(x['tokens'] for x in ledger)==210
    assert sum(x['cost_usd'] for x in ledger)==pytest.approx(.000112)

async def test_daily_guard_uses_current_local_day(rec_env):
    e=rec_env;await configure(e,dailyBudgetTokens=100)
    settings,_=await e.rec.settings_row(OWNER)
    now=e.rec.clock();day=e.rec.local_date(settings,now)
    fake={'run_id':'no-run','root_turn_uuid':'root','local_date':day}
    guard=DailyBudget(e.rec,OWNER,settings,fake,asyncio.current_task())
    asked=[]
    async def spent(owner,date):
        asked.append(date)
        return {'tokens':100 if date==day else 0,'cost':0}
    e.rec.spent=spent;e.rec.clock=lambda:now+86400000
    await guard.before_call()
    assert asked==[e.rec.local_date(settings)] and e.rec.local_date(settings)!=day

async def test_http_auth_owner_and_csrf(rec_env):
    e=rec_env
    assert (await e.client.get('/api/recommendations/overview')).status==401
    cookie={'openbear_web_session':await _login_cookie(e)}
    assert (await e.client.put('/api/recommendations/settings',cookies=cookie,json={'settings':{}},headers={'Origin':'https://other.invalid'})).status==403
    async with e.db.write_transaction() as tx:
        cid=await store.card_save(tx,OWNER+1,{'run_id':'other'},card_args()['params'],e.rec.clock())
    for method,path in [('post',f'/cards/{cid}/continue'),('delete',f'/cards/{cid}'),('post',f'/cards/{cid}/feedback')]:
        r=await getattr(e.client,method)('/api/recommendations'+path,cookies=cookie,json={'action':'follow'})
        assert r.status==404
