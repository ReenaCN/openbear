"""无推进提醒：长时间只在思考和读取、没有实际推进时，提醒模型重新审视状态。

只计时、只提醒：不打断模型、不改思考档位、不中止运行。
计时扣除等待模型重试和工具执行的时间，只算模型自己的时间；用户插话、修改文件、执行有效操作时重新计时。
"""
from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.memory.builtin import TemplateEngine

NOTICE_TAG = "long-thinking-notice"
VARIABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "loop": ("minutes", "reads", "thinkSteps", "count"),
}
VARIABLES = tuple(f"{root}.{name}" for root, names in VARIABLE_FIELDS.items() for name in names)

_COMMON_HEAD = (
    "运行时提醒（按计时自动发出，不是用户消息）：\n"
    "你已经 [[ loop.minutes ]] 分钟没有修改文件或执行有效操作，期间读取、查询了 [[ loop.reads ]] 次、"
    "思考了 [[ loop.thinkSteps ]] 步（本轮第 [[ loop.count ]] 次提醒）。"
    "这通常说明你陷入了 思考 -> 读取 -> 思考 -> 读取…… 的循环。\n\n"
    "用一两句话判断当前属于哪种情况，然后照做：\n"
    "1. 已经调查到尾声，即将开始下一步行动：下一步直接执行这个行动，不再查询。\n"
)
DEFAULT_CONTROLLER_PROMPT = _COMMON_HEAD + (
    "2. 陷入错误的行动中：立即停止并纠正行为，回到正确的目标上。\n"
    "3. 遇到无法解决的问题，但不影响最终目标：把它记为未确认项，回到正确的目标上。\n"
    "4. 遇到无法解决的问题，且该问题影响到最终目标的实现：停止调查，说明卡在哪、需要用户决定什么，请用户介入。\n\n"
    "这条提醒不改变任务范围和授权。"
)
DEFAULT_AGENT_PROMPT = _COMMON_HEAD + (
    "2. 陷入错误的行动中：立即停止并纠正行为，回到任务要求的结果上。\n"
    "3. 遇到无法解决的问题，但不影响最终目标：把它记为未确认项，回到任务要求的结果上。\n"
    "4. 遇到无法解决的问题，且该问题影响到最终目标的实现：停止调查，说明卡在哪、需要什么决定，报告主控，由主控决定。\n\n"
    "这条提醒不改变任务范围和授权。"
)

# 修改文件、执行有实际效果的操作、派出/管理 Agent、向用户确认：都算推进。
_WRITE_TOOLS = frozenset({"Write", "Edit", "EditBatch"})
_EFFECT_TOOLS = frozenset({
    "OpenBearControl", "Cron", "Webhook", "Release",
    "Agent", "AgentContinue", "AgentWait", "AgentMessage", "AgentStop", "AgentPlanDecision",
    "UserInteraction",
})
_READ_TOOLS = frozenset({
    "Read", "History", "AgentHistory", "Memory", "TaskMemory", "AgentInfo", "Process",
    "mcp__parrot__web_search", "mcp__parrot__web_fetch",
})
_BROWSER_READ_ACTIONS = frozenset({"status", "snapshot", "find", "capture", "console", "network", "describe", "wait", "page"})
_RECOMMENDATION_READ_ACTIONS = frozenset({
    "status", "source.next", "source.read", "profile.list", "card.list", "follow.list", "insights.get", "internal.search",
})


def _action(arguments: Any) -> str:
    if isinstance(arguments, dict):
        data = arguments
    else:
        try:
            data = json.loads(arguments or "{}")
        except (TypeError, ValueError):
            return ""
    return str(data.get("action") or "") if isinstance(data, dict) else ""


def is_progress_tool(name: str, arguments: Any, *, wrote: bool) -> bool:
    """Bash 无法区分查还是改：最近改过文件时才视为推进（构建、测试、提交）。"""
    if name in _WRITE_TOOLS or name in _EFFECT_TOOLS:
        return True
    if name == "Bash":
        return wrote
    if name == "Browser":
        return _action(arguments) not in _BROWSER_READ_ACTIONS
    if name == "Recommendation":
        return _action(arguments) not in _RECOMMENDATION_READ_ACTIONS
    if name in _READ_TOOLS:
        return False
    if name.startswith("mcp__"):
        return False
    return True  # 未知工具不计为读取，避免误报


_EXPR_RE = re.compile(r"\[\[\s*(.*?)\s*\]\]")


_ALLOWED_NAMES = frozenset({"len", "str", "int", "float", "bool"})


