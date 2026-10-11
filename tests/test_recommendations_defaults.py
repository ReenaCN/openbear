"""Default recommendation settings through real HTTP, scheduling and Web runtime.

Only model responses are scripted; no live model, MCP or notification upstream is used.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.context.prompt_policy import read_json
from app.recommendations import prompts, store
from app.recommendations.contracts import Settings
from app.web_console.prompt_policy import folder_policy_key
from app.webhooks.repository import many, one
from tests.test_recommendations import (  # noqa: F401
    OWNER, base_env, call, human_conversation, rec_env, step, text, wait_runs,
)
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _login_cookie


async def test_default_disabled_start_and_saved_defaults_never_call_model(rec_env):
    e = rec_env
    backend = FakeStreamBackend([text('不应调用')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    folders = await many(e.db.conn, 'SELECT * FROM web_conversation_folders')
    initial = await e.rec.get_settings(OWNER)
    assert initial['settings'] == Settings().wire()
    assert not initial['settings']['enabled'] and initial['settings']['models'] == []
    assert initial['settings']['networkTools'] == {'search': [], 'fetch': []}
    await e.rec.start()
    await e.rec.tick()
    assert not await many(e.db.conn, 'SELECT * FROM rec_settings')
    assert not await many(e.db.conn, 'SELECT * FROM rec_runs')
    # Opening/saving untouched settings also must not opt in or choose a model/tool.
    cookie = {'openbear_web_session': await _login_cookie(e)}
    response = await e.client.put('/api/recommendations/settings', cookies=cookie,
                                  json={'settings': {}, 'expectedRevision': 0})
    assert response.status == 200
    assert (await response.json())['settings'] == Settings().wire()
    await e.rec.tick()
    assert not await many(e.db.conn, 'SELECT * FROM rec_runs')
    assert not await many(e.db.conn, 'SELECT * FROM model_calls')
    assert await many(e.db.conn, 'SELECT * FROM web_conversation_folders') == folders
    assert backend.calls == 0 and not e.rec.tasks
    assert e.rec.loop_task is not None and not e.rec.loop_task.done()


@pytest.mark.parametrize('explicit_defaults', [False, True], ids=['other-fields-omitted', 'explicit-defaults'])
async def test_model_and_enable_are_sufficient_for_default_daily_and_follow_runtime(rec_env, monkeypatch, explicit_defaults):
    e = rec_env
    # The shared fixture replaces system rendering. Use the production renderer here
    # to prove blank overrides produce the real recommendation prompt, not a stub.
    monkeypatch.delattr(e.server, '_build_system_prompt_for_chat')
    assert e.server.config.memory.provider == 'builtin'
    assert not e.server.config.mcp.enabled and not e.server.config.mcp.servers
    now = e.rec.clock()
    e.rec.clock = lambda: now
    primary = await human_conversation(e, ['资料整理：我想把这些资料归档'] * 4, start_ms=now - 4 * 60_000)
    async with e.db.write_transaction() as conn:
        await conn.execute("INSERT INTO web_conversation_folders(folder_uuid,owner_chat_id,name) VALUES('other',?,'另一个目录')", (OWNER,))
    await human_conversation(e, ['资料整理：另一个目录保留的既有方案'], folder='other', start_ms=now - 2 * 86_400_000)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    body = Settings().wire() if explicit_defaults else {}
    body.update(enabled=True, models=['openai/gpt'])
    response = await e.client.put('/api/recommendations/settings', cookies=cookie,
                                  json={'settings': body, 'expectedRevision': 0})
    assert response.status == 200, await response.text()
    saved = await response.json()
    defaults = Settings(enabled=True, models=['openai/gpt'])
    assert saved['settings'] == defaults.wire()
    assert saved['derivedDailyTime'] == '05:30'
    assert saved['settings']['dailyTime'] == ''
    assert saved['settings']['dailyBudgetUsd'] is None and saved['settings']['dailyBudgetTokens'] is None
    assert saved['settings']['scopeMode'] == 'all' and saved['settings']['notifyMode'] == 'inherit'
    assert all(saved['settings'][key] == '' for key in ('promptSystem', 'promptDaily', 'promptFollow'))
    assert saved['settings']['networkTools'] == {'search': [], 'fetch': []}
    policy = await read_json(e.db.conn, folder_policy_key(OWNER, saved['folderId']))
    assert policy['toolNames'] == ['Recommendation']
    assert (await one(e.db.conn, 'SELECT prompt_markdown FROM web_conversation_folders WHERE folder_uuid=?',
                      (saved['folderId'],)))['prompt_markdown'] == prompts.SYSTEM

    class InternalRecommendation(FakeStreamBackend):
        def __init__(self, kind):
            self.results = {}
            super().__init__([
                step(call('Recommendation', {'action': 'status'})),
                step(call('Recommendation', {'action': 'source.next'})),
                step(),  # source.commit uses the real batch ID returned by the tool.
                step(call('Recommendation', {'action': 'internal.search', 'params': {'query': '资料整理'}})),
                step(call('Recommendation', {'action': 'insights.get'})),
                step(call('Recommendation', {'action': 'card.save', 'params': {
                    'lane': 'openbear' if kind == 'daily' else 'external',
                    'category': 'openbear' if kind == 'daily' else 'resume',
                    'title': '接回自己的资料整理方案', 'body': '另一个目录已有归档方案，可以回到原对话继续整理。',
                    'reason': '来自用户自己的两处对话，不涉及外部资料', 'dedupeKey': 'internal-' + kind,
                    'location': {'conversationUuid': primary['conversation_uuid'], 'folderId': 'folder'},
                }})),
                text('已基于既有对话保存 1 张内部推荐卡片；本轮未联网。'),
            ])

        async def stream(self, messages, **kwargs):
            if self.calls:
                self.results[self.calls] = json.loads(messages[-1]['content'])
            if self.calls == 2:
                self.scripts[2] = step(call('Recommendation', {'action': 'source.commit',
                                                              'params': {'batchId': self.results[2]['batchId']}}))
            async for event in super().stream(messages, **kwargs):
                yield event

    # Default dailyTime derives from activity; afterwards the default four-message,
    # three-minute-idle follow rule triggers without changing any recommendation setting.
    for kind, trigger, task_prompt in [('daily', 'scheduled', prompts.DAILY), ('follow', 'conversation', prompts.FOLLOW)]:
        backend = InternalRecommendation(kind)
        e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
        await e.rec.tick()
        await asyncio.wait_for(wait_runs(e), 5)
        row = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE kind=?', (kind,))
        assert row and row['status'] == 'completed', row
        assert row['trigger'] == trigger and row['error'] == '' and row['model_calls'] == 7 and row['cards'] == 1
        assert row['notification_state'] == ('suppressed' if kind == 'daily' else 'not_requested')
        assert backend.calls == 7 and prompts.SYSTEM.strip() in backend.seen_systems[0]
        assert task_prompt.strip() in next(message['content'] for message in backend.seen_convos[0] if message['role'] == 'user')
        assert all({tool['name'] for tool in tools} == {'Recommendation'} for tools in backend.seen_tools)
        assert backend.results[1]['budget']['budgetUsd'] is None and backend.results[1]['budget']['budgetTokens'] is None
        assert not backend.results[1]['budget']['exhausted']
        assert '资料整理' in backend.results[2]['text'] and backend.results[4]['items']
        assert 'folders' in backend.results[5]  # insights.get really completed, not an error-shaped tool result.
        if kind == 'daily':
            assert '另一个目录保留的既有方案' in backend.results[2]['text']  # Default scope covers both folders.
        else:
            assert row['source_conversation_uuid'] == primary['conversation_uuid']
            assert '另一个目录保留的既有方案' not in backend.results[2]['text']
        user = await one(e.db.conn, "SELECT payload_json FROM web_operations WHERE op_id=?", ('msg:' + row['run_id'],))
        assert task_prompt.strip() in json.loads(user['payload_json'])['text']
    cards = (await store.card_list(e.db.conn, OWNER))['items']
    assert len(cards) == 2 and all(card['sources'] == [] for card in cards)
    assert all('自己的两处对话' in card['reason'] for card in cards)
    cursor = await one(e.db.conn, 'SELECT * FROM rec_cursors WHERE conversation_uuid=?', (primary['conversation_uuid'],))
    assert cursor['last_op_seq'] == 80 and cursor['follow_seq'] == 80
    # Running never materializes prompt text into the user's empty overrides or adds tools/budget.
    assert (await e.rec.get_settings(OWNER))['settings'] == defaults.wire()
    await e.rec.tick()
    assert (await one(e.db.conn, 'SELECT COUNT(*) n FROM rec_runs'))['n'] == 2
    assert not e.rec.tasks


async def test_legacy_settings_without_network_field_keep_all_custom_values(rec_env):
    e = rec_env
    legacy = {
        'enabled': False, 'models': ['openai/cheap'], 'thinkingLevel': 'low', 'timezone': 'UTC',
        'dailyTime': '21:45', 'dailyBudgetUsd': 0.25, 'dailyBudgetTokens': 12000,
        'scopeMode': 'exclude', 'scopeFolders': ['folder'], 'notifyMode': 'silent', 'initialDays': 23,
        'followEnabled': False, 'followCooldownMinutes': 19, 'runTimeoutMinutes': 17,
        'promptSystem': '用户自己的推荐判断规则', 'promptDaily': '用户自己的每日整理步骤',
        'promptFollow': '用户自己的跟随步骤',
    }
    raw = json.dumps(legacy, ensure_ascii=False)
    async with e.db.write_transaction() as conn:
        await conn.execute('INSERT INTO rec_settings(owner_chat_id,config_json,updated_at_ms) VALUES(?,?,?)',
                           (OWNER, raw, e.rec.clock()))
    cookie = {'openbear_web_session': await _login_cookie(e)}
    loaded = await (await e.client.get('/api/recommendations/settings', cookies=cookie)).json()
    assert all(loaded['settings'][key] == value for key, value in legacy.items())
    assert loaded['settings']['networkTools'] == {'search': [], 'fetch': []}
    assert (await one(e.db.conn, 'SELECT config_json FROM rec_settings WHERE owner_chat_id=?', (OWNER,)))['config_json'] == raw
    response = await e.client.put('/api/recommendations/settings', cookies=cookie,
                                  json={'settings': legacy, 'expectedRevision': loaded['revision']})
    assert response.status == 200
    saved = (await response.json())['settings']
    assert all(saved[key] == value for key, value in legacy.items())
    assert saved['networkTools'] == {'search': [], 'fetch': []}
    await e.rec.tick()
    assert not await many(e.db.conn, 'SELECT * FROM rec_runs')
