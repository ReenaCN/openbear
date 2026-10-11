"""Durable physical spend and local-day budget regression tests."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from app.llm.base import OpenBearLLMError
from app.llm.events import StreamEvent, Usage
from app.recommendations.contracts import RecError
from app.webhooks.repository import one, many
from tests.test_recommendations import base_env, rec_env, OWNER, configure, wait_runs, step, text, call
from tests.test_web_admin import FakeStreamBackend, FakeRunFactory, _login_cookie


async def test_cross_midnight_requests_charge_actual_local_day_through_web_runtime(rec_env):
    e = rec_env
    await configure(e, dailyBudgetTokens=150)
    before = int(datetime(2026, 10, 11, 23, 59, 59, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)
    clock = [before]
    e.rec.clock = lambda: clock[0]
    class Midnight(FakeStreamBackend):
        async def stream(self, messages, **kw):
            # First request begins yesterday, but its response settles today.
            clock[0] = before + 2000
            async for event in super().stream(messages, **kw):
                yield event
    backend = Midnight([step(call('Recommendation', {'action': 'status'})), text('完成')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    assert (await one(e.db.conn, 'SELECT status FROM rec_runs WHERE run_id=?', (run['id'],)))['status'] == 'completed'
    usage = await many(e.db.conn, 'SELECT local_date,SUM(tokens) tokens,COUNT(*) calls FROM rec_usage GROUP BY local_date ORDER BY local_date')
    assert usage == [{'local_date': '2026-10-11', 'tokens': 110, 'calls': 1}, {'local_date': '2026-10-12', 'tokens': 110, 'calls': 1}]
    settings, _ = await e.rec.settings_row(OWNER)
    assert (await e.rec.budget_left(OWNER, settings))['spentTokens'] == 110
    # Today's other run shares today's bucket, not this run's initial-day bucket.
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('预算边界')]), context_window=128000)
    second = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    assert (await one(e.db.conn, 'SELECT status FROM rec_runs WHERE run_id=?', (second['id'],)))['status'] == 'budget_exhausted'
    assert (await e.rec.budget_left(OWNER, settings))['spentTokens'] == 220
    cookie = {'openbear_web_session': await _login_cookie(e)}
    conv = await one(e.db.conn, 'SELECT conversation_uuid FROM rec_runs WHERE run_id=?', (run['id'],))
    assert (await e.client.delete('/api/conversations/' + conv['conversation_uuid'], cookies=cookie)).status == 200
    assert (await e.rec.budget_left(OWNER, settings))['spentTokens'] == 220
    with pytest.raises(RecError):
        await e.rec.start_run(OWNER, 'daily')


@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_or_cancelled_physical_request_keeps_reported_usage(rec_env, cancel):
    e = rec_env
    await configure(e, models=['openai/gpt', 'openai/cheap'])
    gate = asyncio.Event()
    class Failing(FakeStreamBackend):
        async def stream(self, messages, **kw):
            self.calls += 1
            if cancel:
                yield StreamEvent(kind='usage', usage=Usage(input_tokens=100, output_tokens=3))
                gate.set()
                await asyncio.Event().wait()
            else:
                error = OpenBearLLMError('insufficient_quota', reason='billing', status=402, retryable=False)
                error.usage = Usage(input_tokens=100, output_tokens=3)
                error.usage_reported = True
                raise error
    first, second = Failing([]), FakeStreamBackend([text('备用完成')])
    class Factory(FakeRunFactory):
        def backend_for(self, label):
            return (first, 'gpt', 8192) if label == 'openai/gpt' else (second, 'cheap', 8192)
    e.server.llm_factory = Factory(first, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    if cancel:
        await asyncio.wait_for(gate.wait(), 3)
        await e.rec.stop(OWNER, run['id'])
    await wait_runs(e)
    calls = await many(e.db.conn, 'SELECT model,status,input_tokens,output_tokens,cost_usd FROM model_calls ORDER BY id')
    ledger = await many(e.db.conn, 'SELECT * FROM rec_usage')
    assert len(calls) == len(ledger) == (1 if cancel else 2)
    assert calls[0]['model'] == 'openai/gpt' and calls[0]['status'] == ('cancelled' if cancel else 'error')
    assert calls[0]['input_tokens'] == 100 and calls[0]['output_tokens'] == 3
    assert calls[0]['cost_usd'] == pytest.approx(.000106)
    assert sum(c['cost_usd'] for c in calls) == pytest.approx(sum(r['cost_usd'] for r in ledger))


async def test_budget_stops_before_a_fallback_can_spend_again(rec_env):
    e = rec_env
    await configure(e, models=['openai/gpt', 'openai/cheap'], dailyBudgetTokens=90)
    first = FakeStreamBackend([[StreamEvent(kind='usage', usage=Usage(input_tokens=100)), StreamEvent(kind='error', error='insufficient_quota', status=402)]])
    second = FakeStreamBackend([text('不得调用')])
    class Factory(FakeRunFactory):
        def backend_for(self, label):
            return (first, 'gpt', 8192) if label == 'openai/gpt' else (second, 'cheap', 8192)
    e.server.llm_factory = Factory(first, context_window=128000)
    await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    assert first.calls == 1 and second.calls == 0
    assert len(await many(e.db.conn, 'SELECT * FROM rec_usage')) == 1


@pytest.mark.parametrize('missing', ['some', 'all'])
async def test_legacy_migration_is_conservative_and_idempotent_after_deletion(rec_env, missing):
    e = rec_env
    await configure(e)
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([step(call('Recommendation', {'action': 'status'})), text('完成')]), context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    old = await one(e.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run['id'],))
    # Simulate the previous schema's durable totals with one missing detail, and
    # a surviving call on the next actual local date. No production DB involved.
    actual = int(datetime(2026, 10, 12, 2, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
    async with e.db.write_transaction() as tx:
        await tx.execute('DELETE FROM rec_usage')
        await tx.execute('DELETE FROM rec_usage_migrations')
        await tx.execute('DELETE FROM model_calls' if missing == 'all' else 'DELETE FROM model_calls WHERE id=(SELECT MIN(id) FROM model_calls)')
        # A legacy stream may settle the following day; use its request start.
        await tx.execute("UPDATE runtime_actions SET created_at=? WHERE kind='model'", (actual,))
        await tx.execute('UPDATE model_calls SET created_at=?', (actual + 86400,))
        await tx.execute("UPDATE rec_runs SET local_date='2026-10-11' WHERE run_id=?", (run['id'],))
    await e.db.close()
    await e.db.connect()  # Real migration path, not just the helper.
    rows = await many(e.db.conn, 'SELECT * FROM rec_usage ORDER BY local_date')
    assert len(rows) == (1 if missing == 'all' else 2)
    assert [r['local_date'] for r in rows] == (['2026-10-11'] if missing == 'all' else ['2026-10-11', '2026-10-12'])
    assert sum(r['cost_usd'] for r in rows) == pytest.approx(old['cost_usd'])
    assert sum(r['tokens'] for r in rows) == old['tokens'] == 220
    cookie = {'openbear_web_session': await _login_cookie(e)}
    assert (await e.client.delete('/api/conversations/' + old['conversation_uuid'], cookies=cookie)).status == 200
    await e.db.close()
    await e.db.connect()
    assert await many(e.db.conn, 'SELECT * FROM rec_usage ORDER BY local_date') == rows


async def test_preflight_crossing_midnight_stamps_transport_day(rec_env, monkeypatch):
    e = rec_env
    await configure(e, timezone='America/Los_Angeles')
    before = int(datetime(2026, 10, 11, 23, 59, 59, tzinfo=ZoneInfo('America/Los_Angeles')).timestamp()*1000)
    clock = [before]
    e.rec.clock = lambda: clock[0]
    from app.web_console.live_stream import _WebStreamRenderer
    original = _WebStreamRenderer.on_model_start
    async def start(renderer, **kw):
        clock[0] = before + 2000
        return await original(renderer, **kw)
    monkeypatch.setattr(_WebStreamRenderer, 'on_model_start', start)
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')]), context_window=128000)
    await e.rec.start_run(OWNER, 'daily')
    await wait_runs(e)
    assert await many(e.db.conn, 'SELECT local_date,tokens FROM rec_usage') == [{'local_date':'2026-10-12','tokens':110}]