def _check_expr(expr: str) -> None:
    try:
        tree = ast.parse(expr.replace("||", " or ").replace("&&", " and "), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"模板表达式语法错误：{expr}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Lambda, ast.NamedExpr, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            raise ValueError("模板不支持赋值、lambda 或推导式")
        if isinstance(node, ast.Name) and node.id != "loop" and node.id not in _ALLOWED_NAMES:
            raise ValueError(f"不支持的模板变量：{node.id}，可用：{', '.join(VARIABLES)}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ValueError("模板不支持私有属性")
            if not (isinstance(node.value, ast.Name) and node.value.id == "loop" and node.attr in VARIABLE_FIELDS["loop"]):
                raise ValueError(f"不支持的模板变量：{ast.unparse(node)}，可用：{', '.join(VARIABLES)}")
        if isinstance(node, ast.Subscript):
            raise ValueError("模板变量请用 loop.名称 的写法")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_NAMES):
            raise ValueError("模板只支持 int/str/float/bool/len 转换函数")


def validate_notice_template(template: str) -> None:
    """语法同用户消息模板（[[ ]]、@if/@else/@endif、@raw）；变量只允许 loop.*。"""
    text = str(template or "")
    if not text.strip():
        return
    stack: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stack and stack[-1] == "raw":
            if stripped == "@endraw":
                stack.pop()
            continue
        if stripped == "@raw":
            stack.append("raw")
            continue
        if stripped.startswith("@if "):
            _check_expr(stripped[4:].strip())
            stack.append("if")
            continue
        if stripped == "@else":
            if not stack or stack[-1] != "if":
                raise ValueError("模板 @else 没有匹配的 @if 或重复出现")
            stack[-1] = "else"
            continue
        if stripped == "@endif":
            if not stack or stack[-1] not in {"if", "else"}:
                raise ValueError("模板 @endif 没有匹配的 @if")
            stack.pop()
            continue
        if re.match(r"^@(if|else|endif|each|endeach|raw|endraw)\b", stripped):
            raise ValueError(f"模板指令不支持或语法错误：{stripped}")
        for match in _EXPR_RE.finditer(line):
            _check_expr(match.group(1))
        remainder = _EXPR_RE.sub("", line)
        if "[[" in remainder or "]]" in remainder:
            raise ValueError("模板 [[ ]] 未配对；字面内容请放在 @raw 块中")
    if stack:
        raise ValueError("模板存在未闭合的 @if 或 @raw")


class _StrictEngine(TemplateEngine):
    def _interpolate(self, line: str, ctx: dict[str, Any]) -> str:
        def replace(match):
            value = self._eval(match.group(1), ctx)
            return str(value if value is not None else "")
        return self._expr_re.sub(replace, line)


def render_notice(template: str, default: str, variables: dict[str, Any]) -> str:
    text = str(template or "").strip() or default
    validate_notice_template(text)
    body = _StrictEngine().render(text, {"loop": dict(variables)}).strip()
    return f'<{NOTICE_TAG} internal="true">\n{body}\n</{NOTICE_TAG}>'


def sample_variables() -> dict[str, Any]:
    return {"minutes": 15, "reads": 28, "thinkSteps": 21, "count": 1}


@dataclass
class ProgressWatch:
    """一个 run 内的无推进计时。时间用 monotonic 秒。"""
    enabled: bool = False
    threshold_s: float = 900.0
    template: str = ""
    default_template: str = DEFAULT_CONTROLLER_PROMPT
    clock: Any = time.monotonic
    started_at: float = field(default=0.0)
    paused_s: float = 0.0
    reads: int = 0
    think_steps: int = 0
    count: int = 0
    last_write_at: float | None = None
    _retry_since: float | None = None
    _tool_since: float | None = None
    _tool_progress: bool = False

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    @classmethod
    def from_config(cls, config: Any, *, agent: bool = False, clock: Any = None) -> ProgressWatch:
        cfg = getattr(config, "progress_reminder", None)
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        if cfg is None:
            return cls(enabled=False, **kwargs)
        return cls(
            enabled=bool(cfg.enabled),
            threshold_s=max(60.0, float(cfg.minutes) * 60.0),
            template=str((cfg.agent_prompt if agent else cfg.controller_prompt) or ""),
            default_template=DEFAULT_AGENT_PROMPT if agent else DEFAULT_CONTROLLER_PROMPT,
            **kwargs,
        )

    def reset(self) -> None:
        self.started_at = self.clock()
        self.paused_s = 0.0
        self.reads = 0
        self.think_steps = 0
        self._tool_since = None
        if self._retry_since is not None:
            self._retry_since = self.started_at

    def on_user_input(self) -> None:
        self.reset()

    def on_model_step(self) -> None:
        self._end_tool_pause()
        self.think_steps += 1

    def _end_tool_pause(self) -> None:
        if self._tool_since is not None:
            self.paused_s += max(0.0, self.clock() - self._tool_since)
            self._tool_since = None

    def on_retry_wait(self, active: bool) -> None:
        now = self.clock()
        if active and self._retry_since is None:
            self._retry_since = now
        elif not active and self._retry_since is not None:
            self.paused_s += max(0.0, now - self._retry_since)
            self._retry_since = None

    def on_tool(self, name: str, arguments: Any) -> None:
        """工具开始执行时调用。距上次改文件已超过阈值时长，之后的 Bash 按查询计。"""
        now = self.clock()
        recent = self.last_write_at is not None and now - self.last_write_at < self.threshold_s
        progress = is_progress_tool(name, arguments, wrote=recent)
        if name in _WRITE_TOOLS:
            self.last_write_at = now
        self._tool_progress = progress
        if progress:
            self.reset()
        else:
            self.reads += 1
            self._end_tool_pause()
            self._tool_since = self.clock()

    def on_tool_finished(self, name: str, arguments: Any) -> None:
        """推进类工具（等 Agent、构建、等用户确认）结束时再起算；读取类工具的执行时间不计入。"""
        if self._tool_progress:
            self.reset()
        else:
            self._end_tool_pause()

    def idle_seconds(self) -> float:
        now = self.clock()
        paused = self.paused_s
        for since in (self._retry_since, self._tool_since):
            if since is not None:
                paused += max(0.0, now - since)
        return max(0.0, now - self.started_at - paused)

    def due(self) -> bool:
        return self.enabled and self.idle_seconds() >= self.threshold_s

    def take_notice(self) -> str:
        """到时返回提醒正文并重新计时；未到时返回空字符串。模板错误时退回内置模板。"""
        if not self.due():
            return ""
        self.count += 1
        variables = {"minutes": int(self.idle_seconds() // 60), "reads": self.reads,
                     "thinkSteps": self.think_steps, "count": self.count}
        try:
            text = render_notice(self.template, self.default_template, variables)
        except Exception:
            text = render_notice("", self.default_template, variables)
        self.reset()
        return text
