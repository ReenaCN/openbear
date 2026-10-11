"""无推进提醒：计时、清零、扣除重试等待、模板校验，以及主循环的注入位置。"""
from __future__ import annotations

import pytest

from app.agent.loop import Agent
from app.agent.progress_watch import (
    DEFAULT_AGENT_PROMPT,
    DEFAULT_CONTROLLER_PROMPT,
    NOTICE_TAG,
    ProgressWatch,
    is_progress_tool,
    render_notice,
    validate_notice_template,
)
from app.admin.settings import parse_setting_value, preview_prompt_setting
from app.config import AgentConfig
from app.llm.events import StreamEvent, ToolCall
from app.tools.base import ToolRegistry
from app.web_console.live_stream import _WebLiveStream, _WebStreamRenderer
from app.web_operations import web_event_operation_specs
from tests.test_agent_loop import FakeBackend, RecordRenderer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _watch(clock: Clock, minutes: float = 15) -> ProgressWatch:
    return ProgressWatch(enabled=True, threshold_s=minutes * 60, clock=clock)


def test_reads_accumulate_until_threshold_then_reset():
    clock = Clock()
    watch = _watch(clock)
    for _ in range(5):
        watch.on_model_step()
        watch.on_tool("Read", '{"path": "a"}')
        watch.on_tool_finished("Read", '{"path": "a"}')
        watch.on_tool("Bash", '{"command": "grep x"}')
        watch.on_tool_finished("Bash", '{"command": "grep x"}')
    clock.now += 14 * 60
    assert watch.take_notice() == ""
    clock.now += 61
    notice = watch.take_notice()
    assert notice.startswith(f'<{NOTICE_TAG} internal="true">')
    assert "15 分钟" in notice and "读取、查询了 10 次" in notice and "思考了 5 步" in notice
    assert "本轮第 1 次提醒" in notice
    # 提醒后重新计时，不连续重复
    assert watch.take_notice() == ""
    clock.now += 15 * 60
    assert "本轮第 2 次提醒" in watch.take_notice()


def test_progress_and_user_input_reset_the_timer():
    clock = Clock()
    watch = _watch(clock)
    clock.now += 14 * 60
    watch.on_tool("Edit", '{"path": "a"}')
    clock.now += 10 * 60
    assert watch.take_notice() == ""
    clock.now += 10 * 60
    watch.on_user_input()
    clock.now += 14 * 60
    assert watch.take_notice() == ""


def test_bash_counts_as_progress_only_after_a_file_change():
    assert not is_progress_tool("Bash", "{}", wrote=False)
    assert is_progress_tool("Bash", "{}", wrote=True)
    clock = Clock()
    watch = _watch(clock)
    watch.on_tool("Bash", "{}")
    assert watch.reads == 1
    watch.on_tool("Write", '{"path": "a"}')
    watch.on_tool("Bash", '{"command": "pytest"}')
    assert watch.reads == 0


def test_bash_long_after_last_file_change_counts_as_reading():
    clock = Clock()
    watch = _watch(clock)
    watch.on_tool("Write", '{"path": "a"}')
    watch.on_tool_finished("Write", '{"path": "a"}')
    clock.now += 14 * 60
    watch.on_tool("Bash", '{"command": "pytest"}')
    watch.on_tool_finished("Bash", '{"command": "pytest"}')
    assert watch.reads == 0
    reminded = 0
    for _ in range(20):  # 之后 40 分钟只用 Bash 查询，没再改文件
        clock.now += 120
        watch.on_model_step()
        watch.on_tool("Bash", '{"command": "grep x"}')
        watch.on_tool_finished("Bash", '{"command": "grep x"}')
        reminded += bool(watch.take_notice())
    assert reminded == 2


