"""Recommendation material boundaries exercised through the real Web runtime."""
import json
import pytest
from app.recommendations import sources
from app.recommendations.contracts import RecError
from app.webhooks.repository import one, many
from tests.test_recommendations import base_env, rec_env, OWNER, configure, human_conversation, wait_runs, call, step, text
from tests.test_web_admin import FakeStreamBackend, FakeRunFactory


class ToolScript(FakeStreamBackend):
    def __init__(self, commands):
        super().__init__([step(call('Recommendation', c)) for c in commands] + [text('完成')])
        self.results = []

    async def stream(self, messages, **kwargs):
        if self.calls:
            self.results.append(json.loads(messages[-1]['content']))
        async for event in super().stream(messages, **kwargs):
            yield event


@pytest.mark.parametrize('mode,folders', [('exclude', ['folder']), ('include', [''])])
async def test_material_scope_and_visibility_apply_to_all_runtime_read_paths(rec_env, mode, folders):
    e = rec_env
    await configure(e, scopeMode=mode, scopeFolders=folders)
    private = await human_conversation(e, ['不要泄漏 SECRET_SCOPE'], folder='folder')
    allowed = await human_conversation(e, ['不要泄漏 SECRET_HIDDEN', '不要丢掉 PUBLIC_VISIBLE', '不要读取 SECRET_AUTOMATIC', '不要读取 SECRET_UNFINISHED'], folder='')
    async with e.db.write_transaction() as tx:
        await tx.execute('INSERT INTO web_hidden_operations(conversation_uuid,op_id,hidden_at_ms) VALUES(?,?,?)',
                         (allowed['conversation_uuid'], 'user_message:10', e.rec.clock()))
        await tx.execute("UPDATE web_operations SET payload_json=? WHERE conversation_uuid=? AND op_id='user_message:50'",
                         (json.dumps({'text': '不要读取 SECRET_AUTOMATIC', 'eventTriggered': True}), allowed['conversation_uuid']))
        await tx.execute("UPDATE web_operations SET lifecycle='running' WHERE conversation_uuid=? AND op_id='user_message:70'", (allowed['conversation_uuid'],))
        await tx.execute("UPDATE web_conversation_folders SET prompt_markdown='SECRET_SCOPE 私密目录约定' WHERE folder_uuid='folder'")
    commands = [
        {'action': 'internal.search', 'params': {'query': 'SECRET'}},
        {'action': 'internal.search', 'params': {'query': 'PUBLIC_VISIBLE'}},
        {'action': 'insights.get'},
        {'action': 'insights.locate', 'params': {'query': 'SECRET_SCOPE'}},
        {'action': 'source.next'},
        {'action': 'source.read', 'params': {'conversationUuid': private['conversation_uuid']}},
        {'action': 'source.read', 'params': {'conversationUuid': allowed['conversation_uuid']}},
    ]
    backend = ToolScript(commands)
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    assert (await one(e.db.conn, 'SELECT status FROM rec_runs WHERE run_id=?', (run['id'],)))['status'] == 'completed'
    assert len(backend.results) == len(commands)
    assert backend.results[0]['items'] == []
    assert len(backend.results[1]['items']) == 2
    assert [c['text'] for c in backend.results[2]['possibleCorrections']] == ['不要丢掉 PUBLIC_VISIBLE']
    assert not any(f['folderId'] == 'folder' for f in backend.results[2]['folders'])
    assert backend.results[3]['items'] == []
    assert 'PUBLIC_VISIBLE' in backend.results[4]['text']
    assert backend.results[5]['code'] == 'not_found'
    assert 'PUBLIC_VISIBLE' in backend.results[6]['text']
    assert '不要泄漏 SECRET_SCOPE' not in json.dumps(backend.results, ensure_ascii=False)
    assert '私密目录约定' not in json.dumps(backend.results, ensure_ascii=False)
    for hidden in ('SECRET_HIDDEN', 'SECRET_AUTOMATIC', 'SECRET_UNFINISHED'):
        assert hidden not in json.dumps(backend.results, ensure_ascii=False)


