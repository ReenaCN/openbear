"""Regression boundaries for A1-A4: real gates/SQLite, no external effects."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.interaction_data import explicit_authorization
from app.mcp.manager import MCPManager
from app.mcp.registry import register_mcp_tools
from app.mcp.types import MCPRawResult, MCPToolMeta
from app.tools.base import ToolRegistry, ToolRuntimeContext, redact_tool_result_for_audit
from app.tools.confirmation import APPROVAL_ARGUMENT, constraints_within
from app.tools.openbear_control import register_openbear_control_tool
from tests import test_user_interactions as ui
from tests import test_browser_native as browser_tests
from tests.test_amended_confirmation import issue_receipt
from tests.test_cron import base_env, cron_env, create as create_cron
from tests.test_mcp import _cfg
from tests.test_openbear_control_tool import FakeSvc
from tests.test_webhooks_backend import env as webhook_env, create as create_webhook, accept as accept_webhook

interactions = ui.interactions
browser_service = browser_tests.service


def context(service):
    ctx = ToolRuntimeContext(chat_id=123, source='web', conversation_uuid='conv', turn_uuid='turn')
    async def confirm(payload):
        return await service.request(payload, owner_chat_id=123, conversation_uuid=ctx.conversation_uuid, turn_uuid=ctx.turn_uuid)
    ctx.web_confirm = confirm
    return ctx


def answer_on_creation(service, text, *, select=False):
    shown = []
    async def listener(event, item):
        assert not service._lock.locked(), 'No callbacks under the submission lock'
        if event == 'created':
            shown.append(item)
            answer = {'decision': 'confirm_with_changes', 'text': text}
            if select:
                answer['selectedValues'] = ['always']
            assert (await service.submit(item['interactionId'], 123, answer))['ok']
    service.add_listener(listener)
    return shown


def mcp_registry(executed):
    class Client:
        async def call_tool(self, name, arguments):
            executed.append(arguments)
            return MCPRawResult(content=[{'type': 'text', 'text': 'mock executed'}])
    manager = MCPManager(_cfg())
    meta = MCPToolMeta(public_name='mcp__serena__delete_symbol', server_key='serena',
        original_tool_name='delete_symbol', normalized_tool_name='delete_symbol', description='mock', approval='ask', risk='destructive')
    manager._tools[meta.public_name] = meta
    manager._clients['serena'] = Client()
    registry = ToolRegistry()
    register_mcp_tools(registry, manager)
    return registry, meta, manager


@pytest.mark.parametrize('changed', [
    {'file': 'other.py', 'symbol': '*', 'op': 'delete'},
    {'file': 'safe.py', 'symbol': 'x', 'op': 'publish'},
    {'file': 'safe.py', 'symbol': 'x', 'op': 'delete', 'overload': 'specific'},
    {'file': 'safe.py', 'symbol': 'x', 'op': 'delete', '_approvalScope': {'trusted': True}},
])
async def test_mcp_changed_arguments_require_fresh_confirmation(interactions, changed):
    executed = []
    reg, meta, manager = mcp_registry(executed)
    text = '只删除 safe.py 中 x 的指定重载，不修改其他文件或符号'
    shown = answer_on_creation(interactions, text, select=True)
    ctx = context(interactions)
    first = json.loads(await reg.dispatch(meta.public_name, json.dumps({'file': 'safe.py', 'symbol': 'x', 'op': 'delete'}), context=ctx))
    assert first['status'] == 'amendments_required' and not executed
    denied = json.loads(await reg.dispatch(meta.public_name, json.dumps({**changed, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert denied['status'] == 'denied'
    assert denied['confirmation']['error'] == 'amended_approval_arguments_changed'
    assert '不带 _openbearApprovalId' in denied['confirmation']['message']
    assert first['confirmation']['text'] == text
    assert not executed and len(shown) == 1
    # Explicit safe fallback: revised arguments without receipt create a NEW
    # gate. Feedback there does not become execution or persistent trust.
    interactions._listeners.clear()
    async def feedback(event, item):
        if event == 'created':
            shown.append(item)
            await interactions.submit(item['interactionId'], 123, {'decision': 'feedback', 'text': text, 'selectedValues': ['always']})
    interactions.add_listener(feedback)
    assert json.loads(await reg.dispatch(meta.public_name, json.dumps(changed), context=ctx))['status'] == 'denied'
    assert len(shown) == 2 and not executed and not manager._has_conversation_grant(meta, ctx)
    # Fresh, explicit authorization covers the revised parameters, not the old receipt.
    interactions._listeners.clear()
    async def allow_once(event, item):
        if event == 'created':
            shown.append(item)
            await interactions.submit(item['interactionId'], 123, {'selectedValues': ['once']})
    interactions.add_listener(allow_once)
    assert await reg.dispatch(meta.public_name, json.dumps(changed), context=ctx) == 'mock executed'
    assert executed == [changed] and len(shown) == 3
    assert not manager._has_conversation_grant(meta, ctx)


async def test_mcp_exact_arguments_types_order_and_unknown_outcome(interactions):
    attempts = []
    reg, meta, manager = mcp_registry([])
    class TimedOut:
        async def call_tool(self, name, arguments):
            attempts.append(arguments)
            raise TimeoutError()
    manager._clients[meta.server_key] = TimedOut()
    shown = answer_on_creation(interactions, '先保存副本。只调用这个对象一次，超时不要重复', select=True)
    ctx = context(interactions)
    first = json.loads(await reg.dispatch(meta.public_name, '{"file":"a","mode":1}', context=ctx))
    denied = json.loads(await reg.dispatch(meta.public_name, json.dumps({'file': 'a', 'mode': True, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert denied['status'] == 'denied' and not attempts
    args = {'mode': 1, 'file': 'a', APPROVAL_ARGUMENT: first['approvalId']}
    second = json.loads(await reg.dispatch(meta.public_name, json.dumps(args), context=ctx))
    assert second['outcome'] == 'unknown' and second['error'] == 'mcp_tool_timeout'
    third = json.loads(await reg.dispatch(meta.public_name, json.dumps(args), context=ctx))
    assert third['status'] == 'denied' and len(attempts) == 1 and len(shown) == 1


async def cron_setup(e, interactions):
    job = await create_cron(e)
    row = await e.server._create_web_conversation(123, folder_uuid='folder')
    ctx = context(interactions)
    ctx.conversation_uuid, ctx.chat_id = row['conversation_uuid'], row['internal_chat_id']
    return job, ctx


async def add_cron_run(e, job, occurrence):
    async with e.db.write_transaction(label='fixture-run') as conn:
        row = await e.cron.row(conn, 123, job['id'])
        return await e.cron.accept_run(conn, row, e.cron.clock(), 'manual', occurrence)


@pytest.mark.parametrize('change', ['finish', 'replace_run', 'add_run', 'other_job', 'revision'])
async def test_cron_receipt_accepts_only_same_job_and_narrower_site(interactions, cron_env, change):
    e = cron_env
    job, ctx = await cron_setup(e, interactions)
    run = await add_cron_run(e, job, 'first')
    shown = answer_on_creation(interactions, '等当前执行结束后删除，保留所有记录；不要停止任务')
    args = {'action': 'delete', 'params': {'jobId': job['id'], 'expectedRevision': job['revision'], 'requestId': 'delete'}}
    first = json.loads(await e.server.tools.dispatch('Cron', json.dumps(args), context=ctx))
    assert first['status'] == 'amendments_required'
    if change in ('finish', 'replace_run'):
        await e.db.conn.execute("UPDATE cron_runs SET status='completed' WHERE run_id=?", (run['run_id'],))
        await e.db.conn.commit()
    if change in ('replace_run', 'add_run'):
        await add_cron_run(e, job, 'second')
    if change == 'other_job':
        args['params']['jobId'] = (await create_cron(e))['id']
    if change == 'revision':
        await e.db.conn.execute('UPDATE cron_jobs SET revision=revision+1 WHERE job_id=?', (job['id'],))
        await e.db.conn.commit()
        args['params']['expectedRevision'] += 1
    second = json.loads(await e.server.tools.dispatch('Cron', json.dumps({**args, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert len(shown) == 1
    if change == 'finish':
        assert second['job']['scheduleState'] == 'deleted'
        assert (await e.cron.get_run(123, run['run_id']))['run']['status'] == 'completed'
        assert first['approvalId'] not in interactions._amended_approvals
        # Business idempotency may return the old result but never re-executes.
        await e.server.tools.dispatch('Cron', json.dumps({**args, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx)
        assert (await e.cron.runs(123, {}))['total'] == 1
    else:
        assert second['userFeedback']['error'] == 'amended_approval_invalid_or_out_of_scope'
        assert (await e.cron.get(123, job['id']))['job']['scheduleState'] != 'deleted'


@pytest.mark.parametrize('before,after,allowed', [
    ({'stopRunning': False}, {'stopRunning': True}, False),
    ({'stopRunning': True}, {'stopRunning': False}, True),
    ({'stopRunning': True, 'scope': 'all'}, {'stopRunning': True, 'scope': 'current'}, True),
    ({'stopRunning': True, 'scope': 'current'}, {'stopRunning': True, 'scope': 'all'}, False),
])
async def test_webhook_receipt_never_expands_stop_effects(interactions, webhook_env, monkeypatch, before, after, allowed):
    e = webhook_env
    created = await create_webhook(e)
    eid = created['endpoint']['id']
    stops = []
    async def worker_stop(eid, scope):
        if scope == 'all': stops.append('scripts')
        return {'stopped': []}
    async def bridge_stop(eid): stops.append('model'); return {'stoppedAssignments': []}
    monkeypatch.setattr(e.worker, 'stop', worker_stop)
    monkeypatch.setattr(e.bridge, 'stop', bridge_stop)
    shown = answer_on_creation(interactions, '归档后删除；缩小停止范围，保留其余运行')
    ctx = context(interactions); ctx.webhook_owner_id = 123
    args = {'action': 'delete', 'params': {'endpointId': eid, 'expectedControlRevision': 0, 'requestId': 'delete-fixture', **before}}
    first = json.loads(await e.server.tools.dispatch('Webhook', json.dumps(args), context=ctx))
    assert first['status'] == 'amendments_required' and not stops
    revised = {**args, 'params': {**args['params'], **after}, APPROVAL_ARGUMENT: first['approvalId']}
    second = json.loads(await e.server.tools.dispatch('Webhook', json.dumps(revised), context=ctx))
    cur = await e.db.conn.execute('SELECT deleted_at_ms FROM webhook_endpoints WHERE endpoint_id=?', (eid,))
    assert bool((await cur.fetchone())['deleted_at_ms']) == allowed
    assert len(shown) == 1
    if allowed:
        assert stops == (['model'] if after['stopRunning'] else [])
        assert first['approvalId'] not in interactions._amended_approvals
    else:
        assert second['userFeedback']['error'] == 'amended_approval_invalid_or_out_of_scope'
        assert not stops


@pytest.mark.parametrize('change', ['finish', 'replacement_event', 'new_event', 'other_endpoint', 'rotate_key'])
async def test_webhook_receipt_site_subsets_identity_and_operation(interactions, webhook_env, change):
    e = webhook_env
    created = await create_webhook(e)
    eid = created['endpoint']['id']
    event = await accept_webhook(e, created)
    shown = answer_on_creation(interactions, '等现有事件完成再删除定义，保留已运行任务')
    ctx = context(interactions); ctx.webhook_owner_id = 123
    args = {'action': 'delete', 'params': {'endpointId': eid, 'stopRunning': False, 'requestId': 'delete'}}
    first = json.loads(await e.server.tools.dispatch('Webhook', json.dumps(args), context=ctx))
    if change in ('finish', 'replacement_event'):
        await e.db.conn.execute('UPDATE webhook_events SET terminal_at_ms=1,review_required=0 WHERE event_id=?', (event['eventId'],))
        await e.db.conn.commit()
    if change in ('replacement_event', 'new_event'):
        await accept_webhook(e, created, key='new')
    if change == 'other_endpoint':
        await e.db.conn.execute("INSERT INTO web_conversation_folders(folder_uuid,owner_chat_id,name) VALUES('other-folder',123,'Other')")
        await e.db.conn.commit()
        other = await e.s.create(123, {'scope': {'type': 'folder', 'id': 'other-folder'}, 'name': 'Other', 'requestId': 'other',
            'config': {'processing': {'instructions': 'fixture'}}})
        args['params']['endpointId'] = other['endpoint']['id']
    if change == 'rotate_key':
        args['action'] = 'update'; args['params']['operation'] = 'rotate_key'
    second = json.loads(await e.server.tools.dispatch('Webhook', json.dumps({**args, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert len(shown) == 1
    cur = await e.db.conn.execute('SELECT deleted_at_ms FROM webhook_endpoints WHERE endpoint_id=?', (eid,))
    assert bool((await cur.fetchone())['deleted_at_ms']) == (change == 'finish')
    if change != 'finish':
        assert second['userFeedback']['error'] == 'amended_approval_invalid_or_out_of_scope'


@pytest.mark.parametrize('first', ['submit', 'cancel'])
@pytest.mark.parametrize('shutdown', [False, True])
async def test_concurrent_cancel_or_stop_atomically_revokes_receipts(interactions, monkeypatch, first, shutdown):
    scope = {'tool': 'OpenBearControl', 'action': 'restart'}
    item, waiter = await ui.pending(interactions, 'confirm', _sourceTool='OpenBearControl',
        _requiresAuthorization=True, _approvalScope=scope)
    original = interactions.db.write_transaction
    entered, release = asyncio.Event(), asyncio.Event()
    label = 'interaction_submit' if first == 'submit' else 'interaction_terminate'
    @asynccontextmanager
    async def paused(*args, **kwargs):
        async with original(*args, **kwargs) as conn:
            if kwargs.get('label') == label:
                entered.set()
                await release.wait()
            yield conn
    monkeypatch.setattr(interactions.db, 'write_transaction', paused)
    async def submit():
        return await interactions.submit(item['interactionId'], 123, {'decision': 'confirm_with_changes', 'text': '延迟9秒'})
    async def cancel():
        if shutdown: await interactions.stop()
        else: await interactions.cancel_conversation('conv')
    task1 = asyncio.create_task(submit() if first == 'submit' else cancel())
    await asyncio.wait_for(entered.wait(), 1)
    task2 = asyncio.create_task(cancel() if first == 'submit' else submit())
    await asyncio.sleep(0)
    assert not task2.done()
    release.set()
    await asyncio.gather(task1, task2)
    await waiter
    resumed = await interactions.request({'_sourceTool': 'OpenBearControl', '_requiresAuthorization': True,
        '_approvalId': item['interactionId'], '_approvalScope': scope},
        owner_chat_id=123, conversation_uuid='conv', turn_uuid='turn')
    assert not explicit_authorization(resumed)
    assert item['interactionId'] not in interactions._amended_approvals


async def test_sensitive_conditions_lossless_without_public_scope_fingerprints(interactions):
    reg, meta, _ = mcp_registry([])
    text = ' full exact conditions\n' + '私密条件 ' * 3200 + '\n end '
    shown = answer_on_creation(interactions, text, select=True)
    result = await reg.dispatch(meta.public_name, '{"file":"safe.py"}', context=context(interactions), max_chars=300)
    parsed = json.loads(result)
    assert parsed['confirmation']['text'] == text and len(result) > 300
    assert '私密条件' not in redact_tool_result_for_audit(meta.public_name, result)
    cur = await interactions.db.conn.execute('SELECT payload_json,result_json FROM user_interactions')
    persisted = str([tuple(row) for row in await cur.fetchall()])
    assert '私密条件' not in persisted
    for projection in [persisted, json.dumps(shown)]:
        assert 'approvalScope' not in projection and 'argumentsDigest' not in projection


async def test_expired_and_agent_receipts_cannot_redeem(interactions):
    payload = await issue_receipt(interactions)
    interactions._amended_approvals[payload['_approvalId']]['approvalExpiresAtMs'] = 0
    assert not explicit_authorization(await interactions.request(payload, owner_chat_id=123, conversation_uuid='conv', turn_uuid='turn'))
    reg, meta, _ = mcp_registry([])
    ctx = context(interactions); ctx.agent_session_uuid = 'agent'
    result = json.loads(await reg.dispatch(meta.public_name, json.dumps({APPROVAL_ARGUMENT: 'receipt'}), context=ctx))
    assert result['error'] == 'invalid_amended_approval'


async def test_browser_same_page_timeout_change_not_other_page(interactions, browser_service):
    from app.tools.browser import register_browser_tool
    service = browser_service
    reg = ToolRegistry(); register_browser_tool(reg, service)
    page_a = await browser_tests.new(service, 'conv')
    page_b = await browser_tests.new(service, 'conv')
    shown = answer_on_creation(interactions, '已保存所需内容，只关闭这一页')
    ctx = context(interactions)
    args = {'action': 'recover', 'page': page_a, 'params': {'op': 'close'}}
    first = json.loads(await reg.dispatch('Browser', json.dumps(args), context=ctx))
    wrong = json.loads(await reg.dispatch('Browser', json.dumps({**args, 'page': page_b, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert wrong['confirmation']['error'] == 'amended_approval_invalid_or_out_of_scope'
    assert page_a in service.pages and page_b in service.pages
    good = json.loads(await reg.dispatch('Browser', json.dumps({**args, 'timeoutMs': 10000, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert good['status'] == 'ok' and page_a not in service.pages and page_b in service.pages
    assert first['approvalId'] not in interactions._amended_approvals and len(shown) == 1


async def test_restart_delay_can_increase_but_not_advance(interactions):
    svc = FakeSvc(); reg = ToolRegistry(); register_openbear_control_tool(reg, svc)
    shown = answer_on_creation(interactions, '延后执行，不要提前')
    ctx = context(interactions)
    args = {'action': 'restart', 'args': {'delayS': 9}}
    first = json.loads(await reg.dispatch('OpenBearControl', json.dumps(args), context=ctx))
    wrong = json.loads(await reg.dispatch('OpenBearControl', json.dumps({**args, 'args': {'delayS': 1}, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert wrong['confirmation']['error'] == 'amended_approval_invalid_or_out_of_scope'
    assert svc.control_actions.pending_count(123) == 0
    good = json.loads(await reg.dispatch('OpenBearControl', json.dumps({**args, 'args': {'delayS': 12}, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert good['status'] == 'scheduled' and svc.control_actions._after_turn[123][0].args['delayS'] == 12
    assert len(shown) == 1


@pytest.mark.parametrize('candidate', [None, {'sets': {}}, {'sets': {'ids': ['other']}},
    {'sets': {'ids': [1]}}, {'sets': {'ids': []}, 'extra': {}}, {'sets': {'ids': []}, 'minimums': {'x': 0}}])
def test_constraint_contract_is_fail_closed(candidate):
    assert not constraints_within({'sets': {'ids': ['original']}}, candidate)


@pytest.mark.parametrize('change', ['finish', 'new_run'])
async def test_cron_prepared_grant_rechecks_site_at_execution(cron_env, change):
    from app.cron.contracts import CronError
    e = cron_env
    job = await create_cron(e)
    run = await add_cron_run(e, job, 'first')
    request = {'requestId': 'delete', 'expectedRevision': job['revision']}
    prepared = await e.cron.delete_impact(123, job['id'], request, tool_approval=True)
    if change == 'finish':
        await e.db.conn.execute("UPDATE cron_runs SET status='completed' WHERE run_id=?", (run['run_id'],))
        await e.db.conn.commit()
        result = await e.cron.delete(123, job['id'], {'requestId': 'delete', 'confirmationToken': prepared['confirmationToken']}, tool_request=request)
        assert result['job']['scheduleState'] == 'deleted'
    else:
        await add_cron_run(e, job, 'second')
        with pytest.raises(CronError, match='confirmation_stale'):
            await e.cron.delete(123, job['id'], {'requestId': 'delete', 'confirmationToken': prepared['confirmationToken']}, tool_request=request)
        assert (await e.cron.get(123, job['id']))['job']['scheduleState'] != 'deleted'


@pytest.mark.parametrize('change', ['finish', 'new_event'])
async def test_webhook_prepared_grant_rechecks_site_at_execution(webhook_env, change):
    from app.webhooks.contracts import WebhookError
    e = webhook_env
    created = await create_webhook(e)
    eid = created['endpoint']['id']
    event = await accept_webhook(e, created)
    prepared = await e.s.prepare(123, eid, 'delete', {'requestId': 'delete'}, tool_approval=True)
    if change == 'finish':
        await e.db.conn.execute('UPDATE webhook_events SET terminal_at_ms=1,review_required=0 WHERE event_id=?', (event['eventId'],))
        await e.db.conn.commit()
        await e.s.control(123, eid, {'requestId': 'delete', 'confirmationToken': prepared['confirmationToken']}, action='delete')
    else:
        await accept_webhook(e, created, key='new')
        with pytest.raises(WebhookError, match='confirmation_stale'):
            await e.s.control(123, eid, {'requestId': 'delete', 'confirmationToken': prepared['confirmationToken']}, action='delete')
    cur = await e.db.conn.execute('SELECT deleted_at_ms FROM webhook_endpoints WHERE endpoint_id=?', (eid,))
    assert bool((await cur.fetchone())['deleted_at_ms']) == (change == 'finish')


async def test_webhook_rotation_parameters_are_not_assumed_monotone(interactions, webhook_env):
    e = webhook_env
    created = await create_webhook(e)
    ctx = context(interactions); ctx.webhook_owner_id = 123
    shown = answer_on_creation(interactions, '先通知对方；保持旧Key过渡期为60秒')
    args = {'action': 'update', 'params': {'endpointId': created['endpoint']['id'], 'operation': 'rotate_key',
        'graceSeconds': 60, 'requestId': 'rotate'}}
    first = json.loads(await e.server.tools.dispatch('Webhook', json.dumps(args), context=ctx))
    for grace in (0, 3600):
        denied = json.loads(await e.server.tools.dispatch('Webhook', json.dumps({**args,
            'params': {**args['params'], 'graceSeconds': grace}, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
        assert denied['userFeedback']['error'] == 'amended_approval_invalid_or_out_of_scope'
    result = json.loads(await e.server.tools.dispatch('Webhook', json.dumps({**args, APPROVAL_ARGUMENT: first['approvalId']}), context=ctx))
    assert 'previousValidUntil' in result and 'credential' not in result
    assert len(shown) == 1