def test_tool_classification():
    assert is_progress_tool("AgentWait", "{}", wrote=False)
    assert is_progress_tool("UserInteraction", "{}", wrote=False)
    assert is_progress_tool("OpenBearControl", "{}", wrote=False)
    assert not is_progress_tool("Read", "{}", wrote=True)
    assert not is_progress_tool("History", "{}", wrote=True)
    assert not is_progress_tool("mcp__parrot__web_search", "{}", wrote=False)
    assert not is_progress_tool("Browser", '{"action": "snapshot"}', wrote=False)
    assert is_progress_tool("Browser", '{"action": "act"}', wrote=False)
    assert not is_progress_tool("Recommendation", '{"action": "source.next"}', wrote=False)
    assert is_progress_tool("Recommendation", '{"action": "card.save"}', wrote=False)


def test_long_progress_tool_does_not_count_as_idle():
    clock = Clock()
    watch = _watch(clock)
    watch.on_tool("AgentWait", "{}")
    clock.now += 40 * 60  # 等 Agent 40 分钟
    watch.on_tool_finished("AgentWait", "{}")
    clock.now += 5 * 60
    assert watch.take_notice() == ""


def test_retry_wait_is_not_counted():
    clock = Clock()
    watch = _watch(clock)
    clock.now += 5 * 60
    watch.on_retry_wait(True)
    clock.now += 14 * 60  # 上游故障，等待重试
    assert watch.take_notice() == ""
    watch.on_retry_wait(False)
    clock.now += 9 * 60
    assert watch.take_notice() == ""
    clock.now += 61
    assert "15 分钟" in watch.take_notice()


def test_read_tool_execution_time_is_not_counted():
    clock = Clock()
    watch = _watch(clock)
    clock.now += 5 * 60
    watch.on_tool("Bash", '{"command": "scp big.tar remote:"}')
    clock.now += 30 * 60  # 远程传输跑了 30 分钟
    assert watch.take_notice() == ""
    watch.on_tool_finished("Bash", '{"command": "scp big.tar remote:"}')
    watch.on_model_step()
    clock.now += 9 * 60
    assert watch.take_notice() == ""
    clock.now += 61
    assert "15 分钟" in watch.take_notice()


def test_tool_pause_closes_at_next_model_step_without_finish_callback():
    clock = Clock()
    watch = _watch(clock)
    watch.on_tool("Read", '{"path": "a"}')
    clock.now += 20 * 60
    watch.on_model_step()
    clock.now += 14 * 60
    assert watch.take_notice() == ""
    clock.now += 61
    assert watch.take_notice()


def test_disabled_never_reminds():
    clock = Clock()
    watch = ProgressWatch(enabled=False, threshold_s=60, clock=clock)
    clock.now += 3600
    assert watch.take_notice() == ""


def test_agent_template_reports_to_controller():
    assert "报告主控" in DEFAULT_AGENT_PROMPT
    assert "请用户介入" in DEFAULT_CONTROLLER_PROMPT
    watch = ProgressWatch.from_config(AgentConfig(), agent=True)
    assert watch.enabled and watch.threshold_s == 900
    assert watch.default_template == DEFAULT_AGENT_PROMPT


def test_template_validation_and_custom_render():
    validate_notice_template("")
    validate_notice_template(DEFAULT_CONTROLLER_PROMPT)
    validate_notice_template("@if loop.count > 1\n第 [[ loop.count ]] 次\n@else\n首次\n@endif")
    for bad in ("[[ time.now ]]", "[[ loop.nope ]]", "[[ loop.__class__ ]]", "[[ open(1) ]]",
                "@if loop.count", "[[ loop.minutes ", "@each x in loop"):
        with pytest.raises(ValueError):
            validate_notice_template(bad)
    text = render_notice("已空转 [[ loop.minutes ]] 分钟", DEFAULT_CONTROLLER_PROMPT,
                         {"minutes": 20, "reads": 1, "thinkSteps": 1, "count": 1})
    assert "已空转 20 分钟" in text and text.endswith(f"</{NOTICE_TAG}>")


