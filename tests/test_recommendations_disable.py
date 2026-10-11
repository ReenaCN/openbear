"""Disabling automatic recommendations is serialized with creating their run rows."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.recommendations import follows, store
from app.recommendations.contracts import RecError
from app.webhooks.repository import many, one
from tests.test_recommendations import (  # noqa: F401
    OWNER, base_env, call, card_args, configure, human_conversation, rec_env, step, text, wait_runs,
)
from tests.test_web_admin import FakeRunFactory, FakeStreamBackend, _login_cookie


async def disable(e, saved, cookie):
    response = await e.client.put('/api/recommendations/settings', cookies=cookie,
                                  json={'settings': {**saved['settings'], 'enabled': False},
                                        'expectedRevision': saved['revision']})
    assert response.status == 200, await response.text()
    result = await response.json()
    assert result['settings'] == {**saved['settings'], 'enabled': False}
    return result


@pytest.mark.parametrize('kind,trigger', [('daily', 'scheduled'), ('follow', 'conversation')])
async def test_disabled_automatic_entry_refuses_but_manual_run_still_works(rec_env, kind, trigger):
    e = rec_env
    saved = await configure(e)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    await disable(e, saved, cookie)
    backend = FakeStreamBackend([text('手动运行仍可用')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    with pytest.raises(RecError) as error:
        await e.rec.start_run(OWNER, kind, trigger=trigger)
    assert error.value.code == 'disabled'
    assert not await many(e.db.conn, 'SELECT * FROM rec_runs')
    assert backend.calls == 0
    run = await e.rec.start_run(OWNER, kind)  # Existing explicit/manual semantics are unchanged.
    await wait_runs(e)
    row = await one(e.db.conn, 'SELECT status,trigger FROM rec_runs WHERE run_id=?', (run['id'],))
    assert row == {'status': 'completed', 'trigger': 'manual'} and backend.calls == 1


@pytest.mark.parametrize('kind', ['daily', 'follow'])
async def test_disable_during_scheduler_wait_prevents_new_automatic_run(rec_env, monkeypatch, kind):
    e = rec_env
    saved = await configure(e, dailyTime='00:00', followMinNewUserMessages=1, followIdleMinutes=0)
    await human_conversation(e, ['需要整理的新对话'], start_ms=e.rec.clock() - 1000)
    if kind == 'follow':
        settings, _ = await e.rec.settings_row(OWNER)
        await e.rec.record_skip(OWNER, settings, 'daily', e.rec.clock())
    before = await many(e.db.conn, 'SELECT * FROM rec_runs')
    # Existing output must survive disabling, even when a scheduler cycle is in flight.
    async with e.db.write_transaction() as conn:
        await store.card_save(conn, OWNER, {'run_id': 'previous-run'}, card_args()['params'], e.rec.clock())
    cards = await many(e.db.conn, 'SELECT * FROM rec_cards')
    backend = FakeStreamBackend([text('不应调用')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_check(*args, **kwargs):
        entered.set()
        await release.wait()
        return 0

    monkeypatch.setattr(follows, 'check_due', delayed_check)
    scheduler = asyncio.create_task(e.rec.tick())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await disable(e, saved, cookie)
        release.set()
        await asyncio.wait_for(scheduler, 3)
        await wait_runs(e)
        assert await many(e.db.conn, 'SELECT * FROM rec_runs') == before
        assert await many(e.db.conn, 'SELECT * FROM rec_cards') == cards
        assert not await many(e.db.conn, 'SELECT * FROM rec_cursors')
        assert backend.calls == 0 and not e.rec.tasks
    finally:
        release.set()
        await asyncio.gather(scheduler, return_exceptions=True)


@pytest.mark.parametrize('kind,trigger', [('daily', 'scheduled'), ('follow', 'conversation')])
async def test_automatic_start_waiting_for_disable_transaction_reads_committed_setting(rec_env, monkeypatch, kind, trigger):
    """The entry read was enabled; the decisive read must happen on the locked writer."""
    e = rec_env
    saved = await configure(e)
    conv = await human_conversation(e, ['尚未跟随的消息'])
    cookie = {'openbear_web_session': await _login_cookie(e)}
    backend = FakeStreamBackend([text('不应调用')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    original = e.db.write_transaction
    at_run, queue_run, waiting_run = asyncio.Event(), asyncio.Event(), asyncio.Event()
    settings_written, commit_settings = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def controlled_transaction(*args, **kwargs):
        label = kwargs.get('label')
        if label == 'recommendation-run':
            at_run.set()  # All preliminary checks and ensure_folder have already finished.
            await queue_run.wait()
            waiting_run.set()
        async with original(*args, **kwargs) as conn:
            yield conn
            if label == 'recommendation-settings':
                settings_written.set()  # Real UPDATE ran; keep its real writer transaction open.
                await commit_settings.wait()

    monkeypatch.setattr(e.db, 'write_transaction', controlled_transaction)
    starting = asyncio.create_task(e.rec.start_run(OWNER, kind, trigger=trigger,
                                                   source_conversation=conv['conversation_uuid'] if kind == 'follow' else ''))
    disabling = None
    try:
        await asyncio.wait_for(at_run.wait(), 3)
        disabling = asyncio.create_task(disable(e, saved, cookie))
        await asyncio.wait_for(settings_written.wait(), 3)
        queue_run.set()
        await asyncio.wait_for(waiting_run.wait(), 3)
        assert not starting.done() and not disabling.done()
        commit_settings.set()
        await asyncio.wait_for(disabling, 3)
        with pytest.raises(RecError) as error:
            await asyncio.wait_for(starting, 3)
        assert error.value.code == 'disabled'
        assert not await many(e.db.conn, 'SELECT * FROM rec_runs')
        assert not await many(e.db.conn, 'SELECT * FROM rec_cursors')
        assert backend.calls == 0 and not e.rec.tasks
    finally:
        queue_run.set()
        commit_settings.set()
        await asyncio.gather(starting, *([disabling] if disabling else []), return_exceptions=True)


async def test_run_transaction_winning_race_commits_before_disable_can_succeed(rec_env, monkeypatch):
    """The other serialization order retains the already-created run, not a late new run."""
    e = rec_env
    saved = await configure(e)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    e.server.llm_factory = FakeRunFactory(FakeStreamBackend([text('完成')]), context_window=128000)
    original = e.db.write_transaction
    run_written, commit_run, settings_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    commits = []

    @asynccontextmanager
    async def controlled_transaction(*args, **kwargs):
        label = kwargs.get('label')
        if label == 'recommendation-settings':
            settings_waiting.set()
        async with original(*args, **kwargs) as conn:
            yield conn
            if label == 'recommendation-run':
                assert (await one(conn, 'SELECT COUNT(*) n FROM rec_runs'))['n'] == 1
                run_written.set()
                await commit_run.wait()
        if label in ('recommendation-run', 'recommendation-settings'):
            commits.append(label)

    monkeypatch.setattr(e.db, 'write_transaction', controlled_transaction)
    starting = asyncio.create_task(e.rec.start_run(OWNER, 'daily', trigger='scheduled'))
    disabling = None
    try:
        await asyncio.wait_for(run_written.wait(), 3)
        disabling = asyncio.create_task(disable(e, saved, cookie))
        await asyncio.wait_for(settings_waiting.wait(), 3)
        assert not disabling.done()
        commit_run.set()
        run = await asyncio.wait_for(starting, 3)
        await asyncio.wait_for(disabling, 3)
        assert commits == ['recommendation-run', 'recommendation-settings']
        assert (await one(e.db.conn, 'SELECT run_id FROM rec_runs'))['run_id'] == run['id']
        await wait_runs(e)
    finally:
        commit_run.set()
        await asyncio.gather(starting, *([disabling] if disabling else []), return_exceptions=True)


async def test_disabling_keeps_active_run_next_model_call_and_output(rec_env):
    e = rec_env
    saved = await configure(e)
    cookie = {'openbear_web_session': await _login_cookie(e)}
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingBackend(FakeStreamBackend):
        async def stream(self, messages, **kwargs):
            if self.calls == 0:
                entered.set()
                await release.wait()
            async for event in super().stream(messages, **kwargs):
                yield event

    backend = BlockingBackend([step(call('Recommendation', card_args())), text('关闭后已有任务仍正常完成')])
    e.server.llm_factory = FakeRunFactory(backend, context_window=128000)
    run = await e.rec.start_run(OWNER, 'daily', trigger='scheduled')
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await disable(e, saved, cookie)
        release.set()
        await wait_runs(e)
        row = await one(e.db.conn, 'SELECT status,model_calls,cards FROM rec_runs WHERE run_id=?', (run['id'],))
        assert row == {'status': 'completed', 'model_calls': 2, 'cards': 1}
        assert backend.calls == 2
        assert (await store.card_list(e.db.conn, OWNER))['total'] == 1
        await e.rec.tick()
        assert (await one(e.db.conn, 'SELECT COUNT(*) n FROM rec_runs'))['n'] == 1
    finally:
        release.set()
