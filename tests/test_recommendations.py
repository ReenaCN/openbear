"""Recommendation module acceptance on isolated SQLite with the real Web runtime."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.llm.events import StreamEvent, ToolCall, Usage
from app.recommendations import sources, store
from app.recommendations.contracts import RecError, Settings
from app.recommendations.fallback import FallbackBackend, FallbackChain
from app.recommendations.service import RecommendationService
from app.references import parse_references
from app.tools.recommendation import register_recommendation_tool
from app.webhooks.repository import many, one
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _login_cookie
from tests.test_webhooks_backend import env as base_env  # noqa: F401,F811

OWNER = 123


def call(name, args, cid=None):
    return StreamEvent(kind='tool_call', tool_calls=[ToolCall(cid or uuid.uuid4().hex[:8], name, json.dumps(args, ensure_ascii=False))])


def step(*events, usage=(100, 10)):
    return [StreamEvent(kind='usage', usage=Usage(input_tokens=usage[0], output_tokens=usage[1])), *events,
            StreamEvent(kind='finish', finish_reason='tool_calls' if any(e.kind == 'tool_call' for e in events) else 'stop')]


def text(value):
    return step(StreamEvent(kind='content', text=value))


@pytest.fixture
async def rec_env(base_env):  # noqa: F811
    e = base_env
    e.server.config.models.providers['openai'].models[0].cost = {'input': 1.0, 'output': 2.0}
    e.server.config.models.providers['openai'].models[1].cost = {'input': 0.1, 'output': 0.2}
    e.rec = RecommendationService(e.db, e.server)
    e.server.recommendations = e.rec
    register_recommendation_tool(e.server.tools, e.rec)
    try:
        yield e
    finally:
        await e.rec.close()


async def configure(e, **values):
    body = {'enabled': True, 'models': ['openai/gpt'], 'followEnabled': True, **values}
    return await e.rec.save_settings(OWNER, {'settings': body})


async def human_conversation(e, texts, *, folder='folder', start_ms=None):
    conv = await e.server._create_web_conversation(OWNER, folder_uuid=folder, title='讨论 RAG')
    now = start_ms or e.rec.clock()
    seq = 0
    for index, value in enumerate(texts):
        for role, body in (('user_message', value), ('assistant_message', '回答：' + value)):
            seq += 10
            await e.db.conn.execute('''INSERT INTO web_operations(conversation_uuid,internal_chat_id,op_id,op_type,turn_uuid,run_root_turn_uuid,
                display_seq,status,lifecycle,internal,source,revision,payload_json,created_at_ms,updated_at_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (conv['conversation_uuid'], conv['internal_chat_id'], f'{role}:{seq}', role,
                f't{index}', f't{index}', seq, 'completed', 'terminal', 0, 'user' if role == 'user_message' else '', 1,
                json.dumps({'text': body}, ensure_ascii=False), now + seq, now + seq))
    await e.db.conn.commit()
    return conv


async def wait_runs(e):
    while e.rec.tasks:
        await asyncio.gather(*list(e.rec.tasks.values()), return_exceptions=True)


def card_args(**overrides):
    return {'action': 'card.save', 'params': {'lane': 'external', 'category': 'adjacent', 'topic': 'RAG',
            'title': 'GraphRAG 2.0 发布', 'body': '新版支持增量索引，比你现在用的方案构建快 3 倍。', 'reason': '你 10-08 在比较 RAG 方案',
            'questions': ['它怎么做到增量的', '能接我的知识库吗'], 'sources': [{'title': 'release', 'url': 'https://example.com/r'}],
            'location': {'conversationUuid': '', 'folderId': 'folder', 'model': 'openai/cheap'}, 'material': '完整资料……',
            'dedupeKey': 'graphrag-2.0', **overrides}}


async def test_daily_cycle_reads_increment_saves_card_and_commits_cursor(rec_env):
    e = rec_env
    await configure(e)
    conv = await human_conversation(e, ['我在比较几个 RAG 方案', '想要支持增量更新的'])
    batch_holder = {}

    class Scripted(FakeStreamBackend):
        async def stream(self, messages, **kw):
            # The batch id is only known after source.next; read it from the last tool result.
            if self.calls == 2:
                last = json.loads(messages[-1]['content'])
                batch_holder['id'] = last['batchId']
                batch_holder['text'] = last['text']
                self.scripts[2] = step(call('Recommendation', {'action': 'source.commit', 'params': {'batchId': last['batchId']}}))
            async for event in super().stream(messages, **kw):
                yield event

    backend = Scripted([
        step(call('Recommendation', {'action': 'status'})),
        step(call('Recommendation', {'action': 'source.next'})),
        step(),  # replaced with source.commit
        step(call('Recommendation', {'action': 'profile.upsert', 'params': {'kind': 'interest', 'topic': 'RAG', 'signal': 'explicit',
                                                                           'detail': '在选支持增量的方案', 'lastMentioned': '2026-10-08 21:15'}})),
        step(call('Recommendation', card_args())),
        text('完成：保存 1 张卡片。'),
    ])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run['id'],))
    assert row['status'] == 'completed', row
    assert row['cards'] == 1 and row['model_calls'] == 6
    assert '我在比较几个 RAG 方案' in batch_holder['text'] and '讨论 RAG' in batch_holder['text']
    item = (await store.profile_list(e.db.conn, OWNER))[0]
    assert item['lastMentionedMs'] == e.rec.parse_when('2026-10-08 21:15', Settings())
    cursor = await one(e.db.conn, 'SELECT * FROM rec_cursors WHERE conversation_uuid=?', (conv['conversation_uuid'],))
    assert cursor['last_op_seq'] == 40 and cursor['digest']
    # The recommendation conversation itself lives in the dedicated folder, never as material.
    rec_conv = await one(e.db.conn, 'SELECT * FROM web_conversations WHERE conversation_uuid=?', (row['conversation_uuid'],))
    assert rec_conv['folder_uuid'] == await e.rec.rec_folder(OWNER)
    assert rec_conv['title'].startswith('[推荐]')
    # The folder prompt overrides the system prompt and only exposes the recommendation tools.
    assert backend.seen_tools[0] and {t['name'] for t in backend.seen_tools[0]} == {'Recommendation'}
    # Nothing new: next batch is empty and the recommendation conversation is not material.
    settings, _ = await e.rec.settings_row(OWNER)
    again = await sources.build_batch(e.db.conn, OWNER, settings, await e.rec.rec_folder(OWNER), since_ms=0)
    assert again['analyzable'] == 0


async def test_uncommitted_batch_is_read_again_and_edits_are_detected(rec_env):
    e = rec_env
    await configure(e)
    conv = await human_conversation(e, ['第一条', '第二条'])
    settings, _ = await e.rec.settings_row(OWNER)
    folder = await e.rec.ensure_folder(OWNER)
    first = await sources.build_batch(e.db.conn, OWNER, settings, folder, since_ms=0)
    assert first['analyzable'] == 1
    # Without a commit, the same material comes back.
    assert (await sources.build_batch(e.db.conn, OWNER, settings, folder, since_ms=0))['analyzable'] == 1
    async with e.db.write_transaction() as conn:
        await sources.commit(conn, OWNER, first['conversations'], e.rec.clock())
    assert (await sources.build_batch(e.db.conn, OWNER, settings, folder, since_ms=0))['analyzable'] == 0
    # Editing an analyzed message re-reads the conversation and says so.
    await e.db.conn.execute("UPDATE web_operations SET payload_json=? WHERE conversation_uuid=? AND display_seq=10",
                            (json.dumps({'text': '第一条（改过）'}, ensure_ascii=False), conv['conversation_uuid']))
    await e.db.conn.commit()
    edited = await sources.build_batch(e.db.conn, OWNER, settings, folder, since_ms=0)
    assert edited['analyzable'] == 1 and '有编辑或删除' in edited['text'] and '第一条（改过）' in edited['text']


async def test_automatic_inputs_hidden_messages_and_card_material_are_not_user_voice(rec_env):
    e = rec_env
    await configure(e)
    conv = await human_conversation(e, ['真实的话'])
    now = e.rec.clock()
    rows = [
        ('cron-op', 'user_message', 'cron', json.dumps({'text': '定时任务指令', 'source': 'cron', 'cronRunId': 'x'})),
        ('hidden-op', 'user_message', 'user', json.dumps({'text': '被隐藏的话'})),
        ('rec-op', 'user_message', 'user', json.dumps({'text': '[卡片标题](openbear://ref/rec/0123abcd-0000) 它怎么做到的',
                                                         'references': [{'kind': 'rec', 'label': '卡片标题'}]}, ensure_ascii=False)),
    ]
    for index, (op, kind, source, payload) in enumerate(rows):
        await e.db.conn.execute('''INSERT INTO web_operations(conversation_uuid,internal_chat_id,op_id,op_type,turn_uuid,run_root_turn_uuid,
            display_seq,status,lifecycle,internal,source,revision,payload_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                (conv['conversation_uuid'], conv['internal_chat_id'], op, kind, op, op, 100 + index, 'completed', 'terminal',
                                 0, source, 1, payload, now + 100 + index, now + 100 + index))
    await e.db.conn.execute('INSERT INTO web_hidden_operations(conversation_uuid,op_id,hidden_at_ms) VALUES(?,?,?)',
                            (conv['conversation_uuid'], 'hidden-op', now))
    await e.db.conn.commit()
    messages = await sources.visible_messages(e.db.conn, OWNER, conv['conversation_uuid'])
    texts = [m['text'] for m in messages if m['role'] == 'user']
    assert texts == ['真实的话', '它怎么做到的']


async def test_scope_excludes_folders(rec_env):
    e = rec_env
    await configure(e, scopeMode='exclude', scopeFolders=['folder'])
    await human_conversation(e, ['不应读取'])
    settings, _ = await e.rec.settings_row(OWNER)
    batch = await sources.build_batch(e.db.conn, OWNER, settings, await e.rec.ensure_folder(OWNER), since_ms=0)
    assert batch['analyzable'] == 0


async def test_fallback_switches_on_quota_and_accounts_each_model(rec_env):
    e = rec_env
    await configure(e, models=['openai/gpt', 'openai/cheap'])
    failing = FakeStreamBackend([[StreamEvent(kind='error', error='insufficient_quota: credit exhausted', status=402)]])
    working = FakeStreamBackend([step(call('Recommendation', {'action': 'status'})), text('完成')])

    class Factory(FakeRunFactory):
        def backend_for(self, fullname):
            return (failing, 'gpt', 8192) if fullname == 'openai/gpt' else (working, 'cheap', 8192)
    e.server.llm_factory = Factory(failing, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run['id'],))
    assert row['status'] == 'completed', row
    assert json.loads(row['models_json']) == ['openai/gpt', 'openai/cheap']
    calls = await many(e.db.conn, 'SELECT model,status,cost_usd FROM model_calls ORDER BY id')
    assert [c['model'] for c in calls if c['status'] == 'ok'] == ['openai/cheap', 'openai/cheap']
    # Cheap model price: 100 input tokens * $0.1/M + 10 output * $0.2/M.
    assert all(abs(c['cost_usd'] - (100 * 0.1 + 10 * 0.2) / 1e6) < 1e-12 for c in calls if c['status'] == 'ok')
    notices = [op for op in await e.server._web_operations(row['conversation_uuid']) if '改用 openai/cheap' in json.dumps(op, ensure_ascii=False)]
    assert notices


async def test_fallback_does_not_switch_on_request_format_errors():
    class Broken:
        protocol = 'chat'

        async def stream(self, messages, **opts):
            yield StreamEvent(kind='error', error='invalid_request_error: bad schema', status=400)
    chain = FallbackChain([{'label': 'a', 'backend': Broken(), 'model_id': 'a'}, {'label': 'b', 'backend': Broken(), 'model_id': 'b'}])
    events = [event async for event in FallbackBackend(chain).stream([], model='a')]
    assert events[-1].kind == 'error' and chain.index == 0


async def test_mixed_protocol_chain_rejected(rec_env):
    e = rec_env
    e.server.config.models.providers['anth'] = e.server.config.models.providers['openai'].model_copy(update={'protocol': 'anthropic'})
    with pytest.raises(RecError) as error:
        await configure(e, models=['openai/gpt', 'anth/gpt'])
    assert '同一协议' in error.value.payload['message']


async def test_daily_budget_stops_before_next_model_call(rec_env):
    e = rec_env
    await configure(e, dailyBudgetUsd=0.0001)
    backend = FakeStreamBackend([step(call('Recommendation', {'action': 'status'}), usage=(100, 10))] * 5)
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run['id'],))
    # One call costs $0.00012 ≥ $0.0001: the run stops before another model call.
    assert row['status'] == 'budget_exhausted', row
    assert backend.calls == 1
    with pytest.raises(RecError) as error:
        await e.rec.start_run(OWNER, 'daily')
    assert error.value.code == 'budget_exhausted'


async def test_card_dedupe_parking_and_feedback_weights(rec_env):
    e = rec_env
    await configure(e)
    run = {'run_id': 'r1', 'source_conversation_uuid': ''}
    now = e.rec.clock()
    async with e.db.write_transaction() as conn:
        first = await store.card_save(conn, OWNER, run, card_args()['params'], now)
        with pytest.raises(RecError) as duplicate:
            await store.card_save(conn, OWNER, run, card_args(title='换个说法')['params'], now)
        assert duplicate.value.code == 'duplicate'
        # Unanswered for 36h → parked, and still blocked.
        await store.park_unanswered(conn, OWNER, now + 37 * 3_600_000)
        card = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=?', (first,))
        assert card['state'] == 'parked'
        with pytest.raises(RecError):
            await store.card_save(conn, OWNER, run, card_args()['params'], now)
        # A substantive new reason may re-offer once.
        second = await store.card_save(conn, OWNER, run, card_args(newReason='发布了 2.1，修复了你遇到的问题')['params'], now)
        assert second != first
        card2 = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=?', (second,))
        await store.feedback(conn, OWNER, card2, 'continued', now)
        await store.feedback(conn, OWNER, card2, 'known', now)
    weights = await store.weights(e.db.conn, OWNER, now)
    assert weights['topic']['rag'] == 1.0 and weights['category'].get('adjacent', 0) == 0
    # Decay: two half-lives later the topic weight is a quarter.
    later = await store.weights(e.db.conn, OWNER, now + 28 * 86_400_000)
    assert later['topic']['rag'] == 0.25
    async with e.db.write_transaction() as conn:
        card2 = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=?', (second,))
        await store.feedback(conn, OWNER, card2, 'dismiss', now)
    assert (await store.weights(e.db.conn, OWNER, now))['topic']['rag'] == -0.5


async def test_user_edited_profile_wins(rec_env):
    e = rec_env
    now = e.rec.clock()
    async with e.db.write_transaction() as conn:
        item_id, _ = await store.profile_upsert(conn, OWNER, {'kind': 'ongoing', 'topic': '换 CPU 散热', 'status': 'active'}, now)
        await store.profile_upsert(conn, OWNER, {'id': item_id, 'kind': 'ongoing', 'topic': '换 CPU 散热', 'status': 'done',
                                                 'detail': '已换风扇，问题解决'}, now, by_user=True)
        _, result = await store.profile_upsert(conn, OWNER, {'kind': 'ongoing', 'topic': '换 CPU 散热', 'status': 'active'}, now + 1)
        assert result == 'kept_user_edit'
        with pytest.raises(RecError):
            await store.profile_remove(conn, OWNER, item_id, now)
    item = (await store.profile_list(e.db.conn, OWNER))[0]
    assert item['status'] == 'done' and item['userEdited']


async def test_follow_conversation_trigger_and_results_only_in_page(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=3, followIdleMinutes=0, followCooldownMinutes=30)
    conv = await human_conversation(e, ['一', '二', '三'])
    settings, _ = await e.rec.settings_row(OWNER)
    folder = await e.rec.ensure_folder(OWNER)
    found = await sources.follow_candidates(e.db.conn, OWNER, settings, folder, now_ms=e.rec.clock() + 1000)
    assert [c['conversation_uuid'] for c in found] == [conv['conversation_uuid']]
    backend = FakeStreamBackend([step(call('Recommendation', {'action': 'source.next'})),
                                 step(call('Recommendation', card_args(dedupeKey='follow-card', location={}))), text('完成')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=conv['conversation_uuid'])
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run['id'],))
    assert row['status'] == 'completed', row
    card = await one(e.db.conn, "SELECT * FROM rec_cards WHERE dedupe_key=?", (store.dedupe_key({'dedupeKey': 'follow-card'}),))
    assert json.loads(card['location_json'])['conversationUuid'] == conv['conversation_uuid']
    assert card['origin_conversation_uuid'] == conv['conversation_uuid']
    # Nothing was written into the user's conversation.
    ops = await e.server._web_operations(conv['conversation_uuid'])
    assert all('GraphRAG' not in json.dumps(op, ensure_ascii=False) for op in ops)
    # Cooldown blocks an immediate retrigger; the follow lane does not consume daily material.
    assert not await sources.follow_candidates(e.db.conn, OWNER, settings, folder, now_ms=e.rec.clock() + 1000)
    assert (await sources.build_batch(e.db.conn, OWNER, settings, folder, since_ms=0))['analyzable'] == 1


async def test_tool_refuses_outside_recommendation_runs(rec_env):
    e = rec_env
    # Not advertised to ordinary conversations, and refused if called anyway.
    assert 'Recommendation' not in e.server.tools.names(scope='main')
    result = await e.server.tools.dispatch('Recommendation', json.dumps({'action': 'status'}))
    assert 'not_recommendation_run' in result


async def test_fallback_keeps_own_native_state_and_strips_foreign():
    class Native:
        protocol = 'responses'

        def __init__(self, tag):
            self.tag, self.seen = tag, []

        async def stream(self, messages, **opts):
            self.seen.append(messages)
            yield StreamEvent(kind='native_output_item', native_output_items=[{'type': 'reasoning', 'id': self.tag}])
            yield StreamEvent(kind='content', text='ok')
    a, b = Native('a'), Native('b')
    chain = FallbackChain([{'label': 'a', 'backend': a, 'model_id': 'a'}, {'label': 'b', 'backend': b, 'model_id': 'b'}])
    adapter = FallbackBackend(chain)
    [event async for event in adapter.stream([], model='a')]
    chain.index = 1  # a switched away
    [event async for event in adapter.stream([], model='b')]
    history = [{'role': 'assistant', 'content': 'from a', 'native_output_items': [{'type': 'reasoning', 'id': 'a'}]},
               {'role': 'assistant', 'content': 'from b', 'native_output_items': [{'type': 'reasoning', 'id': 'b'}]}]
    [event async for event in adapter.stream(history, model='b')]
    sent = b.seen[-1]
    assert 'native_output_items' not in sent[0]
    assert sent[1]['native_output_items'] == [{'type': 'reasoning', 'id': 'b'}]


async def test_rec_reference_resolves_card_material_for_owner_only(rec_env):
    e = rec_env
    async with e.db.write_transaction() as conn:
        card_id = await store.card_save(conn, OWNER, {'run_id': 'r', 'source_conversation_uuid': ''}, card_args()['params'], e.rec.clock())
    text_value = f'[GraphRAG 2.0 发布](openbear://ref/rec/{card_id}) 它怎么做到增量的'
    assert parse_references(text_value)[0]['kind'] == 'rec'
    resolved = await e.server._reference_store().resolve(text_value, owner=OWNER)
    assert '完整资料……' in resolved['materials'][0]['content']
    assert '不是用户的话' in resolved['materials'][0]['content']
    from app.references import ReferenceError
    with pytest.raises(ReferenceError):
        await e.server._reference_store().resolve(text_value, owner=999)


async def test_http_feedback_continue_and_sent_flow(rec_env):
    e = rec_env
    await configure(e)
    async with e.db.write_transaction() as conn:
        card_id = await store.card_save(conn, OWNER, {'run_id': 'r', 'source_conversation_uuid': ''}, card_args()['params'], e.rec.clock())
    cookie = {'openbear_web_session': await _login_cookie(e)}
    listing = await (await e.client.get('/api/recommendations/cards', cookies=cookie)).json()
    assert listing['items'][0]['id'] == card_id and 'material' not in listing['items'][0]
    prepared = await (await e.client.post(f'/api/recommendations/cards/{card_id}/continue', cookies=cookie,
                                          json={'question': '它怎么做到增量的'})).json()
    assert prepared['folderId'] == 'folder' and prepared['model'] == 'openai/cheap'
    assert prepared['reference'] == {'kind': 'rec', 'id': card_id, 'label': 'GraphRAG 2.0 发布'}
    # The user sends from a conversation: the card counts as sent, later messages as continued.
    conv = await e.server._create_web_conversation(OWNER, folder_uuid='folder', title='续聊')
    await e.rec.observe_user_message(conv, [{'kind': 'rec', 'id': card_id}], 't1')
    await e.rec.observe_user_message(conv, [], 't2')
    actions = [r['action'] for r in await many(e.db.conn, 'SELECT action FROM rec_feedback WHERE card_id=? ORDER BY id', (card_id,))]
    assert actions == ['ask', 'sent', 'continued']
    follow = await (await e.client.post(f'/api/recommendations/cards/{card_id}/feedback', cookies=cookie, json={'action': 'follow'})).json()
    assert follow['card']['state'] in ('followed', 'continued')
    follows = await (await e.client.get('/api/recommendations/follows', cookies=cookie)).json()
    assert follows['items'][0]['kind'] == 'topic'
    settings = await (await e.client.get('/api/recommendations/settings', cookies=cookie)).json()
    assert 'chat' in settings['protocols'] and settings['derivedDailyTime']


async def test_scheduler_skips_quiet_day_without_model_call(rec_env):
    e = rec_env
    await configure(e, dailyTime='00:00')
    backend = FakeStreamBackend([text('不应调用')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    await e.rec.tick()
    await wait_runs(e)
    rows = await many(e.db.conn, 'SELECT status FROM rec_runs')
    assert [r['status'] for r in rows] == ['skipped'] and backend.calls == 0


async def test_run_start_reads_back_its_own_row_and_marks_the_trigger_message(rec_env):
    e = rec_env
    await configure(e)
    await human_conversation(e, ['我在比较 RAG 方案'])
    # Pin the shared reader to an older snapshot, as an unclosed cursor would.
    pinned = await e.db.conn.execute('SELECT run_id FROM rec_runs')
    await pinned.fetchone()
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')]), context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily', trigger='manual')
    await pinned.close()
    assert run['id'] and run['status'] == 'starting'
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run['id'],))
    assert row['status'] == 'completed' and row['conversation_uuid']
    user = await one(e.db.conn, "SELECT source,payload_json FROM web_operations WHERE conversation_uuid=? AND op_type='user_message'",
                     (row['conversation_uuid'],))
    payload = json.loads(user['payload_json'])
    assert user['source'] == 'recommendation' and payload['recommendationCard']['kind'] == 'daily'


async def test_orphaned_starting_run_is_shown_as_interrupted_and_badge_counts_new_cards(rec_env):
    e = rec_env
    await configure(e)
    async with e.db.write_transaction() as conn:
        await conn.execute('''INSERT INTO rec_runs(run_id,owner_chat_id,kind,trigger,root_turn_uuid,local_date,started_at_ms,status)
            VALUES('orphan',?,?,?,?,?,?,?)''', (OWNER, 'follow', 'conversation', 'r-orphan', '2026-10-09', e.rec.clock() - 120_000, 'starting'))
    cookie = {'openbear_web_session': await _login_cookie(e)}
    overview = await (await e.client.get('/api/recommendations/overview', cookies=cookie)).json()
    assert [r['status'] for r in overview['runs'] if r['id'] == 'orphan'] == ['interrupted']
    assert overview['unseen'] == 0
    badge = await (await e.client.get('/api/recommendations/badge', cookies=cookie)).json()
    assert badge == {'unseen': 0}


async def test_cards_stay_unread_until_explicit_read_and_rec_runs_skip_recent(rec_env):
    e = rec_env
    await configure(e)
    await human_conversation(e, ['我在比较 RAG 方案'])
    backend = FakeStreamBackend([step(call('Recommendation', card_args(dedupeKey='a'))),
                                 step(call('Recommendation', card_args(dedupeKey='b', title='另一张'))), text('完成')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily', trigger='manual')
    await wait_runs(e)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    listed = await (await e.client.get('/api/recommendations/cards', cookies=cookie)).json()
    assert len(listed['items']) == 2 and all(not c['seenAtMs'] for c in listed['items'])
    # Listing never marks read.
    assert (await (await e.client.get('/api/recommendations/badge', cookies=cookie)).json())['unseen'] == 2
    first = listed['items'][0]['id']
    one_read = await (await e.client.post('/api/recommendations/cards/seen', cookies=cookie, json={'ids': [first]})).json()
    assert one_read['unseen'] == 1
    unread = await (await e.client.get('/api/recommendations/cards?unread=1', cookies=cookie)).json()
    assert [c['id'] for c in unread['items']] != [first] and len(unread['items']) == 1
    all_read = await (await e.client.post('/api/recommendations/cards/seen', cookies=cookie, json={'all': True})).json()
    assert all_read['unseen'] == 0
    # The run's conversation lives in the recommendation folder and is not a recent conversation.
    row = await one(e.db.conn, 'SELECT conversation_uuid FROM rec_runs WHERE run_id=?', (run['id'],))
    state = await e.server._tree_running_state(OWNER)
    assert row['conversation_uuid'] not in {i['conversationUuid'] for i in state['recentItems'] + state['activityItems']}


async def test_profile_evidence_string_is_not_split_into_characters_and_follow_targets_the_matter(rec_env):
    e = rec_env
    async with e.db.write_transaction() as conn:
        item_id, _ = await store.profile_upsert(conn, OWNER, {'kind': 'interest', 'topic': 'RAG', 'evidence': '10-06 用户说要比较 RAG 方案'}, 1)
        broken, _ = await store.profile_upsert(conn, OWNER, {'kind': 'interest', 'topic': '旧数据', 'evidence': []}, 1)
        await conn.execute('UPDATE rec_profile SET evidence_json=? WHERE item_id=?', (json.dumps(list('10-06~08')), broken))
    items = {i['topic']: i for i in await store.profile_list(e.db.conn, OWNER)}
    assert items['RAG']['evidence'] == ['10-06 用户说要比较 RAG 方案']
    assert items['旧数据']['evidence'] == []
    # Following a card follows the matter itself; a repo signal attaches as an extra check.
    await configure(e)
    await human_conversation(e, ['在看论文'])
    backend = FakeStreamBackend([step(call('Recommendation', card_args(dedupeKey='paper', topic='主动式 Agent 研究',
                                                                        follow={'kind': 'github_release', 'target': 'a/b'}))), text('完成')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    await e.rec.start_run(OWNER, 'daily', trigger='manual')
    await wait_runs(e)
    card = await one(e.db.conn, 'SELECT card_id FROM rec_cards WHERE dedupe_key=?', (store.dedupe_key({'dedupeKey': 'paper'}),))
    cookie = {'openbear_web_session': await _login_cookie(e)}
    await e.client.post(f"/api/recommendations/cards/{card['card_id']}/feedback", cookies=cookie, json={'action': 'follow', 'detail': '有没有人复现'})
    follows = (await (await e.client.get('/api/recommendations/follows', cookies=cookie)).json())['items']
    kinds = {f['kind']: f for f in follows}
    assert kinds['topic']['target'] == '主动式 Agent 研究' and kinds['topic']['note'] == '有没有人复现'
    assert kinds['github_release']['target'] == 'a/b'


async def test_card_list_carries_origin_conversation_title(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followIdleMinutes=0)
    conv = await human_conversation(e, ['在比较 RAG'])
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([step(call('Recommendation', card_args(dedupeKey='o'))), text('完成')]),
                                          context_window=128000)
    await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=conv['conversation_uuid'])
    await wait_runs(e)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    items = (await (await e.client.get('/api/recommendations/cards', cookies=cookie)).json())['items']
    assert items[0]['originConversationUuid'] == conv['conversation_uuid'] and items[0]['originTitle'] == '讨论 RAG'


async def test_root_bootstrap_never_lands_in_a_recommendation_run(rec_env):
    e = rec_env
    await configure(e)
    human = await human_conversation(e, ['普通对话'])
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')]), context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily', trigger='manual')
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT conversation_uuid FROM rec_runs WHERE run_id=?', (run['id'],))
    async with e.db.write_transaction() as conn:  # the run is the most recent conversation
        await conn.execute('UPDATE web_conversations SET last_conversation_at=? WHERE conversation_uuid=?', (9_999_999_999, row['conversation_uuid']))
        await conn.execute('UPDATE web_conversations SET last_conversation_at=? WHERE conversation_uuid=?', (1, human['conversation_uuid']))
    cookie = {'openbear_web_session': await _login_cookie(e)}
    data = await (await e.client.get('/api/conversation-tree/bootstrap', cookies=cookie)).json()
    assert data['selected']['item']['conversationUuid'] == human['conversation_uuid']


async def test_runs_cannot_be_started_from_the_page(rec_env):
    e = rec_env
    await configure(e)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    response = await e.client.post('/api/recommendations/runs', cookies=cookie, json={'kind': 'daily'})
    assert response.status in (404, 405)
    assert not await many(e.db.conn, 'SELECT run_id FROM rec_runs')


async def test_follow_reuses_one_conversation_per_source_and_respects_concurrency(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followIdleMinutes=0, followCooldownMinutes=1, followConcurrency=2)
    a = await human_conversation(e, ['讨论 A'])
    b = await human_conversation(e, ['讨论 B'])
    c = await human_conversation(e, ['讨论 C'])
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')] * 8), context_window=128000)
    first = await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=a['conversation_uuid'])
    await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=b['conversation_uuid'])
    with pytest.raises(RecError):  # the third exceeds followConcurrency=2
        await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=c['conversation_uuid'])
    await wait_runs(e)
    second = await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=a['conversation_uuid'])
    await wait_runs(e)
    rows = {r['run_id']: r for r in await many(e.db.conn, 'SELECT run_id,conversation_uuid,status FROM rec_runs')}
    assert rows[first['id']]['conversation_uuid'] == rows[second['id']]['conversation_uuid']
    assert rows[second['id']]['status'] == 'completed'


async def test_internal_search_reads_own_conversations_and_memory_but_not_rec_folder(rec_env):
    e = rec_env
    await configure(e)
    await human_conversation(e, ['Parrot 的 Claude 路由要走 tw 组'])
    folder = await e.rec.ensure_folder(OWNER)
    hidden = await e.server._create_web_conversation(OWNER, folder_uuid=folder, title='推荐自己的会话 Parrot')
    await e.db.conn.execute('''INSERT INTO web_operations(conversation_uuid,internal_chat_id,op_id,op_type,turn_uuid,display_seq,status,lifecycle,
        internal,source,revision,payload_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (hidden['conversation_uuid'], hidden['internal_chat_id'], 'x', 'user_message', 't', 1, 'completed', 'terminal', 0, 'user', 1,
         json.dumps({'text': 'Parrot 路由'}), e.rec.clock(), e.rec.clock()))
    await e.db.conn.commit()
    from app.recommendations import insights
    found = await insights.internal_search(e.server, OWNER, 'parrot 路由', rec_folder=folder, now_ms=e.rec.clock() + 1000)
    kinds = [(i['kind'], i.get('conversationUuid')) for i in found['items']]
    assert kinds and all(conv != hidden['conversation_uuid'] for _, conv in kinds)
    assert any(k == 'conversation' for k, _ in kinds)


async def test_prompts_are_configurable_and_reach_the_run(rec_env):
    e = rec_env
    await configure(e, promptSystem='# 自定义推荐系统提示词', promptDaily='自定义每日任务：只看 Parrot。')
    folder = await e.rec.ensure_folder(OWNER)
    row = await one(e.db.conn, 'SELECT prompt_markdown FROM web_conversation_folders WHERE folder_uuid=?', (folder,))
    assert row['prompt_markdown'] == '# 自定义推荐系统提示词'
    await human_conversation(e, ['Parrot'])
    backend = FakeStreamBackend([text('完成')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily', trigger='manual')
    await wait_runs(e)
    sent = json.dumps(backend.seen_messages if hasattr(backend, 'seen_messages') else [], ensure_ascii=False)
    user = await one(e.db.conn, "SELECT payload_json FROM web_operations WHERE op_id=?", ('msg:' + run['id'],))
    assert '自定义每日任务：只看 Parrot。' in json.loads(user['payload_json'])['text']
    # Clearing returns to the built-in default; the settings response carries the defaults for the editor.
    data = await e.rec.save_settings(OWNER, {'settings': {**(await e.rec.get_settings(OWNER))['settings'], 'promptSystem': '', 'promptDaily': ''}})
    assert data['defaultPrompts']['system'].startswith('# OpenBear 推荐模块')
    row = await one(e.db.conn, 'SELECT prompt_markdown FROM web_conversation_folders WHERE folder_uuid=?', (folder,))
    assert row['prompt_markdown'].startswith('# OpenBear 推荐模块')


async def _delete_folder(e, folder_id, target=''):
    cookie = {'openbear_web_session': await _login_cookie(e)}
    return await e.client.post(f'/api/conversation-folders/{folder_id}/delete', cookies=cookie, json={'targetFolderId': target})


async def test_recommendation_folder_is_protected_while_enabled_then_forgotten_on_delete(rec_env):
    e = rec_env
    await configure(e)
    folder = await e.rec.ensure_folder(OWNER)
    response = await _delete_folder(e, folder)
    assert response.status == 409 and (await response.json())['error'] == 'folder_managed'
    nodes = (await e.server._tree_folders(OWNER))
    assert nodes[folder]['rec_managed'] == 1
    await e.rec.save_settings(OWNER, {'settings': {**(await e.rec.get_settings(OWNER))['settings'], 'enabled': False}})
    response = await _delete_folder(e, folder)
    assert response.status == 200, await response.text()
    assert await e.rec.rec_folder(OWNER) == ''
    assert (await one(e.db.conn, 'SELECT folder_id FROM rec_settings WHERE owner_chat_id=?', (OWNER,)))['folder_id'] == ''
    # Enabling again creates a fresh folder.
    await e.rec.save_settings(OWNER, {'settings': {**(await e.rec.get_settings(OWNER))['settings'], 'enabled': True}})
    assert (await e.rec.rec_folder(OWNER)) not in ('', folder)


async def test_new_recommendation_folder_avoids_an_existing_user_folder_name(rec_env):
    e = rec_env
    async with e.db.write_transaction() as conn:
        await conn.execute('''INSERT INTO web_conversation_folders(folder_uuid,owner_chat_id,parent_uuid,name,created_at,updated_at)
            VALUES('mine',?,'','推荐',1,1)''', (OWNER,))
    await configure(e)
    folder = await e.rec.ensure_folder(OWNER)
    row = await one(e.db.conn, 'SELECT name FROM web_conversation_folders WHERE folder_uuid=?', (folder,))
    assert folder != 'mine' and row['name'] == 'OpenBear 推荐'


async def test_follow_conversation_moved_out_of_the_folder_is_not_reused(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followIdleMinutes=0, followCooldownMinutes=1)
    conv = await human_conversation(e, ['讨论 A'])
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')] * 4), context_window=128000)
    first = await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=conv['conversation_uuid'])
    await wait_runs(e)
    first_conv = (await one(e.db.conn, 'SELECT conversation_uuid FROM rec_runs WHERE run_id=?', (first['id'],)))['conversation_uuid']
    async with e.db.write_transaction() as tx:
        await tx.execute("UPDATE web_conversations SET folder_uuid='folder' WHERE conversation_uuid=?", (first_conv,))
    second = await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=conv['conversation_uuid'])
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT conversation_uuid,status FROM rec_runs WHERE run_id=?', (second['id'],))
    assert row['status'] == 'completed' and row['conversation_uuid'] != first_conv


async def test_follow_never_takes_over_a_recommendation_conversation_the_user_is_running(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followIdleMinutes=0, followCooldownMinutes=1)
    conv = await human_conversation(e, ['讨论 A'], start_ms=e.rec.clock() - 120_000)
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')] * 4), context_window=128000)
    first = await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=conv['conversation_uuid'])
    await wait_runs(e)
    rec_conv = await one(e.db.conn, '''SELECT c.conversation_uuid,c.internal_chat_id FROM rec_runs r
        JOIN web_conversations c ON c.conversation_uuid=r.conversation_uuid WHERE r.run_id=?''', (first['id'],))
    user_turn = asyncio.create_task(asyncio.Event().wait())
    e.server.runs.register(rec_conv['internal_chat_id'], user_turn)
    try:
        # The source is due again, but the user is talking in its follow conversation: wait.
        async with e.db.write_transaction() as tx:
            await tx.execute('UPDATE rec_cursors SET follow_seq=0,follow_at_ms=0 WHERE conversation_uuid=?', (conv['conversation_uuid'],))
        settings, _ = await e.rec.settings_row(OWNER)
        await e.rec.record_skip(OWNER, settings, 'daily', e.rec.clock())  # today's daily is done
        assert await sources.follow_candidates(e.db.conn, OWNER, settings, await e.rec.rec_folder(OWNER), now_ms=e.rec.clock())
        await e.rec.tick_owner(OWNER, settings, e.rec.clock())
        assert not e.rec.tasks
        # A direct start never shares it either: it gets a fresh conversation.
        second = await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=conv['conversation_uuid'])
        await wait_runs(e)
        row = await one(e.db.conn, 'SELECT conversation_uuid,status FROM rec_runs WHERE run_id=?', (second['id'],))
        assert row['status'] == 'completed' and row['conversation_uuid'] != rec_conv['conversation_uuid']
        assert e.server.runs.task(rec_conv['internal_chat_id']) is user_turn
    finally:
        user_turn.cancel()
        await asyncio.gather(user_turn, return_exceptions=True)


@pytest.mark.parametrize('ending', ['timeout', 'stop'])
async def test_timed_out_or_stopped_run_is_not_reported_as_a_failure(rec_env, ending):
    e = rec_env
    await configure(e, runTimeoutMinutes=5)
    await human_conversation(e, ['我在比较 RAG 方案'])
    entered = asyncio.Event()

    class Hang(FakeStreamBackend):
        async def stream(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover
    e.server.llm_factory = FakeRunFactory(Hang(), context_window=128000)
    if ending == 'timeout':
        real_timeout = asyncio.timeout
        import app.recommendations.service as service_module
        service_module.asyncio = type('A', (), {**{k: getattr(asyncio, k) for k in dir(asyncio) if not k.startswith('__')},
                                                 'timeout': staticmethod(lambda _s: real_timeout(0.3))})
    try:
        run = await e.rec.start_run(OWNER, 'daily', trigger='manual')
        await asyncio.wait_for(entered.wait(), 3)
        if ending == 'stop':
            assert (await e.rec.stop(OWNER, run['id']))['accepted']
        await asyncio.wait_for(wait_runs(e), 10)
    finally:
        if ending == 'timeout':
            service_module.asyncio = asyncio
    row = await one(e.db.conn, 'SELECT status,error,notification_state FROM rec_runs WHERE run_id=?', (run['id'],))
    expected = ('timed_out', '执行超时') if ending == 'timeout' else ('cancelled', '')
    assert (row['status'], row['error']) == expected
    assert row['notification_state'] == 'suppressed'


async def test_scheduler_releases_a_run_left_active_without_a_task(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followIdleMinutes=0, followCooldownMinutes=1)
    async with e.db.write_transaction() as conn:
        await conn.execute('''INSERT INTO rec_runs(run_id,owner_chat_id,kind,trigger,root_turn_uuid,local_date,started_at_ms,status)
            VALUES('stuck',?,?,?,?,?,?,?)''', (OWNER, 'follow', 'conversation', 'r-stuck', e.rec.local_date(Settings()), e.rec.clock() - 120_000, 'running'))
    conv = await human_conversation(e, ['讨论 A'], start_ms=e.rec.clock() - 120_000)
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')]), context_window=128000)
    settings, _ = await e.rec.settings_row(OWNER)
    await e.rec.record_skip(OWNER, settings, 'daily', e.rec.clock())  # today's daily is done
    await e.rec.tick_owner(OWNER, settings, e.rec.clock())
    await wait_runs(e)
    assert (await one(e.db.conn, "SELECT status FROM rec_runs WHERE run_id='stuck'"))['status'] == 'interrupted'
    follow = await one(e.db.conn, "SELECT status FROM rec_runs WHERE kind='follow' AND source_conversation_uuid=?", (conv['conversation_uuid'],))
    assert follow and follow['status'] == 'completed'


async def test_deleted_or_out_of_scope_follow_source_ends_quietly(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followIdleMinutes=0)
    conv = await human_conversation(e, ['讨论 A'])
    settings, _ = await e.rec.settings_row(OWNER)
    ok = await sources.build_follow_batch(e.db.conn, OWNER, settings, conv['conversation_uuid'], now_ms=e.rec.clock() + 1000, from_seq=0)
    assert ok['analyzable'] == 1
    gone = await sources.build_follow_batch(e.db.conn, OWNER, settings, conv['conversation_uuid'], now_ms=e.rec.clock() + 1000, from_seq=0,
                                            rec_folder='folder')  # its folder is now excluded
    assert gone == {'text': '', 'conversations': [], 'remaining': 0, 'chars': 0, 'analyzable': 0}
    await e.db.conn.execute('DELETE FROM web_conversations WHERE conversation_uuid=?', (conv['conversation_uuid'],))
    await e.db.conn.commit()
    deleted = await sources.build_follow_batch(e.db.conn, OWNER, settings, conv['conversation_uuid'], now_ms=e.rec.clock() + 1000, from_seq=0)
    assert deleted['analyzable'] == 0 and deleted['conversations'] == []


async def test_interrupted_follow_keeps_uncommitted_material_for_retry(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followIdleMinutes=0, followCooldownMinutes=1)
    conv = await human_conversation(e, ['讨论 A', '继续 A'], start_ms=e.rec.clock() - 120_000)
    gate = asyncio.Event()

    class Hang(FakeStreamBackend):
        async def stream(self, *args, **kwargs):
            await gate.wait()
            async for item in super().stream(*args, **kwargs):
                yield item
    e.server.llm_factory = FakeRunFactory(Hang([text('完成')]), context_window=128000)
    run = await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=conv['conversation_uuid'])
    # Start records only cooldown; unread material must survive interruption.
    cursor = await one(e.db.conn, 'SELECT follow_seq FROM rec_cursors WHERE owner_chat_id=? AND conversation_uuid=?', (OWNER, conv['conversation_uuid']))
    assert cursor['follow_seq'] == 0
    assert not await sources.follow_candidates(e.db.conn, OWNER, (await e.rec.settings_row(OWNER))[0], '', now_ms=e.rec.clock() + 30_000)
    # After cooldown an interrupted, uncommitted run remains eligible.
    assert await sources.follow_candidates(e.db.conn, OWNER, (await e.rec.settings_row(OWNER))[0], '', now_ms=e.rec.clock() + 10 * 60_000)
    with pytest.raises(RecError):
        await e.rec.start_run(OWNER, 'follow', source_conversation=conv['conversation_uuid'])
    # The running follow still reads its own batch from where it started.
    row = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run['id'],))
    batch = await e.rec.source_next(row)
    assert batch['conversations'] == 1 and '继续 A' in batch['text']
    gate.set()
    await wait_runs(e)


async def test_unavailable_models_fall_back_and_are_reported(rec_env, monkeypatch):
    e = rec_env
    second = e.server.config.models.providers['openai'].models[1].id
    await configure(e, models=['openai/gpt', f'openai/{second}'])
    models_cls = type(e.server.config.models)
    original = models_cls.resolve
    gone = {'openai/gpt'}
    monkeypatch.setattr(models_cls, 'resolve', lambda self, label, **kw: None if label in gone else original(self, label, **kw))
    settings, _ = await e.rec.settings_row(OWNER)
    assert e.rec.model_health(settings) == {'usable': [f'openai/{second}'], 'unavailable': ['openai/gpt']}
    cookie = {'openbear_web_session': await _login_cookie(e)}
    data = await (await e.client.get('/api/recommendations/overview', cookies=cookie)).json()
    assert data['models']['unavailable'] == ['openai/gpt']
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')]), context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily', trigger='scheduled')
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT status,models_json FROM rec_runs WHERE run_id=?', (run['id'],))
    assert row['status'] == 'completed' and json.loads(row['models_json']) == [f'openai/{second}']
    gone.add(f'openai/{second}')
    with pytest.raises(RecError) as caught:
        await e.rec.start_run(OWNER, 'daily', trigger='scheduled')
    assert caught.value.payload['code'] == 'models_unavailable'


async def test_cards_and_runs_flag_deleted_conversations(rec_env):
    e = rec_env
    await configure(e, followMinNewUserMessages=1, followIdleMinutes=0)
    conv = await human_conversation(e, ['在比较 RAG'])
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([step(call('Recommendation', card_args(dedupeKey='z'))), text('完成')]),
                                          context_window=128000)
    await e.rec.start_run(OWNER, 'follow', trigger='conversation', source_conversation=conv['conversation_uuid'])
    await wait_runs(e)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    card = (await (await e.client.get('/api/recommendations/cards', cookies=cookie)).json())['items'][0]
    run = (await (await e.client.get('/api/recommendations/runs', cookies=cookie)).json())['items'][0]
    assert card['originExists'] is True and run['sourceExists'] is True and run['conversationExists'] is True
    await e.db.conn.execute('DELETE FROM web_conversations WHERE conversation_uuid=?', (conv['conversation_uuid'],))
    await e.db.conn.commit()
    card = (await (await e.client.get('/api/recommendations/cards', cookies=cookie)).json())['items'][0]
    run = (await (await e.client.get('/api/recommendations/runs', cookies=cookie)).json())['items'][0]
    assert card['originExists'] is False and run['sourceExists'] is False


async def test_settings_still_save_after_a_saved_model_is_removed(rec_env, monkeypatch):
    e = rec_env
    second = e.server.config.models.providers['openai'].models[1].id
    await configure(e, models=['openai/gpt', f'openai/{second}'])
    models_cls = type(e.server.config.models)
    original = models_cls.resolve
    gone = {'openai/gpt'}
    monkeypatch.setattr(models_cls, 'resolve', lambda self, label, **kw: None if label in gone else original(self, label, **kw))
    current = (await e.rec.get_settings(OWNER))['settings']
    saved = await e.rec.save_settings(OWNER, {'settings': {**current, 'followCooldownMinutes': 7}})
    assert saved['settings']['followCooldownMinutes'] == 7
    gone.add(f'openai/{second}')
    off = await e.rec.save_settings(OWNER, {'settings': {**saved['settings'], 'enabled': False}})  # turning off always works
    assert off['settings']['enabled'] is False
    with pytest.raises(RecError):  # a new, unknown model is still rejected
        await e.rec.save_settings(OWNER, {'settings': {**off['settings'], 'models': ['openai/nope']}})


async def test_to_check_is_saved_shown_and_carried_into_the_follow_up(rec_env):
    e = rec_env
    run = {'run_id': 'r', 'source_conversation_uuid': ''}
    question = '我们的计时是否把命令执行时间也算进了空转？'
    async with e.db.write_transaction() as conn:
        card_id = await store.card_save(conn, OWNER, run, card_args(toCheck=[question, '', 3])['params'], e.rec.clock())
        plain = await store.card_save(conn, OWNER, run, card_args(dedupeKey='plain', toCheck='单个问题')['params'], e.rec.clock())
    listed = {c['id']: c for c in (await store.card_list(e.db.conn, OWNER))['items']}
    assert listed[card_id]['toCheck'] == [question] and listed[plain]['toCheck'] == ['单个问题']
    resolved = await e.server._reference_store().resolve(f'[卡](openbear://ref/rec/{card_id}) 聊聊', owner=OWNER)
    assert '需要先核对的：' + question in resolved['materials'][0]['content']


async def test_review_of_an_opened_card_turns_engagement_into_a_correction(rec_env):
    e = rec_env
    await configure(e)
    now = e.rec.clock()
    async with e.db.write_transaction() as conn:
        card_id = await store.card_save(conn, OWNER, {'run_id': 'r', 'source_conversation_uuid': ''}, card_args()['params'], now)
        card = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=?', (card_id,))
        await store.feedback(conn, OWNER, card, 'sent', now, conversation_uuid='conv-a', request_id='s1')
        await store.feedback(conn, OWNER, card, 'continued', now, conversation_uuid='conv-a', request_id='c1')
    opened = await store.opened_unreviewed(e.db.conn, OWNER, now - 1)
    assert [(c['id'], c['conversations']) for c in opened] == [(card_id, ['conv-a'])]
    assert (await store.weights(e.db.conn, OWNER, now))['topic']['rag'] == 1.6
    async with e.db.write_transaction() as conn:
        card = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=?', (card_id,))
        with pytest.raises(RecError):
            await store.review(conn, OWNER, card, 'misread', '', now)
        await store.review(conn, OWNER, card, 'misread', '他要解决的是只思考不动手，不是重复调用', now)
    assert (await store.weights(e.db.conn, OWNER, now))['topic']['rag'] == -0.2
    assert not await store.opened_unreviewed(e.db.conn, OWNER, now - 1)
    assert await store.misreads(e.db.conn, OWNER, now - 1) == [
        {'card': 'GraphRAG 2.0 发布', 'topic': 'RAG', 'verdict': 'misread', 'issue': '他要解决的是只思考不动手，不是重复调用'}]
    # A second review replaces the first instead of stacking penalties.
    async with e.db.write_transaction() as conn:
        card = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=?', (card_id,))
        await store.review(conn, OWNER, card, 'held', '', now)
    assert (await store.weights(e.db.conn, OWNER, now))['topic']['rag'] == 1.6
    assert not await store.misreads(e.db.conn, OWNER, now - 1)


async def test_follow_material_keeps_the_assistant_conclusions(rec_env):
    e = rec_env
    await configure(e)
    conv = await human_conversation(e, ['早先的问题'])
    conclusion = '结论：问题是只思考不动手。' + '细节' * 900 + '已经否掉签名检测。'
    now = e.rec.clock()
    for seq, role, body in ((100, 'user_message', '那就修吧'), (110, 'assistant_message', conclusion)):
        await e.db.conn.execute('''INSERT INTO web_operations(conversation_uuid,internal_chat_id,op_id,op_type,turn_uuid,run_root_turn_uuid,
            display_seq,status,lifecycle,internal,source,revision,payload_json,created_at_ms,updated_at_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (conv['conversation_uuid'], conv['internal_chat_id'], f'{role}:{seq}', role,
            'tx', 'tx', seq, 'completed', 'terminal', 0, 'user' if role == 'user_message' else '', 1,
            json.dumps({'text': body}, ensure_ascii=False), now + seq, now + seq))
    await e.db.conn.commit()
    settings, _ = await e.rec.settings_row(OWNER)
    batch = await sources.build_follow_batch(e.db.conn, OWNER, settings, conv['conversation_uuid'], now_ms=now + 1000, from_seq=20)
    assert '已经否掉签名检测' in batch['text'] and '回答：早先的问题' in batch['text']
    daily = await sources.build_batch(e.db.conn, OWNER, settings, '', since_ms=0)
    assert '已经否掉签名检测' in daily['text']  # Daily material is now paged without dropping message tails.


async def test_daily_run_sees_opened_cards_and_records_a_review_through_the_tool(rec_env):
    e = rec_env
    await configure(e)
    now = e.rec.clock()
    async with e.db.write_transaction() as conn:
        card_id = await store.card_save(conn, OWNER, {'run_id': 'r', 'source_conversation_uuid': ''}, card_args()['params'], now)
        card = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=?', (card_id,))
        await store.feedback(conn, OWNER, card, 'sent', now, conversation_uuid='conv-a', request_id='s1')
    seen = {}

    class Scripted(FakeStreamBackend):
        async def stream(self, messages, **kw):
            if self.calls == 1:
                seen['status'] = json.loads(messages[-1]['content'])
            if self.calls == 2:
                seen['review'] = json.loads(messages[-1]['content'])
            async for event in super().stream(messages, **kw):
                yield event

    backend = Scripted([
        step(call('Recommendation', {'action': 'status'})),
        step(call('Recommendation', {'action': 'card.review', 'params': {'id': card_id, 'verdict': 'partial',
                                                                         'issue': '外部做法属实，但和他的问题不是同一个根因'}})),
        text('完成'),
    ])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    assert [c['id'] for c in seen['status']['openedCards']] == [card_id]
    assert seen['status']['openedCards'][0]['conversations'] == ['conv-a']
    assert seen['review'] == {'reviewed': True}
    row = await one(e.db.conn, 'SELECT review,review_issue FROM rec_cards WHERE card_id=?', (card_id,))
    assert row['review'] == 'partial' and '根因' in row['review_issue']
    assert (await store.misreads(e.db.conn, OWNER, now - 1))[0]['verdict'] == 'partial'


async def test_openbear_lane_sees_habits_folder_defaults_and_where_a_convention_is_written(rec_env):
    e = rec_env
    await configure(e)
    folder = await e.rec.ensure_folder(OWNER)
    conn = e.db.conn
    await conn.execute('''INSERT INTO web_conversation_folders(folder_uuid,owner_chat_id,parent_uuid,name,display_order,created_at,updated_at,
        prompt_markdown,run_defaults_json) VALUES('glm',?,'','GLM',1,0,0,'逆向分析统一放这里',?)''',
                       (OWNER, json.dumps({'mainModel': 'openai/glm'})))
    await conn.execute("INSERT INTO memory_entries(category_id,ref,title,body,enabled) VALUES(1,'rules','纠偏规则','选项用问卷让他点选',0)")
    await conn.execute("UPDATE memory_templates SET is_active=0")
    await conn.execute("INSERT INTO memory_templates(name,content,is_active) VALUES('主控','改界面前先出预览',1)")
    await conn.commit()
    now = e.rec.clock()
    async with e.db.write_transaction() as tx:
        await store.profile_upsert(tx, OWNER, {'kind': 'habit', 'topic': '界面改动先出预览', 'signal': 'repeated',
                                               'detail': '多次要求先看预览再改'}, now)
        await store.profile_upsert(tx, OWNER, {'kind': 'interest', 'topic': 'RAG', 'signal': 'explicit'}, now)
        await store.profile_upsert(tx, OWNER, {'kind': 'preference', 'topic': '一次性的小偏好', 'signal': 'weak'}, now)
    human = await human_conversation(e, ['不要再横向拖动了'])
    rec_conv = await e.server._create_web_conversation(OWNER, folder_uuid=folder, title='[推荐] 每日整理')
    await conn.execute('''INSERT INTO web_operations(conversation_uuid,internal_chat_id,op_id,op_type,turn_uuid,display_seq,status,lifecycle,
        internal,source,revision,payload_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (rec_conv['conversation_uuid'], rec_conv['internal_chat_id'], 'r', 'user_message', 't', 1, 'completed', 'terminal', 0,
         'recommendation', 1, json.dumps({'text': '不要为了产出而搜索'}), now, now))
    await conn.commit()
    from app.recommendations import insights
    data = await insights.summary(e.server, OWNER, now_ms=now + 1000, rec_folder=folder)
    assert [h['topic'] for h in data['habits']] == ['界面改动先出预览']
    assert {'folder': 'GLM', 'folderId': 'glm', 'promptChars': 9, 'defaultModel': 'openai/glm', 'workspaceDir': ''} in data['folders']
    assert all(f['folderId'] != folder for f in data['folders'])
    assert [(m['ref'], m['title']) for m in data['disabledMemoryEntries']] == [('rules', '纠偏规则')]
    assert data['activeTemplate']['name'] == '主控'
    assert [c['conversation_uuid'] for c in data['possibleCorrections']] == [human['conversation_uuid']]
    found = await insights.locate(e.server, OWNER, '问卷 点选', rec_folder=folder)
    assert [(i['kind'], i['name'], i['enabled']) for i in found['items']] == [('memory', 'rules', False)]
    assert [i['kind'] for i in (await insights.locate(e.server, OWNER, '预览', rec_folder=folder))['items']] == ['template']
    assert [i['name'] for i in (await insights.locate(e.server, OWNER, '逆向', rec_folder=folder))['items']] == ['GLM']