def test_bad_saved_template_falls_back_to_builtin():
    clock = Clock()
    watch = _watch(clock)
    watch.template = "[[ loop.minutes / 0 ]]"
    clock.now += 16 * 60
    assert "运行时提醒" in watch.take_notice()


def test_settings_validation_and_preview():
    assert parse_setting_value("agent.progressReminder.minutes", "15") == 15.0
    with pytest.raises(ValueError):
        parse_setting_value("agent.progressReminder.minutes", "0")
    with pytest.raises(ValueError):
        parse_setting_value("agent.progressReminder.controllerPrompt", "[[ time.now ]]")
    assert "运行时提醒" in preview_prompt_setting("agent.progressReminder.controllerPrompt", "")
    assert "报告主控" in preview_prompt_setting("agent.progressReminder.agentPrompt", "")
    with pytest.raises(ValueError):
        AgentConfig.model_validate({"progressReminder": {"controllerPrompt": "[[ x ]]"}})


def _read_registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def _read(args):
        return "content"

    async def _write(args):
        return "ok"

    reg.add("Read", "read", {"type": "object", "properties": {"path": {"type": "string"}}}, _read)
    reg.add("Write", "write", {"type": "object", "properties": {"path": {"type": "string"}}}, _write)
    return reg


class NoticeRenderer(RecordRenderer):
    def __init__(self) -> None:
        super().__init__()
        self.runtime_notices: list[str] = []

    async def on_runtime_notice(self, note: str) -> None:
        self.runtime_notices.append(note)


def _read_round(i: int) -> list[StreamEvent]:
    return [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id=f"r{i}", name="Read", arguments=f'{{"path": "f{i}"}}')]),
            StreamEvent(kind="finish", finish_reason="tool_calls")]


async def test_web_runtime_notice_preserves_visibility_marker_without_ending_run():
    events = []

    async def sink(event):
        events.append(event)
        return event

    live = _WebLiveStream("progress-reminder", -1, event_sink=sink)
    live.status = "running"
    live.current_turn_uuid = "turn-1"
    renderer = _WebStreamRenderer(live)
    text = "已 15 分钟以上没有推进，已提醒模型重新审视状态（第 1 次）"
    await renderer.on_runtime_notice(text)
    await renderer.on_runtime_notice("第二次运行时提醒")

    notices = [event for event in events if event["type"] == "notice"]
    assert len(notices) == 2
    assert notices[0]["eventUuid"] != notices[1]["eventUuid"]
    for event in notices:
        spec, = web_event_operation_specs(event)
        assert spec["op_type"] == "notice"
        assert spec["lifecycle"] == "informational"
        assert spec["turn_uuid"] == "turn-1"
        assert spec["payload"]["runtimeNotice"] is True
        assert spec["payload"]["text"] == event["text"]
    assert live.status == "running"
    await renderer.on_status("正在思考 …")
    assert live.status == "running"
    await renderer.finalize_notice("（已停止）")
    spec, = web_event_operation_specs(events[-1])
    assert "runtimeNotice" not in spec["payload"]


async def test_loop_appends_notice_before_next_request_without_interrupting():
    clock = Clock()
    watch = ProgressWatch(enabled=True, threshold_s=15 * 60, clock=clock)

    class TickingBackend(FakeBackend):
        async def stream(self, messages, **kw):
            clock.now += 6 * 60  # 每一步思考加读取约 6 分钟
            async for ev in super().stream(messages, **kw):
                yield ev

    final = [StreamEvent(kind="content", text="完成"), StreamEvent(kind="finish", finish_reason="stop")]
    backend = TickingBackend([_read_round(i) for i in range(4)] + [final])
    renderer = NoticeRenderer()
    r = await Agent(backend, _read_registry(), progress_watch=watch).run(
        [{"role": "user", "content": "查一下"}], renderer, model="m")

    assert r.text == "完成" and not r.halted_reason
    reminded = [i for i, convo in enumerate(backend.seen_convos)
                if any(NOTICE_TAG in str(m.get("content")) for m in convo if m.get("role") == "user")]
    # 第 3 次请求前已过 18 分钟：从那次请求开始带上提醒，且提醒追加在末尾
    assert reminded and reminded[0] == 3
    last = backend.seen_convos[3][-1]
    assert last["role"] == "user" and NOTICE_TAG in last["content"]
    assert len(renderer.runtime_notices) == 1
    # 不改思考档位
    assert {opt.get("think_level") for opt in backend.seen_opts} == {"off"}


