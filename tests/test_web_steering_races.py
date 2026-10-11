"""Real Web send/handoff races with deterministic, task-local scheduling."""
from __future__ import annotations

import asyncio

import pytest

from app.agent import steering
from app.llm.events import StreamEvent
from tests.test_web_admin import FakeStreamBackend, web_env  # noqa: F401
from tests.test_web_steering_handoff import _setup, _wait_idle


async def test_runner_finishing_during_enqueue_still_hands_off_once(web_env, monkeypatch):  # noqa: F811
    server = web_env.server
    finish, started = asyncio.Event(), asyncio.Event()

    class Backend(FakeStreamBackend):
        async def stream(self, messages, **kwargs):
            started.set()
            await finish.wait()
            async for event in super().stream(messages, **kwargs):
                yield event

    backend = Backend([
        [StreamEvent("content", text="FIRST"), StreamEvent("finish", finish_reason="stop")],
        [StreamEvent("content", text="SECOND"), StreamEvent("finish", finish_reason="stop")],
    ])
    _setup(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="enqueue race", model="openai/gpt")
    chat, conv = int(row["internal_chat_id"]), row["conversation_uuid"]
    live = server._live_for(row)
    publish = live.publish
    old_runner = None

    async def publish_after_old_runner(event, **kwargs):
        if event.get("type") == "queued":
            # The running check succeeded, but the queued event's DB await can
            # outlast the runner. Its registry entry will already be gone.
            finish.set()
            await asyncio.wait_for(asyncio.shield(old_runner), 3)
            assert server.runs.task(chat) is None
        return await publish(event, **kwargs)

    monkeypatch.setattr(live, "publish", publish_after_old_runner)
    try:
        await server._start_or_steer_web_conversation(row, "FIRST question", [], live)
        old_runner = server.runs.task(chat)
        await asyncio.wait_for(started.wait(), 3)
        response = await server._start_or_steer_web_conversation(row, "SECOND question", [], live)
        assert response["queued"]
        await _wait_idle(server, chat)
        assert backend.calls == 2
        assert not steering.has_pending(chat)
        assert sum(str(m.get("content", "")).startswith("SECOND question")
                   for m in backend.seen_convos[1] if m["role"] == "user") == 1
        operations = await server._web_operations(conv)
        users = [o for o in operations if o["opType"] == "user_message"
                 and o["payload"].get("text") == "SECOND question"]
        answer, = [o for o in operations if o["opType"] == "assistant_message"
                   and o["payload"].get("text") == "SECOND"]
        assert len(users) == 1
        assert users[0]["turnUuid"] == answer["turnUuid"]
        assert users[0]["opId"] == f"msg:{response['pendingSteering'][0]['messageUuid']}"
    finally:
        finish.set()
        await server.stop()
        steering.clear(chat)


async def test_multiple_late_inputs_and_callbacks_are_consumed_once(web_env, monkeypatch):  # noqa: F811
    server = web_env.server
    backend = FakeStreamBackend([
        [StreamEvent("content", text="FIRST"), StreamEvent("finish", finish_reason="stop")],
        [StreamEvent("content", text="COMBINED"), StreamEvent("finish", finish_reason="stop")],
    ])
    _setup(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="multiple late inputs", model="openai/gpt")
    chat, conv = int(row["internal_chat_id"]), row["conversation_uuid"]
    live = server._live_for(row)
    touch = server._touch_web_conversation
    responses = []

    async def queue_at_last_boundary(conversation_uuid, **kwargs):
        if kwargs.get("current_status") == "就绪" and not responses:
            for text in ("late one", "late two"):
                responses.append(await server._start_or_steer_web_conversation(row, text, [], live))
        return await touch(conversation_uuid, **kwargs)

    monkeypatch.setattr(server, "_touch_web_conversation", queue_at_last_boundary)
    try:
        await server._start_or_steer_web_conversation(row, "first question", [], live)
        await _wait_idle(server, chat)
        assert all(response["queued"] for response in responses) and len(responses) == 2
        assert backend.calls == 2
        operations = await server._web_operations(conv)
        answer, = [o for o in operations if o["opType"] == "assistant_message"
                   and o["payload"].get("text") == "COMBINED"]
        for text in ("late one", "late two"):
            user, = [o for o in operations if o["opType"] == "user_message"
                     and o["payload"].get("text") == text]
            assert user["turnUuid"] == answer["turnUuid"]
            assert sum(text in str(m.get("content", "")) for m in backend.seen_convos[1]
                       if m["role"] == "user") == 1
        assert not steering.has_pending(chat)
    finally:
        await server.stop()
        steering.clear(chat)


