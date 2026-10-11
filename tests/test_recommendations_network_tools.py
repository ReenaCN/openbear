"""Recommendation network selection on isolated SQLite, real policies and fake MCP."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import MCPServerConfig
from app.context.prompt_policy import PromptToolRegistry, read_json, write_json
from app.mcp.manager import MCPManager
from app.mcp.permissions import build_tool_meta
from app.mcp.registry import register_mcp_tools
from app.mcp.types import MCPRawResult, MCPRawTool, MCPServerState
from app.recommendations.contracts import RecError
from app.tools.base import ToolRuntimeContext
from app.web_console.prompt_policy import folder_policy_key
from app.webhooks.repository import one
from tests.test_recommendations import (  # noqa: F401
    OWNER, base_env, call, configure, human_conversation, rec_env, step, text, wait_runs,
)
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _login_cookie


class LocalMCP:
    def __init__(self):
        self.calls = []
        self.transport = SimpleNamespace(fatal_error='')

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return MCPRawResult(content=[{'type': 'text', 'text': 'isolated research result'}])


def install_mcp(e, *, server='research', names=('discover', 'document'), approval='allow', read_only=True):
    cfg = e.server.config
    cfg.mcp.enabled = True
    cfg.mcp.servers[server] = MCPServerConfig(transport='stdio', command='never-spawned', approval=approval)
    manager = MCPManager(cfg, db=e.db)
    client = LocalMCP()
    manager._clients[server] = client
    manager._states[server] = MCPServerState(key=server, transport='stdio', status='connected', approval=approval)
    for name in names:
        meta = build_tool_meta(cfg.mcp, cfg.mcp.servers[server], server_key=server, used_names=set(manager._tools),
                               raw_tool=MCPRawTool(name=name, description='User-assigned purpose: ' + name,
                                   input_schema={'type': 'object', 'properties': {'query': {'type': 'string'}}},
                                   annotations={'readOnlyHint': read_only}))
        manager._tools[meta.public_name] = meta
        manager._all_tools.append(meta)
    register_mcp_tools(e.server.tools, manager)
    return manager, client, list(manager._tools)


async def saved_policy(e):
    folder = await e.rec.rec_folder(OWNER)
    return await read_json(e.db.conn, folder_policy_key(OWNER, folder))


async def test_non_parrot_selection_reaches_actual_model_tools_and_mcp_handler(rec_env):
    e = rec_env
    manager, client, names = install_mcp(e)
    async def unused(args):
        pytest.fail('unselected tool must never run')
    e.server.tools.add('Unrelated', 'Not network-classified by the app', {}, unused)
    e.server.tools.add('AgentOnly', 'Not visible to the controller', {}, unused, visibility={'agent'})
    cookie = {'openbear_web_session': await _login_cookie(e)}
    data = await (await e.client.get('/api/recommendations/settings', cookies=cookie)).json()
    assert data['settings']['networkTools'] == {'search': [], 'fetch': []}
    options = {item['name']: item for item in data['networkToolOptions']}
    assert set(options) == {*names, 'Unrelated', 'Webhook'}  # No heuristic name-based filtering.
    assert all(options[name]['available'] for name in names)
    original_mcp = e.server.config.mcp.model_dump()
    response = await e.client.put('/api/recommendations/settings', cookies=cookie, json={'settings': {
        'enabled': True, 'models': ['openai/gpt'], 'networkTools': {'search': names[:1], 'fetch': names[1:]}}})
    assert response.status == 200
    assert set((await saved_policy(e))['toolNames']) == {'Recommendation', *names}
    backend = FakeStreamBackend([step(call(names[0], {'query': 'public topic'})),
                                 step(call(names[1], {'query': 'public document'})), text('完成')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    assert (await one(e.db.conn, 'SELECT status FROM rec_runs WHERE run_id=?', (run['id'],)))['status'] == 'completed'
    assert {tool['name'] for tool in backend.seen_tools[0]} == {'Recommendation', *names}
    assert client.calls == [('discover', {'query': 'public topic'}), ('document', {'query': 'public document'})]
    assert e.server.config.mcp.model_dump() == original_mcp and not manager._conversation_grants


async def test_absent_and_denied_tools_cannot_be_newly_selected_but_empty_selection_runs(rec_env):
    e = rec_env
    _, _, names = install_mcp(e, approval='deny')
    data = await e.rec.get_settings(OWNER)
    assert not any(item['available'] for item in data['networkToolOptions'] if item['name'] in names)
    for name in [names[0], 'mcp__missing__search', 'Recommendation']:
        with pytest.raises(RecError) as error:
            await configure(e, networkTools={'search': [name], 'fetch': []})
        assert error.value.status == 422
    await configure(e, networkTools={'search': [], 'fetch': []})
    assert (await saved_policy(e))['toolNames'] == ['Recommendation']
    backend = FakeStreamBackend([step(call('Recommendation', {'action': 'status'})), text('内部推荐仍可运行')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    assert (await one(e.db.conn, 'SELECT status FROM rec_runs WHERE run_id=?', (run['id'],)))['status'] == 'completed'
    assert {tool['name'] for tool in backend.seen_tools[0]} == {'Recommendation'}


async def test_offline_selection_survives_save_and_reconnect_and_removal_preserves_user_tools(rec_env):
    e = rec_env
    manager, client, names = install_mcp(e)
    saved = await configure(e, networkTools={'search': names[:1], 'fetch': names[1:]})
    folder = await e.rec.rec_folder(OWNER)
    policy = await saved_policy(e)
    policy['toolNames'].append('UserAddedTool')
    async with e.db.write_transaction() as conn:
        await write_json(conn, folder_policy_key(OWNER, folder), policy)
    # The stale registry still contains these tools; the transport failure must be shown.
    client.transport.fatal_error = 'offline'
    data = await e.rec.get_settings(OWNER)
    assert data['settings']['networkTools'] == saved['settings']['networkTools']
    assert not any(item['available'] for item in data['networkToolOptions'] if item['name'] in names)
    data = await e.rec.save_settings(OWNER, {'settings': {**data['settings'], 'enabled': False, 'initialDays': 9}})
    assert data['settings']['networkTools'] == saved['settings']['networkTools']
    assert set((await saved_policy(e))['toolNames']) == {'Recommendation', 'UserAddedTool', *names}
    # Complete deregistration likewise retains saved names instead of validating them away.
    for name in names:
        e.server.tools._tools.pop(name)
    data = await e.rec.save_settings(OWNER, {'settings': data['settings']})
    assert data['settings']['networkTools'] == saved['settings']['networkTools']
    client.transport.fatal_error = ''
    manager._states['research'].status = 'connected'
    register_mcp_tools(e.server.tools, manager)
    assert all(item['available'] for item in (await e.rec.get_settings(OWNER))['networkToolOptions'])
    # Removal applies even while disabled, and cannot leave old managed permissions behind.
    await e.rec.save_settings(OWNER, {'settings': {**data['settings'], 'networkTools': {'search': [], 'fetch': []}}})
    assert set((await saved_policy(e))['toolNames']) == {'Recommendation', 'UserAddedTool'}
    await e.rec.ensure_folder(OWNER)
    assert set((await saved_policy(e))['toolNames']) == {'Recommendation', 'UserAddedTool'}


async def test_legacy_parrot_grants_are_preserved_once_without_autogranting_new_installations(rec_env):
    e = rec_env
    _, _, names = install_mcp(e, server='parrot', names=('web_search', 'web_fetch'))
    assert (await e.rec.get_settings(OWNER))['settings']['networkTools'] == {'search': [], 'fetch': []}
    data = await configure(e)
    assert (await saved_policy(e))['toolNames'] == ['Recommendation']  # Fresh even with Parrot installed.
    folder = await e.rec.rec_folder(OWNER)
    policy = await saved_policy(e)
    policy['toolNames'] += [*names, 'UserAddedTool']
    old = {key: value for key, value in data['settings'].items() if key != 'networkTools'}
    async with e.db.write_transaction() as conn:
        await conn.execute('UPDATE rec_settings SET config_json=? WHERE owner_chat_id=?', (json.dumps(old), OWNER))
        await conn.execute('DELETE FROM app_state WHERE key=?', (f'recommendation_managed_tools:{OWNER}:{folder}',))
        await write_json(conn, folder_policy_key(OWNER, folder), policy)
        await conn.execute('UPDATE web_conversation_folders SET prompt_markdown=? WHERE folder_uuid=?', ('用户目录正文', folder))
    data = await e.rec.get_settings(OWNER)
    assert data['settings']['networkTools'] == {'search': names[:1], 'fetch': names[1:]}
    # An old API client that omits the new field must not erase it.
    data = await e.rec.save_settings(OWNER, {'settings': {**old, 'initialDays': 12}})
    assert data['settings']['networkTools'] == {'search': names[:1], 'fetch': names[1:]}
    assert set((await saved_policy(e))['toolNames']) == {'Recommendation', 'UserAddedTool', *names}
    assert (await one(e.db.conn, 'SELECT prompt_markdown FROM web_conversation_folders WHERE folder_uuid=?', (folder,)))['prompt_markdown'] == '用户目录正文'
    await e.rec.save_settings(OWNER, {'settings': {**data['settings'], 'networkTools': {'search': [], 'fetch': []}}})
    assert set((await saved_policy(e))['toolNames']) == {'Recommendation', 'UserAddedTool'}
    assert (await e.rec.get_settings(OWNER))['settings']['networkTools'] == {'search': [], 'fetch': []}


async def test_queued_folder_refresh_cannot_restore_explicitly_removed_tools(rec_env, monkeypatch):
    e = rec_env
    _, _, names = install_mcp(e)
    saved = await configure(e, networkTools={'search': names[:1], 'fetch': names[1:]})
    real_lock = e.server._conversation_tree_lock
    waiting, release = asyncio.Event(), asyncio.Event()
    queued = None

    class PausedLock:
        async def __aenter__(self):
            if asyncio.current_task() is queued:
                waiting.set()
                await release.wait()
            return await real_lock.__aenter__()

        async def __aexit__(self, *args):
            return await real_lock.__aexit__(*args)

    monkeypatch.setattr(e.server, '_conversation_tree_lock', PausedLock())
    queued = asyncio.create_task(e.rec.ensure_folder(OWNER))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        await e.rec.save_settings(OWNER, {'settings': {**saved['settings'], 'networkTools': {'search': [], 'fetch': []}}})
        assert (await saved_policy(e))['toolNames'] == ['Recommendation']
        release.set()
        await asyncio.wait_for(queued, 3)
        assert (await saved_policy(e))['toolNames'] == ['Recommendation']
    finally:
        release.set()
        await asyncio.gather(queued, return_exceptions=True)


async def test_next_reused_follow_applies_removed_and_new_network_tools(rec_env):
    e = rec_env
    _, _, names = install_mcp(e, names=('first', 'second'))
    await configure(e, networkTools={'search': names[:1], 'fetch': []})
    conv = await human_conversation(e, ['待跟随的消息'])
    backend = FakeStreamBackend([text('first'), text('second')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    first = await e.rec.start_run(OWNER, 'follow', source_conversation=conv['conversation_uuid'])
    await wait_runs(e)
    # The base fixture stubs system rendering. Make its snapshot match the real
    # folder prompt so the pre-existing text-change branch cannot hide a stale tool policy.
    settings, _ = await e.rec.settings_row(OWNER)
    async with e.db.write_transaction() as conn:
        await conn.execute('''UPDATE sessions SET system_snapshot=? WHERE chat_id=(
            SELECT c.internal_chat_id FROM web_conversations c JOIN rec_runs r
            ON r.conversation_uuid=c.conversation_uuid WHERE r.run_id=?)''',
                           (e.rec.system_prompt(settings), first['id']))
    data = await e.rec.get_settings(OWNER)
    await e.rec.save_settings(OWNER, {'settings': {**data['settings'], 'networkTools': {'search': names[1:], 'fetch': []}}})
    second = await e.rec.start_run(OWNER, 'follow', source_conversation=conv['conversation_uuid'])
    await wait_runs(e)
    a = await one(e.db.conn, 'SELECT conversation_uuid,status FROM rec_runs WHERE run_id=?', (first['id'],))
    b = await one(e.db.conn, 'SELECT conversation_uuid,status FROM rec_runs WHERE run_id=?', (second['id'],))
    assert a == b and a['status'] == 'completed'
    assert {tool['name'] for tool in backend.seen_tools[0]} == {'Recommendation', names[0]}
    assert {tool['name'] for tool in backend.seen_tools[1]} == {'Recommendation', names[1]}


async def test_selecting_ask_tool_does_not_bypass_existing_mcp_confirmation(rec_env):
    e = rec_env
    manager, client, names = install_mcp(e, approval='ask', read_only=False)
    confirmations = []
    async def deny(payload):
        confirmations.append(payload)
        return {'cancelled': True, 'authorizationGranted': False}
    original_mcp = e.server.config.mcp.model_dump()
    await configure(e, networkTools={'search': names[:1], 'fetch': []})
    registry = PromptToolRegistry(e.server.tools, (await saved_policy(e))['toolNames'])
    result = await registry.dispatch(names[0], '{}', context=ToolRuntimeContext(source='web', recommendation_run_id='test', web_confirm=deny))
    assert json.loads(result)['status'] == 'denied'
    assert len(confirmations) == 1 and confirmations[0]['_requiresAuthorization'] is True
    assert client.calls == []
    assert e.server.config.mcp.model_dump() == original_mcp and not manager._conversation_grants