async def test_loop_does_not_remind_when_files_are_changed():
    clock = Clock()
    watch = ProgressWatch(enabled=True, threshold_s=15 * 60, clock=clock)

    class TickingBackend(FakeBackend):
        async def stream(self, messages, **kw):
            clock.now += 6 * 60
            async for ev in super().stream(messages, **kw):
                yield ev

    write = [StreamEvent(kind="tool_call", tool_calls=[ToolCall(id="w", name="Write", arguments='{"path": "a"}')]),
             StreamEvent(kind="finish", finish_reason="tool_calls")]
    final = [StreamEvent(kind="content", text="完成"), StreamEvent(kind="finish", finish_reason="stop")]
    backend = TickingBackend([_read_round(0), _read_round(1), write, _read_round(2), _read_round(3), final])
    renderer = NoticeRenderer()
    await Agent(backend, _read_registry(), progress_watch=watch).run(
        [{"role": "user", "content": "改一下"}], renderer, model="m")
    assert not renderer.runtime_notices
    assert not any(NOTICE_TAG in str(m.get("content")) for convo in backend.seen_convos for m in convo)


async def test_child_agent_gets_agent_notice_before_next_request(tmp_path):
    import json as _json

    from app.agents.dao import AgentDAO
    from app.agents.execution import AgentExecutor
    from app.agents.profiles import ensure_builtin_workflows
    from app.agents.schemas import AgentDefinition
    from app.db.engine import DB
    from app.llm.base import AgentResult
    from app.llm.events import Usage

    db = DB(str(tmp_path / "rath.db"))
    await db.connect()
    try:
        dao = AgentDAO(db)
        workflow_uuid = await ensure_builtin_workflows(dao)
        task_uuid = await dao.create_task(chat_id=123, workflow_uuid=workflow_uuid, title="调查",
                                          input_data={"instruction": "读几个文件"})
        clock = Clock()
        watch = ProgressWatch.from_config(AgentConfig(), agent=True, clock=clock)

        class Backend:
            protocol = "chat"

            def __init__(self) -> None:
                self.calls = []

            async def complete(self, messages, **kw):
                self.calls.append([dict(m) for m in messages])
                clock.now += 6 * 60
                if len(self.calls) <= 4:
                    return AgentResult(tool_calls=[ToolCall(id=f"r{len(self.calls)}", name="Read",
                                                            arguments=_json.dumps({"path": "x"}))])
                return AgentResult(text="完成", usage=Usage(input_tokens=1, output_tokens=1))

        reg = _read_registry()
        agent = AgentDefinition(id=1, workflow_uuid=workflow_uuid, agent_key="reader", name="读者",
                                description="读", system_prompt="你是读者", model="openai/gpt",
                                think_level="off", tool_allowlist=["Read"], enabled=True)
        backend = Backend()
        output = await AgentExecutor(dao, task_uuid, agent=agent, backend=backend, model="gpt", max_tokens=1024,
                                     tools=reg, plan_protocol_enabled=False, progress_watch=watch).run()
        assert output["summary"] == "完成"
        reminded = [i for i, msgs in enumerate(backend.calls)
                    if any(NOTICE_TAG in str(m.get("content")) for m in msgs if m.get("role") == "user")]
        assert reminded and reminded[0] == 3
        assert "报告主控" in str(backend.calls[3])
        events = await dao.events(task_uuid)
        assert "progress_reminder" in [e.kind for e in events]
    finally:
        await db.close()
