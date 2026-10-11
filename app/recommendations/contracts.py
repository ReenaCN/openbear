"""Recommendation settings and stable vocabularies."""
from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from app.webhooks.contracts import Model

LANES = ('external', 'openbear')
CATEGORIES = ('update', 'project', 'adjacent', 'improvement', 'resume', 'curiosity', 'lifestyle', 'news', 'openbear')
CARD_STATES = ('new', 'seen', 'engaged', 'sent', 'continued', 'followed', 'known', 'resolved', 'dismissed', 'adopted', 'parked')
FEEDBACK_ACTIONS = ('open_source', 'ask', 'sent', 'continued', 'follow', 'unfollow', 'known', 'resolved', 'dismiss', 'adopted', 'note')
PROFILE_KINDS = ('interest', 'ongoing', 'preference', 'background', 'habit')
PROFILE_SIGNALS = ('explicit', 'repeated', 'weak')
PROFILE_STATUSES = ('', 'done', 'active', 'paused', 'dropped', 'unknown')
# topic: researched by the model in the daily run; others are scripted checks.
FOLLOW_KINDS = ('topic', 'github_release', 'github_commits', 'npm', 'webpage')


class RecError(Exception):
    def __init__(self, code, message='', *, status=400, **extra):
        super().__init__(message or code)
        self.code, self.status = code, status
        self.payload = {'code': code, 'message': message or code, **extra}


class NetworkTools(Model):
    # Roles are assigned by the user, never inferred from a tool's public name.
    search: list[str] = Field(default_factory=list, max_length=20)
    fetch: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('search', 'fetch')
    @classmethod
    def tool_names(cls, values):
        names = list(dict.fromkeys(value.strip() for value in values))
        if any(not name or len(name) > 256 for name in names):
            raise ValueError('工具名称不能为空且不得超过 256 字符')
        return names

    def names(self):
        return list(dict.fromkeys([*self.search, *self.fetch]))


class Settings(Model):
    enabled: bool = Field(False, strict=True)
    # Same-protocol fallback chain. The first entry decides the protocol.
    models: list[str] = Field(default_factory=list, max_length=6)
    network_tools: NetworkTools = Field(default_factory=NetworkTools)
    thinking_level: str = ''
    timezone: str = 'Asia/Shanghai'
    # Daily organization time, HH:MM in the timezone. Empty = derive from activity.
    daily_time: str = ''
    follow_enabled: bool = Field(True, strict=True)
    follow_min_new_user_messages: int = Field(4, ge=1, le=50)
    follow_cooldown_minutes: int = Field(10, ge=1, le=1440)
    # How many conversations may be followed at the same time (each has its own run).
    follow_concurrency: int = Field(1, ge=1, le=5)
    follow_idle_minutes: int = Field(3, ge=0, le=120)
    tracking_interval_minutes: int = Field(45, ge=10, le=1440)
    daily_budget_usd: float | None = Field(None, gt=0, le=1000)
    daily_budget_tokens: int | None = Field(None, gt=0)
    scope_mode: Literal['all', 'include', 'exclude'] = 'all'
    scope_folders: list[str] = Field(default_factory=list, max_length=200)
    notify_mode: Literal['inherit', 'silent', 'telegram', 'push', 'both'] = 'inherit'
    initial_days: int = Field(7, ge=1, le=60)
    run_timeout_minutes: int = Field(40, ge=5, le=240)
    # User-editable prompts. Empty = use the built-in default (which may improve with updates).
    prompt_system: str = Field('', max_length=40000)
    prompt_daily: str = Field('', max_length=20000)
    prompt_follow: str = Field('', max_length=20000)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator('daily_time')
    @classmethod
    def valid_time(cls, value):
        value = (value or '').strip()
        if value:
            hour, _, minute = value.partition(':')
            if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) < 24 and 0 <= int(minute) < 60):
                raise ValueError('dailyTime must be HH:MM')
            value = f'{int(hour):02d}:{int(minute):02d}'
        return value

    @field_validator('models')
    @classmethod
    def unique_models(cls, value):
        out = []
        for item in value:
            item = str(item or '').strip()
            if item and item not in out:
                out.append(item)
        return out

    @model_validator(mode='after')
    def enabled_needs_model(self):
        if self.enabled and not self.models:
            raise ValueError('启用推荐需要至少选择一个模型')
        return self