@pytest.mark.parametrize("boundary", ["lock", "row", "user_publish"])
async def test_stop_joins_handoffs_and_rejects_late_callbacks(web_env, monkeypatch, boundary):  # noqa: F811
    server = web_env.server
    backend = FakeStreamBackend()
    _setup(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="shutdown race", model="openai/gpt")
    chat, conv = int(row["internal_chat_id"]), row["conversation_uuid"]
    entered, release = asyncio.Event(), asyncio.Event()
    original_row = server._conversation_row
    live = server._live_for(row)
    original_publish = live.publish

    async def pause():
        entered.set()
        await release.wait()

    async def paused_row(*args):
        if boundary == "row" and asyncio.current_task().get_name().startswith("web-steering-handoff-"):
            await pause()
        return await original_row(*args)

    async def paused_publish(event, **kwargs):
        if boundary == "user_publish" and event.get("type") == "user":
            await pause()
        return await original_publish(event, **kwargs)

    monkeypatch.setattr(server, "_conversation_row", paused_row)
    monkeypatch.setattr(live, "publish", paused_publish)
    steering.enqueue(chat, "late input", turnUuid="old", messageUuid="late-input", source="web")
    done = asyncio.create_task(asyncio.sleep(0, result=True))
    await done
    lock = server.operation_locks.chat(chat, "web_send")
    if boundary == "lock":
        await lock.__aenter__()
    try:
        server._schedule_steering_handoff(row, done)
        handoff, = server._web_steering_handoffs
        if boundary == "lock":
            await asyncio.sleep(0)
            assert not handoff.done()
        else:
            await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(server.stop(), 3)
        assert handoff.cancelled()
        assert not server._web_steering_handoffs
        assert not server._web_starting_turns.get(conv)
        assert server.runs.task(chat) is None
        assert [item["text"] for item in steering.pending_items(chat)] == ["late input"]
        release.set()
        # A controller completing after stop cannot add another handoff.
        server._schedule_steering_handoff(row, done)
        if boundary != "lock":
            await server._resume_stranded_steering(row)
        await asyncio.sleep(0)
        assert not server._web_steering_handoffs
        assert backend.calls == 0
    finally:
        release.set()
        if boundary == "lock":
            await lock.__aexit__(None, None, None)
        await server.stop()
        steering.clear(chat)
    response = await server._start_or_steer_web_conversation(row, "after stop", [], live)
    assert response == {"ok": False, "error": "server_stopping"}
    assert server.runs.task(chat) is None


async def test_stop_during_direct_send_preparation_cannot_register_controller(web_env, monkeypatch):  # noqa: F811
    server = web_env.server
    backend = FakeStreamBackend()
    _setup(server, monkeypatch, backend)
    row = await server._create_web_conversation(123, title="send during stop", model="openai/gpt")
    chat = int(row["internal_chat_id"])
    live = server._live_for(row)
    entered, release = asyncio.Event(), asyncio.Event()
    publish = live.publish

    async def paused_publish(event, **kwargs):
        if event.get("type") == "user":
            entered.set()
            await release.wait()
        return await publish(event, **kwargs)

    monkeypatch.setattr(live, "publish", paused_publish)
    sending = asyncio.create_task(server._start_or_steer_web_conversation(row, "question", [], live))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await server.stop()
        release.set()
        response = await asyncio.wait_for(sending, 3)
        assert response == {"ok": False, "error": "server_stopping"}
        assert server.runs.task(chat) is None
        assert backend.calls == 0
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await server.stop()
        steering.clear(chat)
