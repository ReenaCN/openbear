"""Default configuration stays usable without adding new keys to user files."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.admin.settings import parse_setting_value, safe_settings_payload
from app.agent.loop import Agent
from app.agent.progress_watch import (
    DEFAULT_AGENT_PROMPT,
    DEFAULT_CONTROLLER_PROMPT,
    NOTICE_TAG,
    ProgressWatch,
)
from app.config_store import ConfigStore
from app.llm.events import StreamEvent
from app.settings.specs import SPECS
from tests.test_agent_loop import FakeBackend
from tests.test_progress_reminder import Clock, NoticeRenderer, _read_registry, _read_round

ROOT = Path(__file__).resolve().parents[1]


def _raw_config(minimal=False):
    raw = json.loads((ROOT / "openbear.json.example").read_text())
    if minimal:
        raw = {key: raw[key] for key in ("telegram", "models")}
        raw["memory"] = {}
    return raw


def _store(tmp_path, raw):
    path = tmp_path / "openbear.json"
    path.write_text(json.dumps(raw, ensure_ascii=False))
    return ConfigStore(path)


@pytest.mark.parametrize("minimal", [False, True], ids=["public-example", "required-fields-only"])
async def test_defaults_load_without_materializing_new_keys(tmp_path, minimal):
    store = _store(tmp_path, _raw_config(minimal))
    original = store.path.read_bytes()
    config = await store.load_config()

    assert not config.validate_for_startup()
    reminder = config.agent.progress_reminder
    assert reminder.enabled is True and reminder.minutes == 15
    assert reminder.controller_prompt == DEFAULT_CONTROLLER_PROMPT
    assert reminder.agent_prompt == DEFAULT_AGENT_PROMPT
    assert config.memory.provider == "builtin"
    assert not config.mcp.enabled and not config.browser.enabled
    assert store.path.read_bytes() == original and store.revision == 0

    payload = safe_settings_payload(config.model_dump(by_alias=True))
    assert payload["values"]["agent.progressReminder.enabled"] is True
    assert payload["values"]["agent.progressReminder.minutes"] == 15
    assert payload["values"]["agent.progressReminder.controllerPrompt"] == DEFAULT_CONTROLLER_PROMPT
    assert payload["values"]["agent.progressReminder.agentPrompt"] == DEFAULT_AGENT_PROMPT
    for path in SPECS:
        parse_setting_value(path, payload["values"][path])


async def test_saving_one_new_default_does_not_freeze_other_defaults(tmp_path):
    original = _raw_config(minimal=True)
    store = _store(tmp_path, original)
    config = await store.update_path("agent.progressReminder.minutes", 30)
    expected = copy.deepcopy(original)
    expected["agent"] = {"progressReminder": {"minutes": 30}}
    assert json.loads(store.path.read_text()) == expected
    assert config.agent.progress_reminder.enabled is True
    assert config.agent.progress_reminder.controller_prompt == DEFAULT_CONTROLLER_PROMPT
    assert config.agent.progress_reminder.agent_prompt == DEFAULT_AGENT_PROMPT

    await store.update_path("ui.showTurnStats", False)
    expected["ui"] = {"showTurnStats": False}
    assert json.loads(store.path.read_text()) == expected
    reloaded = await store.load_config()
    assert reloaded.agent.progress_reminder.minutes == 30
    assert not reloaded.ui.show_turn_stats


async def test_explicit_false_and_custom_values_are_not_replaced_by_defaults(tmp_path):
    raw = _raw_config(minimal=True)
    raw["agent"] = {"progressReminder": {
        "enabled": False, "minutes": 2,
        "controllerPrompt": "主控 [[ loop.count ]]", "agentPrompt": "子任务 [[ loop.count ]]",
    }}
    store = _store(tmp_path, raw)
    original = store.path.read_bytes()
    config = await store.load_config()
    reminder = config.agent.progress_reminder
    assert reminder.enabled is False and reminder.minutes == 2
    assert reminder.controller_prompt == "主控 [[ loop.count ]]"
    assert reminder.agent_prompt == "子任务 [[ loop.count ]]"
    assert store.path.read_bytes() == original
    clock = Clock()
    for agent in (False, True):
        watch = ProgressWatch.from_config(config.agent, agent=agent, clock=clock)
        clock.now += 3600
        assert watch.take_notice() == ""


@pytest.mark.parametrize("empty_prompts", [False, True], ids=["omitted", "explicit-empty"])
async def test_default_reminder_loaded_from_file_runs_without_user_templates(tmp_path, empty_prompts):
    raw = _raw_config(minimal=True)
    if empty_prompts:
        raw["agent"] = {"progressReminder": {"controllerPrompt": "", "agentPrompt": ""}}
    store = _store(tmp_path, raw)
    original = store.path.read_bytes()
    config = await store.load_config()
    clock = Clock()
    watch = ProgressWatch.from_config(config.agent, clock=clock)

    class Backend(FakeBackend):
        async def stream(self, messages, **kwargs):
            clock.now += 6 * 60
            async for event in super().stream(messages, **kwargs):
                yield event

    final = [StreamEvent(kind="content", text="完成"), StreamEvent(kind="finish", finish_reason="stop")]
    backend = Backend([_read_round(index) for index in range(4)] + [final])
    renderer = NoticeRenderer()
    result = await Agent(backend, _read_registry(), progress_watch=watch).run(
        [{"role": "user", "content": "读取材料"}], renderer, model="fixture")
    assert result.text == "完成" and not result.halted_reason
    assert len(renderer.runtime_notices) == 1
    notice = backend.seen_convos[3][-1]["content"]
    assert NOTICE_TAG in notice and "请用户介入" in notice
    assert {options.get("think_level") for options in backend.seen_opts} == {"off"}

    child = ProgressWatch.from_config(config.agent, agent=True, clock=clock)
    clock.now += 900
    assert "报告主控" in child.take_notice()
    assert store.path.read_bytes() == original
