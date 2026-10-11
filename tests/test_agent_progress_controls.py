"""Every actually consumed Agent control resets the progress reminder clock."""
from __future__ import annotations

import copy
import json

import pytest

from app.agent.progress_watch import NOTICE_TAG, ProgressWatch
from app.agents.control import AgentControlService
from app.agents.execution import AgentExecutor
from app.agents.plan import AgentPlanCoordinator, register_agent_plan_tools
from app.llm.base import AgentResult
from app.llm.events import ToolCall
from app.tools.base import ToolRegistry
from tests.test_rath_single_agent import env  # noqa: F401


@pytest.mark.parametrize("boundary", [
    "before_model", "after_tool_call", "after_model", "after_final_compression", "window_pre_model",
])
async def test_consumed_control_resets_clock_at_every_model_boundary(env, monkeypatch, boundary):  # noqa: F811
    dao, task_uuid, agent = env
    clock = [1000.0]
    watch = ProgressWatch(enabled=True, threshold_s=900, clock=lambda: clock[0])
    seen, idle_at_request = [], []
    control_uuid = ""

    class Backend:
        protocol = "chat"

        async def complete(self, messages, **kwargs):
            seen.append(copy.deepcopy(messages))
            idle_at_request.append(watch.idle_seconds())
            if len(seen) == 1:
                if boundary in {"before_model", "after_tool_call", "window_pre_model"}:
                    return AgentResult(tool_calls=[ToolCall("read", "Read", "{}")])
                return AgentResult(text="initial answer")
            if len(seen) == 2:
                assert control_uuid
                return AgentResult(tool_calls=[ToolCall("ack", "AgentControlAck", json.dumps({
                    "controlUuid": control_uuid, "status": "accepted", "reason": "New scope received",
                }))])
            return AgentResult(text="done")

    tools = ToolRegistry()
    register_agent_plan_tools(tools, AgentPlanCoordinator(dao, AgentControlService(dao)))

    async def read(args):
        return "read result"

    tools.add("Read", "fake", {"type": "object"}, read, visibility={"agent"})
    runner = AgentExecutor(dao, task_uuid, agent=agent, backend=Backend(), model="gpt", max_tokens=1024,
                           tools=tools, plan_protocol_enabled=False, progress_watch=watch, max_retries=0)
    checkpoint = runner.checkpoint

    async def receive_control(stage, **kwargs):
        nonlocal control_uuid
        if stage == boundary and seen and not control_uuid:
            # Advance only at the chosen safe boundary, then deliver a real
            # durable control. No arbitrary sleep, mocked consume or model ack.
            clock[0] += 901
            control_uuid = await AgentControlService(dao).steer(
                task_uuid, "new scope just arrived", requested_by="main-controller")
        return await checkpoint(stage, **kwargs)

    monkeypatch.setattr(runner, "checkpoint", receive_control)
    output = await runner.run()
    assert output["summary"] == "done"
    assert len(seen) == 3
    assert idle_at_request[1] == 0
    assert "new scope just arrived" in str(seen[1])
    assert NOTICE_TAG not in str(seen[1])
    assert watch.count == 0
    events = await dao.events(task_uuid, limit=200)
    assert "steer_applied" in [event.kind for event in events]
    assert "control_response" in [event.kind for event in events]
    assert "progress_reminder" not in [event.kind for event in events]


async def test_empty_control_checks_do_not_reset_read_or_retry_clock(env):  # noqa: F811
    dao, task_uuid, agent = env
    clock = [1000.0]
    watch = ProgressWatch(enabled=True, threshold_s=900, clock=lambda: clock[0])
    runner = AgentExecutor(dao, task_uuid, agent=agent, backend=None, model="gpt", max_tokens=1024,
                           tools=ToolRegistry(), plan_protocol_enabled=False, progress_watch=watch)
    messages = []
    clock[0] += 300
    watch.on_tool("Read", "{}")
    clock[0] += 1200
    watch.on_tool_finished("Read", "{}")
    assert not runner._append_pending_steers(messages)
    assert watch.idle_seconds() == 300
    watch.on_retry_wait(True)
    clock[0] += 1200
    assert not runner._append_pending_steers(messages)
    watch.on_retry_wait(False)
    assert watch.idle_seconds() == 300
    runner.steers.append({"message": "   "})
    assert not runner._append_pending_steers(messages)
    assert watch.idle_seconds() == 300
    clock[0] += 600
    assert watch.take_notice()