@pytest.mark.parametrize('kind', ['daily', 'follow'])
@pytest.mark.parametrize('shape', ['many_messages', 'one_long_message'])
async def test_runtime_source_pages_deliver_all_material_before_committing(rec_env, kind, shape):
    e = rec_env
    await configure(e)
    # Include characters that grow during JSON encoding, including control bytes.
    texts = [f'msg-{i:03d} ' + '材料"\\\n\t' * 80 for i in range(70)] if shape == 'many_messages' else ['BEGIN-' + 'x"\\\n\t' * 12000 + '-END']
    conv = await human_conversation(e, texts)
    pages = []
    class Pages(FakeStreamBackend):
        def __init__(self):
            super().__init__([step(call('Recommendation', {'action': 'source.next'}))])
        async def stream(self, messages, **kw):
            if self.calls:
                raw = messages[-1]['content']
                assert len(raw) <= 24000 and '中间内容已省略' not in raw
                result = json.loads(raw)
                if 'committed' in result:
                    command = step(call('Recommendation', {'action': 'source.next'}))
                elif result['batchId']:
                    if pages:
                        assert result['continuation']['conversationUuid'] == conv['conversation_uuid']
                        assert result['continuation']['offset'] > 0
                    pages.append(result['text'])
                    command = step(call('Recommendation', {'action': 'source.commit', 'params': {'batchId': result['batchId']}}))
                else:
                    command = text('完成')
                self.scripts.append(command)
            async for event in super().stream(messages, **kw):
                yield event
    backend = Pages()
    e.server.llm_factory = FakeRunFactory(backend, context_window=1000000)
    run = await e.rec.start_run(OWNER, kind, source_conversation=conv['conversation_uuid'] if kind == 'follow' else '')
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run['id'],))
    assert row['status'] == 'completed', row
    assert len(pages) > 1
    delivered = ''.join(pages)
    for value in texts:
        assert value in delivered
        assert '回答：' + value in delivered
    cursor = await one(e.db.conn, 'SELECT * FROM rec_cursors WHERE conversation_uuid=?', (conv['conversation_uuid'],))
    assert cursor['follow_seq' if kind == 'follow' else 'last_op_seq'] == len(texts) * 20
    assert cursor['follow_page_json' if kind == 'follow' else 'daily_page_json'] == ''


async def test_partial_page_survives_restart_and_edits_without_skipping(rec_env):
    e = rec_env
    await configure(e)
    conv = await human_conversation(e, ['x' * 40000 + 'TAIL'])
    settings, _ = await e.rec.settings_row(OWNER)
    batch = await sources.build_batch(e.db.conn, OWNER, settings, '', since_ms=0, max_chars=3000)
    assert 'TAIL' not in batch['text']
    async with e.db.write_transaction() as tx:
        await sources.commit(tx, OWNER, batch['conversations'], e.rec.clock())
    cursor = await one(e.db.conn, 'SELECT * FROM rec_cursors WHERE conversation_uuid=?', (conv['conversation_uuid'],))
    assert cursor['last_op_seq'] == 0 and cursor['daily_page_json']
    await e.db.close()
    await e.db.connect()
    second = await sources.build_batch(e.db.conn, OWNER, settings, '', since_ms=0, max_chars=3000)
    assert second['text'].startswith('x')
    async with e.db.write_transaction() as tx:
        with pytest.raises(RecError):
            await sources.commit(tx, OWNER, batch['conversations'], e.rec.clock())  # stale replay
        await tx.execute("UPDATE web_operations SET payload_json=? WHERE conversation_uuid=? AND op_id='user_message:10'",
                         (json.dumps({'text': 'EDITED START ' + 'y' * 50000}), conv['conversation_uuid']))
    edited = await sources.build_batch(e.db.conn, OWNER, settings, '', since_ms=0, max_chars=3000)
    assert 'EDITED START' in edited['text']


async def test_source_read_pages_are_valid_json_at_small_runtime_cap(rec_env):
    e = rec_env
    await configure(e)
    conv = await human_conversation(e, ['BEGIN' + '\x01"\\' * 15000 + 'TAIL'])
    from app.tools.recommendation import RecommendationRunTools
    from app.tools.base import ToolRuntimeContext
    async with e.db.write_transaction() as tx:
        await tx.execute("INSERT INTO rec_runs(run_id,owner_chat_id,kind,trigger,root_turn_uuid,local_date,started_at_ms,status) VALUES('read',?,'daily','manual','root','2026-10-11',1,'running')", (OWNER,))
    tool = RecommendationRunTools(e.server.tools)
    chunks, offset = [], 0
    while True:
        raw = await tool.dispatch('Recommendation', json.dumps({'action': 'source.read', 'params': {'conversationUuid': conv['conversation_uuid'], 'offset': offset}}),
                                  max_chars=4096, context=ToolRuntimeContext(recommendation_run_id='read'))
        assert len(raw) <= 4096
        result = json.loads(raw)
        chunks.append(result['text'])
        if result['nextOffset'] is None:
            break
        assert result['nextOffset'] > offset
        offset = result['nextOffset']
    assert 'BEGIN' + '\x01"\\' * 15000 + 'TAIL' in ''.join(chunks)


@pytest.mark.parametrize('kind', ['daily', 'follow'])
async def test_partial_pages_keep_pinned_end_and_then_deliver_concurrent_append(rec_env, kind):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followCooldownMinutes=1, followIdleMinutes=0)
    conv = await human_conversation(e, ['ORIGINAL-' + 'x' * 8000])
    settings, _ = await e.rec.settings_row(OWNER)
    now = e.rec.clock() + 1000
    async def build(at):
        if kind == 'daily':
            return await sources.build_batch(e.db.conn, OWNER, settings, '', since_ms=0, max_chars=3000)
        return await sources.build_follow_batch(e.db.conn, OWNER, settings, conv['conversation_uuid'], now_ms=at, max_chars=3000)
    async def commit(batch):
        async with e.db.write_transaction() as tx:
            await (sources.commit if kind == 'daily' else sources.commit_follow)(tx, OWNER, batch['conversations'], now)
    first = await build(now)
    await commit(first)
    # New messages arriving while a multi-page source is being read must not
    # reset its offset, or be hidden behind the final old-page revision.
    async with e.db.write_transaction() as tx:
        await tx.execute("""INSERT INTO web_operations(conversation_uuid,internal_chat_id,op_id,op_type,turn_uuid,run_root_turn_uuid,
            display_seq,status,lifecycle,internal,source,revision,payload_json,created_at_ms,updated_at_ms)
            VALUES(?,?,'late','user_message','late','late',30,'completed','terminal',0,'user',1,?,?,?)""",
            (conv['conversation_uuid'], conv['internal_chat_id'], json.dumps({'text': 'CONCURRENT_APPEND'}), now, now))
    await e.db.close()
    await e.db.connect()
    if kind == 'follow':
        # An interrupted partial read remains work even after its original 6h
        # selection window expires. The persisted page owns its original window.
        assert await sources.follow_candidates(e.db.conn, OWNER, settings, '', now_ms=now + 7*3600000)
    chunks = [first['text']]
    for _ in range(15):
        batch = await build(now)
        if not batch['conversations']:
            break
        chunks.append(batch['text'])
        await commit(batch)
    else:
        pytest.fail('pagination did not finish')
    all_text = ''.join(chunks)
    assert 'ORIGINAL-' + 'x' * 8000 in all_text
    assert 'CONCURRENT_APPEND' in all_text
    cursor = await one(e.db.conn, 'SELECT * FROM rec_cursors WHERE conversation_uuid=?', (conv['conversation_uuid'],))
    assert cursor['last_op_seq' if kind == 'daily' else 'follow_seq'] == 30
