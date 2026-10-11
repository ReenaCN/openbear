"""Recommendation module service.

Each cycle (daily / follow-conversation) is an ordinary Web conversation in a
dedicated recommendation folder: the process stays visible and auditable while
cards, profile, feedback and follows live in recommendation tables. Scheduling,
increment selection, budget, fallback, feedback weights and follow checks are
programmatic; the model only judges and writes through the Recommendation tool.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.logging import get_logger
from app.recommendations import follows as follow_checks
from app.recommendations import prompts, sources, store
from app.recommendations.contracts import RecError, Settings
from app.recommendations.fallback import (
    FallbackBackend,
    FallbackChain,
    chain_entries,
    protocol_groups,
)
from app.recommendations.network_tools import legacy_selection, tool_options
from app.runtime.budget import bind, unbind
from app.webhooks.contracts import dumps
from app.webhooks.repository import insert, many, one, uid

log = get_logger('recommendations')

FOLDER_NAME = '推荐'
TOOL_NAME = 'Recommendation'
ACTIVE = ('starting', 'running')
KEEP_ACTIVE_RUN_CONVERSATIONS = 3


class DailyBudget:
    """Shared daily guard for model calls of every recommendation run (search is free)."""

    def __init__(self, service, owner, settings, run, task):
        self.s, self.db, self.owner, self.settings = service, service.db, owner, settings
        self.run, self.root, self.task = run, run['root_turn_uuid'], task
        self.closed = False
        self.reason = ''
        self.lock = asyncio.Lock()

    async def start_attempt(self, outcome):
        # Capture the actual request day, not the run's starting day or the
        # eventual settlement day of a stream that crosses midnight.
        outcome.started_at_ms = self.s.clock()

    async def account_attempt(self, outcome):
        from app.recommendations.ledger import record
        await record(self.db.conn, self.run, outcome, self.settings.timezone)

    async def refresh(self):
        return await one(self.db.conn, '''SELECT COALESCE(SUM(calls),0) calls,
            COALESCE(SUM(tokens),0) tokens,COALESCE(SUM(cost_usd),0) cost FROM rec_usage WHERE run_id=?''', (self.run['run_id'],))

    async def spent_today(self):
        return await self.s.spent(self.owner, self.s.local_date(self.settings))

    async def before_call(self):
        if self.closed or self.reason:
            raise asyncio.CancelledError()
        spent = await self.spent_today()
        if self.settings.daily_budget_usd is not None and spent['cost'] >= self.settings.daily_budget_usd:
            self.reason = 'budget_exhausted'
        elif self.settings.daily_budget_tokens is not None and spent['tokens'] >= self.settings.daily_budget_tokens:
            self.reason = 'budget_exhausted'
        if self.reason:
            raise asyncio.CancelledError()

    async def after_call(self):
        await self.refresh()
        if self.closed:
            return
        spent = await self.spent_today()
        if (self.settings.daily_budget_usd is not None and spent['cost'] >= self.settings.daily_budget_usd) or \
                (self.settings.daily_budget_tokens is not None and spent['tokens'] >= self.settings.daily_budget_tokens):
            self.reason = 'budget_exhausted'
            if asyncio.current_task() is not self.task and not self.task.done():
                self.task.cancel()


class RecommendationService:
    def __init__(self, db, host, *, clock=None):
        self.db, self.host = db, host
        self.clock = clock or (lambda: int(datetime.now().timestamp() * 1000))
        self.tasks: dict[str, asyncio.Task] = {}
        self.chains: dict[str, FallbackChain] = {}
        self.loop_task = None
        self.wake = asyncio.Event()
        self.closing = False
        self.batches: dict[str, dict] = {}

    # ------------------------------------------------------------ settings

    async def settings_row(self, owner):
        row = await one(self.db.conn, 'SELECT * FROM rec_settings WHERE owner_chat_id=?', (owner,))
        settings = Settings.model_validate_json(row['config_json']) if row else Settings()
        if row and 'network_tools' not in settings.model_fields_set:
            # Preserve only the legacy tools already granted to this owner's folder,
            # including temporarily unavailable ones. Reading settings grants nothing.
            from app.context.prompt_policy import read_json
            from app.web_console.prompt_policy import folder_policy_key
            policy = await read_json(self.db.conn, folder_policy_key(owner, row['folder_id'])) or {}
            settings.network_tools = legacy_selection(policy.get('toolNames') or [])
        return settings, row

    async def get_settings(self, owner):
        settings, row = await self.settings_row(owner)
        groups = protocol_groups(self.host.config)
        activity = await sources.activity(self.db.conn, owner, now_ms=self.clock(), tz_name=settings.timezone)
        return {'settings': settings.wire(), 'revision': row['revision'] if row else 0,
                'defaultPrompts': {'system': prompts.SYSTEM, 'daily': prompts.DAILY, 'follow': prompts.FOLLOW},
                'folderId': row['folder_id'] if row else '', 'protocols': groups,
                'networkToolOptions': tool_options(getattr(self.host, 'tools', None)),
                'derivedDailyTime': sources.quiet_time(activity['hourHistogram']), 'activity': activity,
                'folders': await self.host.cron.folders(owner) if getattr(self.host, 'cron', None) else []}

    def usable_models(self, settings):
        """Configured models that still resolve, in order (the first one's protocol wins)."""
        out, protocol = [], ''
        for label in settings.models:
            resolved = self.host.config.models.resolve(label)
            if not resolved or (protocol and resolved[0].protocol != protocol):
                continue
            protocol = protocol or resolved[0].protocol
            out.append(label)
        return out

    def model_health(self, settings):
        usable = self.usable_models(settings)
        missing = [label for label in settings.models if label not in usable]
        return {'usable': usable, 'unavailable': missing}

    def validate_models(self, settings, *, keep=frozenset()):
        """Newly chosen models must exist. A previously saved model that has since been removed
        may stay (it is skipped at run time and reported), so other settings — including turning
        recommendations off — can still be saved."""
        if not settings.models:
            return
        protocols = []
        for label in settings.models:
            resolved = self.host.config.models.resolve(label)
            if not resolved:
                if label in keep:
                    continue
                raise RecError('invalid_parameters', f'模型不可用：{label}', status=422)
            protocols.append(resolved[0].protocol)
        if len(set(protocols)) > 1:
            raise RecError('invalid_parameters', '备用模型必须与第一个模型使用同一协议', status=422)
        if settings.enabled and not protocols:
            raise RecError('invalid_parameters', '所选模型都已不可用，请重新选择', status=422)

    async def save_settings(self, owner, body):
        expected = body.get('expectedRevision')
        settings = Settings.model_validate(body.get('settings') or {})
        previous, _ = await self.settings_row(owner)
        self.validate_models(settings, keep=set(previous.models))
        if 'network_tools' not in settings.model_fields_set:
            settings.network_tools = previous.network_tools.model_copy(deep=True)
        available = {item['name'] for item in tool_options(getattr(self.host, 'tools', None)) if item['available']}
        missing = set(settings.network_tools.names()) - available - set(previous.network_tools.names())
        if missing:
            raise RecError('invalid_parameters', '工具不可用：' + '、'.join(sorted(missing)), status=422)
        now = self.clock()
        async with self.db.write_transaction(label='recommendation-settings') as conn:
            row = await one(conn, 'SELECT * FROM rec_settings WHERE owner_chat_id=?', (owner,))
            current = row['revision'] if row else 0
            if expected is not None and int(expected) != current:
                raise RecError('revision_conflict', '设置已被修改，请刷新后重试', status=409, currentRevision=current)
            if row:
                await conn.execute('UPDATE rec_settings SET config_json=?,revision=revision+1,updated_at_ms=? WHERE owner_chat_id=?',
                                   (settings.model_dump_json(by_alias=True), now, owner))
            else:
                await insert(conn, 'rec_settings', owner_chat_id=owner, config_json=settings.model_dump_json(by_alias=True),
                             updated_at_ms=now)
        prompt_changed = row is not None and Settings.model_validate_json(row['config_json']).prompt_system != settings.prompt_system
        tools_changed = previous.network_tools != settings.network_tools
        if settings.enabled or ((prompt_changed or tools_changed) and await self.rec_folder(owner)):
            await self.ensure_folder(owner, force_prompt=prompt_changed)
        self.wake.set()
        return await self.get_settings(owner)

    # ------------------------------------------------------------ folder & prompt

    async def ensure_folder(self, owner, *, force_prompt=False):
        """Dedicated recommendation folder with an override prompt and tool allowlist.
        The prompt follows the recommendation settings; force_prompt applies a just-saved edit."""
        from app.context.prompt_policy import write_json
        from app.web_console.prompt_policy import folder_policy_key
        from app.context.prompt_policy import read_json
        digest_key = f'recommendation_prompt_default:{owner}'
        now_s = self.clock() // 1000
        async with self.host._conversation_tree_lock:
            async with self.db.write_transaction(label='recommendation-folder') as conn:
                # Read on the writer after acquiring both locks: a queued refresh must
                # not restore tools removed by a settings save that completed meanwhile.
                settings, row = await self.settings_row(owner)
                folder_id = row['folder_id'] if row else ''
                if folder_id and not await folder_owned(conn, owner, folder_id):
                    folder_id = ''
                managed = settings.network_tools.names()
                required = [TOOL_NAME, *managed]
                system = self.system_prompt(settings)
                default_digest = hashlib.sha256(system.encode()).hexdigest()
                if not folder_id:
                    folder_id = str(uuid.uuid4())
                    taken = {r['name'] for r in await many(conn, "SELECT name FROM web_conversation_folders WHERE owner_chat_id=? AND parent_uuid=''", (owner,))}
                    name = next(n for n in (FOLDER_NAME, 'OpenBear 推荐', *(f'OpenBear 推荐 {i}' for i in range(2, 100))) if n not in taken)
                    await conn.execute('''INSERT INTO web_conversation_folders
                        (folder_uuid,owner_chat_id,parent_uuid,name,display_order,created_at,updated_at,prompt_markdown)
                        VALUES (?,?,?,?,?,?,?,?)''', (folder_id, owner, '', name, 1e12, now_s, now_s, system))
                    await conn.execute('''INSERT INTO rec_settings(owner_chat_id,config_json,folder_id,updated_at_ms) VALUES(?,?,?,?)
                        ON CONFLICT(owner_chat_id) DO UPDATE SET folder_id=excluded.folder_id''',
                                       (owner, Settings().model_dump_json(by_alias=True), folder_id, self.clock()))
                    await write_json(conn, digest_key, default_digest)
                else:
                    # The folder prompt is user-editable. Upgrade it only while it is
                    # still the default we wrote (or empty); never overwrite an edit.
                    current = await one(conn, 'SELECT prompt_markdown FROM web_conversation_folders WHERE folder_uuid=?', (folder_id,))
                    text = (current or {}).get('prompt_markdown') or ''
                    written = await read_json(conn, digest_key)
                    if force_prompt or not text.strip() or hashlib.sha256(text.encode()).hexdigest() == written:
                        if text != system:
                            await conn.execute('UPDATE web_conversation_folders SET prompt_markdown=?,updated_at=? WHERE folder_uuid=?',
                                               (system, now_s, folder_id))
                        await write_json(conn, digest_key, default_digest)
                # Replace only our managed part; preserve unrelated tools the user added.
                # Offline selections stay allowed so reconnecting cannot silently lose them.
                existing = await read_json(conn, folder_policy_key(owner, folder_id)) or {}
                managed_key = f'recommendation_managed_tools:{owner}:{folder_id}'
                old_managed = await read_json(conn, managed_key)
                if old_managed is None:
                    old_managed = legacy_selection(existing.get('toolNames') or []).names()
                kept = [name for name in existing.get('toolNames') or [] if isinstance(name, str) and name not in old_managed]
                policy_value = {'overrideSystemPrompt': True, 'toolsEnabled': True,
                                'userMessageTemplateEnabled': bool(existing.get('userMessageTemplateEnabled', False)),
                                'toolNames': list(dict.fromkeys([*required, *kept]))}
                await write_json(conn, folder_policy_key(owner, folder_id), policy_value)
                await write_json(conn, managed_key, managed)
        if hasattr(self.host, '_invalidate_tree_status'):
            self.host._invalidate_tree_status(owner)
        return folder_id

    async def rec_folder(self, owner):
        row = await one(self.db.conn, '''SELECT s.folder_id FROM rec_settings s JOIN web_conversation_folders f
            ON f.folder_uuid=s.folder_id AND f.owner_chat_id=s.owner_chat_id WHERE s.owner_chat_id=?''', (owner,))
        return row['folder_id'] if row else ''

    async def protects_folder(self, owner, folder_id):
        """The recommendation folder cannot be deleted while recommendations are on."""
        if not folder_id or folder_id != await self.rec_folder(owner):
            return False
        settings, _ = await self.settings_row(owner)
        return bool(settings.enabled) or bool(await self.active_count(owner, 'daily') or await self.active_count(owner, 'follow'))

    async def forget_folder(self, conn, owner, folder_id):
        """Called inside the folder-delete transaction: drop our pointer so the next enable makes a new one."""
        await conn.execute("UPDATE rec_settings SET folder_id='' WHERE owner_chat_id=? AND folder_id=?", (owner, folder_id))

    # ------------------------------------------------------------ spend

    def local_date(self, settings, ms=None):
        return datetime.fromtimestamp((ms or self.clock()) / 1000, ZoneInfo(settings.timezone)).strftime('%Y-%m-%d')

    async def spent(self, owner, local_date):
        row = await one(self.db.conn, 'SELECT COALESCE(SUM(cost_usd),0) cost,COALESCE(SUM(tokens),0) tokens FROM rec_usage WHERE owner_chat_id=? AND local_date=?', (owner, local_date))
        return {'cost': float(row['cost'] or 0), 'tokens': int(row['tokens'] or 0)}

    async def budget_left(self, owner, settings):
        spent = await self.spent(owner, self.local_date(settings))
        return {'spentUsd': round(spent['cost'], 4), 'spentTokens': spent['tokens'],
                'budgetUsd': settings.daily_budget_usd, 'budgetTokens': settings.daily_budget_tokens,
                'exhausted': bool((settings.daily_budget_usd is not None and spent['cost'] >= settings.daily_budget_usd)
                                  or (settings.daily_budget_tokens is not None and spent['tokens'] >= settings.daily_budget_tokens))}

    # ------------------------------------------------------------ runs

    async def active_run(self, owner, kind=None):
        sql = 'SELECT * FROM rec_runs WHERE owner_chat_id=? AND status IN (?,?)'
        args = [owner, *ACTIVE]
        if kind:
            sql += ' AND kind=?'
            args.append(kind)
        return await one(self.db.conn, sql + ' ORDER BY started_at_ms DESC LIMIT 1', args)

    async def active_count(self, owner, kind):
        row = await one(self.db.conn, 'SELECT COUNT(*) n FROM rec_runs WHERE owner_chat_id=? AND kind=? AND status IN (?,?)', (owner, kind, *ACTIVE))
        return int(row['n'] or 0)

    async def run_conversation(self, owner, row, folder_id, entries, thinking, settings):
        """Daily runs get a fresh conversation. A followed conversation keeps ONE recommendation
        conversation, so later follows continue with what was already found for it."""
        label = '每日整理' if row['kind'] == 'daily' else '跟随对话'
        if row['kind'] == 'follow' and row['source_conversation_uuid']:
            conv_id = follow_conversation_id(owner, row['source_conversation_uuid'])
            existing = await self.host._conversation_row(owner, conv_id)
            if existing and (str(existing.get('folder_uuid') or '') != folder_id
                             or self.host.runs.is_running(int(existing['internal_chat_id']))):
                # Moved out by the user (it is theirs now), or the user is talking in it:
                # never take over that conversation. Start a fresh follow conversation.
                conv_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'openbear:recommendation:follow:{owner}:{row["source_conversation_uuid"]}:{row["run_id"]}'))
                existing = None
            if existing:
                # Refresh the frozen prompt/tool policy only between runs. An active run
                # keeps its own snapshot; a reused follow must see the latest tool selection.
                from app.context.prompt_policy import read_json, snapshot_key
                frozen_policy = await read_json(self.db.conn, snapshot_key(conv_id))
                live_policy = await self.host._resolved_prompt_policy(conv_id)
                tools_changed = frozen_policy is not None and any(
                    frozen_policy.get(key) != live_policy.get(key)
                    for key in ('overrideSystemPrompt', 'toolsEnabled', 'toolNames'))
                folder_prompt = await one(self.db.conn, 'SELECT prompt_markdown FROM web_conversation_folders WHERE folder_uuid=?', (folder_id,))
                snapshot = await one(self.db.conn, 'SELECT system_snapshot FROM sessions WHERE chat_id=?', (existing['internal_chat_id'],))
                wanted = str((folder_prompt or {}).get('prompt_markdown') or '')
                if snapshot and snapshot['system_snapshot'] and (tools_changed or (wanted and wanted not in snapshot['system_snapshot'])):
                    async with self.db.write_transaction(label='recommendation-follow-prompt') as conn:
                        await conn.execute("UPDATE sessions SET system_snapshot='' WHERE chat_id=?", (existing['internal_chat_id'],))
                if existing.get('archived_at'):
                    async with self.db.write_transaction(label='recommendation-follow-unarchive') as conn:
                        await conn.execute('UPDATE web_conversations SET archived_at=NULL WHERE conversation_uuid=?', (conv_id,))
                    self.host._invalidate_tree_status(owner)
                    existing = await self.host._conversation_row(owner, conv_id)
                async with self.db.write_transaction(label='recommendation-follow-model') as conn:
                    await conn.execute('UPDATE web_conversations SET model=? WHERE conversation_uuid=?',
                                       (entries[0]['label'], conv_id))
                    await conn.execute('UPDATE sessions SET thinking_level=? WHERE chat_id=?',
                                       (thinking, existing['internal_chat_id']))
                return await self.host._conversation_row(owner, conv_id)
            source = await one(self.db.conn, 'SELECT title FROM web_conversations WHERE conversation_uuid=?', (row['source_conversation_uuid'],))
            title = f'[推荐] 跟随 · {(source or {}).get("title") or "会话"}'[:80]
        else:
            conv_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'openbear:recommendation:' + row['run_id']))
            when = datetime.fromtimestamp(row['started_at_ms'] / 1000, ZoneInfo(settings.timezone)).strftime('%m-%d %H:%M')
            title = f'[推荐] {label} · {when}'
        return await self.host._create_web_conversation(
            owner, folder_uuid=folder_id, title=title, conversation_uuid=conv_id,
            run_config={'main_model': entries[0]['label'], 'main_thinking_level': thinking,
                        'context_strategy': 'sliding_window'})

    async def start_run(self, owner, kind, *, trigger='manual', source_conversation=''):
        settings, _ = await self.settings_row(owner)
        if trigger != 'manual' and not settings.enabled:
            raise RecError('disabled', '推荐已关闭', status=409)
        if not settings.models:
            raise RecError('not_configured', '请先在推荐设置里选择模型', status=409)
        if not self.usable_models(settings):
            raise RecError('models_unavailable', '推荐设置里的模型都已不可用，请重新选择', status=409)
        if (await self.budget_left(owner, settings))['exhausted']:
            raise RecError('budget_exhausted', '今日推荐预算已用完', status=409)
        if kind == 'follow':
            if source_conversation and await one(self.db.conn, "SELECT 1 FROM rec_runs WHERE owner_chat_id=? AND kind='follow' AND source_conversation_uuid=? AND status IN (?,?)",
                                                 (owner, source_conversation, *ACTIVE)):
                raise RecError('already_running', '这个会话正在被跟随', status=409)
            if await self.active_count(owner, 'follow') >= settings.follow_concurrency:
                raise RecError('already_running', '同时跟随的会话已达上限', status=409)
        elif await self.active_run(owner, kind):
            raise RecError('already_running', '同类推荐正在运行', status=409)
        folder_id = await self.ensure_folder(owner)
        now = self.clock()
        run_id = uid()
        async with self.db.write_transaction(label='recommendation-run') as conn:
            if trigger != 'manual':
                # Serialize the final enabled check and run creation with save_settings.
                # Preliminary reads may predate a disable committed while we awaited work
                # or this writer. Once committed, the run is allowed to finish normally.
                current = await one(conn, 'SELECT config_json FROM rec_settings WHERE owner_chat_id=?', (owner,))
                if not current or not Settings.model_validate_json(current['config_json']).enabled:
                    raise RecError('disabled', '推荐已关闭', status=409)
            follow_from = 0
            if kind == 'follow' and source_conversation:
                cursor = await one(conn, 'SELECT follow_seq FROM rec_cursors WHERE owner_chat_id=? AND conversation_uuid=?',
                                   (owner, source_conversation))
                follow_from = int((cursor or {}).get('follow_seq') or 0)
            await insert(conn, 'rec_runs', run_id=run_id, owner_chat_id=owner, kind=kind, trigger=trigger,
                         source_conversation_uuid=source_conversation, root_turn_uuid=uid(),
                         models_json=dumps(self.usable_models(settings)), local_date=self.local_date(settings, now), started_at_ms=now,
                         batch_json=dumps({'followFrom': follow_from}) if kind == 'follow' else '{}',
                         notification_state='pending' if kind == 'daily' else 'not_requested')
            if kind == 'follow' and source_conversation:
                await sources.mark_follow_started(conn, owner, source_conversation, now)
            # Read back on the writer: the shared reader may still hold an older snapshot.
            row = await one(conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run_id,))
        task = asyncio.create_task(self.execute(row, settings, folder_id), name='recommendation:' + run_id)
        self.tasks[run_id] = task
        task.add_done_callback(lambda finished: self._done(run_id, finished))
        return run_public(row)

    def _done(self, run_id, finished):
        self.tasks.pop(run_id, None)
        self.chains.pop(run_id, None)
        self.batches.pop(run_id, None)
        if not finished.cancelled() and finished.exception():
            log.error('Recommendation run failed', run_id=run_id,
                      error=f'{type(finished.exception()).__name__}: {finished.exception()}')

    async def patch(self, run_id, **values):
        async with self.db.write_transaction(label='recommendation-run-patch') as conn:
            await conn.execute('UPDATE rec_runs SET ' + ','.join(k + '=?' for k in values) + ' WHERE run_id=?',
                               (*values.values(), run_id))

    def instruction(self, row, settings):
        tz = ZoneInfo(settings.timezone)
        now = datetime.fromtimestamp(self.clock() / 1000, tz).strftime('%Y-%m-%d %H:%M (%A)')
        body = (settings.prompt_daily.strip() or prompts.DAILY) if row['kind'] == 'daily' else (settings.prompt_follow.strip() or prompts.FOLLOW)
        return f'当前时间：{now}（{settings.timezone}）\n本轮 runId：{row["run_id"]}\n\n{body}'

    @staticmethod
    def system_prompt(settings):
        return settings.prompt_system.strip() or prompts.SYSTEM

    async def execute(self, row, settings, folder_id):
        from app.web_console.live_stream import _WebStreamRenderer
        owner, run_id, root = row['owner_chat_id'], row['run_id'], row['root_turn_uuid']
        conversation = None
        status, error = 'failed', ''
        budget = DailyBudget(self, owner, settings, row, asyncio.current_task())
        token = bind(budget)
        try:
            entries = chain_entries(self.host.config, self.host.llm_factory, self.usable_models(settings))
            if not entries:
                raise RecError('not_configured', '推荐模型均不可用')

            async def switched(previous, current, reason):
                if live is not None:
                    await live.publish({'type': 'notice', 'turnUuid': root,
                                        'text': f'模型 {previous} 不可用（{reason}），从这一步起改用 {current}'})
                await self.patch(run_id, models_json=dumps(self.chains[run_id].used))

            live = None
            self.chains[run_id] = FallbackChain(entries, on_switch=switched)
            thinking = settings.thinking_level or entries[0]['default_thinking'] or ''
            conversation = await self.run_conversation(owner, row, folder_id, entries, thinking, settings)
            await self.patch(run_id, conversation_uuid=conversation['conversation_uuid'], status='running')
            self.host.runs.register(conversation['internal_chat_id'], asyncio.current_task())
            live = self.host._live_for(conversation)
            await live.publish({'type': 'accepted', 'turnUuid': root, 'runUuid': root, 'source': 'recommendation',
                                'taskNotificationSilent': True})
            text = self.instruction(row, settings)
            await live.publish({'type': 'user', 'turnUuid': root, 'messageUuid': run_id, 'text': text,
                                'source': 'recommendation', 'recommendationRunId': run_id,
                                'recommendationCard': run_card(row)})
            renderer = _WebStreamRenderer(live=live)
            async with asyncio.timeout(settings.run_timeout_minutes * 60) as deadline:
                success = await self.host._run_web_turn(
                    conversation['internal_chat_id'], text, renderer, conversation=conversation,
                    root_turn_uuid=root, user_op_id='msg:' + run_id, recommendation_run_id=run_id,
                    backend_override=FallbackBackend(self.chains[run_id]))
            status = 'completed' if success else 'failed'
            if not success:
                error = str(getattr(live, 'last_error', '') or '模型执行失败')
            # The turn absorbs a timeout or stop and just reports failure; classify it here.
            if deadline.expired():
                status, error = 'timed_out', '执行超时'
            elif asyncio.current_task().cancelling():
                status, error = 'interrupted' if self.closing else 'cancelled', ''
            if budget.reason:
                # The runtime absorbs the guard's cancellation; the guard owns the outcome.
                status, error = budget.reason, '今日推荐预算已用完'
        except TimeoutError:
            status, error = 'timed_out', '执行超时'
        except asyncio.CancelledError:
            status = budget.reason or ('interrupted' if self.closing else 'cancelled')
            error = '今日推荐预算已用完' if budget.reason else ''
        except RecError as exc:
            status, error = 'failed', exc.payload['message']
        except Exception as exc:  # Recorded on the run; never re-run business work.
            status, error = 'failed', f'{type(exc).__name__}: {exc}'
            log.exception('Recommendation run error')
        finally:
            budget.closed = True
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await budget.refresh()
            unbind(budget, token)
        await asyncio.shield(self.finalize(row, conversation, status, error))

    async def finalize(self, row, conversation, status, error):
        run_id = row['run_id']
        cards = (await one(self.db.conn, 'SELECT COUNT(*) n FROM rec_cards WHERE run_id=?', (run_id,)))['n']
        chain = self.chains.get(run_id)
        await self.patch(run_id, status=status, error=error, cards=cards, finished_at_ms=self.clock(),
                         **({'models_json': dumps(chain.used)} if chain else {}))
        if conversation:
            with contextlib.suppress(Exception):
                await self.host._touch_web_conversation(conversation['conversation_uuid'],
                                                        status='idle' if status in ('completed', 'cancelled') else 'error',
                                                        current_status='推荐完成' if status == 'completed' else '推荐：' + status,
                                                        last_error=error)
            with contextlib.suppress(Exception):
                await self.archive_old_conversations(row['owner_chat_id'])
        if row['kind'] == 'daily':
            with contextlib.suppress(Exception):
                await self.notify(await one(self.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run_id,)))

    async def archive_old_conversations(self, owner):
        folder = await self.rec_folder(owner)
        if not folder:
            return
        rows = await many(self.db.conn, '''SELECT conversation_uuid FROM web_conversations WHERE owner_chat_id=? AND folder_uuid=?
            AND COALESCE(archived_at,0)=0 AND status NOT IN ('running') ORDER BY updated_at DESC,created_at DESC''', (owner, folder))
        stale = [r['conversation_uuid'] for r in rows[KEEP_ACTIVE_RUN_CONVERSATIONS:]]
        if stale:
            async with self.db.write_transaction(label='recommendation-archive') as conn:
                for item in stale:
                    await conn.execute('UPDATE web_conversations SET archived_at=? WHERE conversation_uuid=? AND COALESCE(archived_at,0)=0',
                                       (self.clock() // 1000, item))
            self.host._invalidate_tree_status(owner)

    async def unseen(self, owner):
        row = await one(self.db.conn, """SELECT COUNT(*) n FROM rec_cards WHERE owner_chat_id=? AND seen_at_ms IS NULL
            AND removed_at_ms IS NULL""", (owner,))
        return int(row['n'] or 0)

    async def reconcile_orphans(self, owner):
        """A run row without a live task in this process can never progress; show it as interrupted."""
        stale = [r['run_id'] for r in await many(self.db.conn, 'SELECT run_id FROM rec_runs WHERE owner_chat_id=? AND status IN (?,?) AND started_at_ms<?',
                                                   (owner, *ACTIVE, self.clock() - 60_000)) if r['run_id'] not in self.tasks]
        if stale:
            async with self.db.write_transaction(label='recommendation-orphans') as conn:
                for run_id in stale:
                    await conn.execute("UPDATE rec_runs SET status='interrupted',error='运行未正常结束',finished_at_ms=? WHERE run_id=? AND status IN (?,?)",
                                       (self.clock(), run_id, *ACTIVE))

    async def stop(self, owner, run_id):
        row = await one(self.db.conn, 'SELECT * FROM rec_runs WHERE run_id=? AND owner_chat_id=?', (run_id, owner))
        if not row:
            raise RecError('not_found', '推荐运行不存在', status=404)
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            return {'accepted': True}
        return {'accepted': False, 'status': row['status']}

    async def owns_event(self, conversation, root):
        return bool(root and await one(self.db.conn, 'SELECT 1 FROM rec_runs WHERE conversation_uuid=? AND root_turn_uuid=?',
                                       (conversation, root)))

    async def owns_conversation(self, conversation_uuid):
        return await one(self.db.conn, 'SELECT * FROM rec_runs WHERE conversation_uuid=? ORDER BY started_at_ms DESC LIMIT 1',
                         (conversation_uuid,))

    # ------------------------------------------------------------ notifications

    async def notify(self, row):
        settings, _ = await self.settings_row(row['owner_chat_id'])
        mode = settings.notify_mode
        if mode == 'silent' or row['status'] not in ('completed',) and row['status'] != 'failed':
            await self.patch(row['run_id'], notification_state='suppressed')
            return
        cards = await many(self.db.conn, 'SELECT title FROM rec_cards WHERE run_id=? ORDER BY priority DESC,created_at_ms LIMIT 3',
                           (row['run_id'],))
        if row['status'] == 'completed' and not row['cards']:
            await self.patch(row['run_id'], notification_state='suppressed')
            return
        if row['status'] == 'completed':
            text = f'今日推荐 {row["cards"]} 条\n' + '\n'.join('· ' + c['title'] for c in cards)
        else:
            text = '今日推荐生成失败：' + (row['error'] or row['status'])
        base = str(getattr(self.host.config.web, 'custom_url', '') or '').rstrip('/')
        link = f'\n{base}/recommendations' if base else ''
        channels = 0
        owner = row['owner_chat_id']
        tg = getattr(self.host, 'web_task_telegram', None)
        if tg is not None and mode in ('inherit', 'telegram', 'both'):
            import html

            from app.telegram_ui import send_rich
            whitelist = {int(v) for v in self.host.config.telegram.whitelist_ids}
            if owner in whitelist and (mode != 'inherit' or self.host.config.web.task_notifications.enabled):
                with contextlib.suppress(Exception):
                    await send_rich(self.host.bot, owner, '<b>OpenBear 推荐</b>\n\n' + html.escape(text + link))
                    channels += 1
        push = getattr(self.host, 'browser_push', None)
        if push is not None and mode in ('inherit', 'push', 'both') and await push.subscriptions(owner):
            await push.enqueue(owner, '', 'recommendation:' + row['run_id'], 'recommendation', task_title=text.split('\n')[0])
            channels += 1
        await self.patch(row['run_id'], notification_state='sent' if channels else 'suppressed')

    # ------------------------------------------------------------ scheduling

    async def start(self):
        now = self.clock()
        async with self.db.write_transaction(label='recommendation-startup') as conn:
            await conn.execute("UPDATE rec_runs SET status='interrupted',error='服务重启中断，未自动重跑',finished_at_ms=? WHERE status IN ('starting','running')",
                               (now,))
        self.loop_task = asyncio.create_task(self.loop(), name='recommendation-loop')

    async def close(self):
        self.closing = True
        self.wake.set()
        if self.loop_task:
            self.loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.loop_task
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def loop(self):
        while not self.closing:
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Recommendation scheduler failure')
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), 60)

    def daily_due_at(self, settings, derived, now):
        tz = ZoneInfo(settings.timezone)
        hh, mm = (settings.daily_time or derived).split(':')
        local = datetime.fromtimestamp(now / 1000, tz)
        due = local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if due > local:
            due -= timedelta(days=1)
        return int(due.timestamp() * 1000)

    async def tick(self):
        now = self.clock()
        for row in await many(self.db.conn, 'SELECT owner_chat_id,config_json FROM rec_settings'):
            settings = Settings.model_validate_json(row['config_json'])
            if not settings.enabled or not settings.models:
                continue
            owner = row['owner_chat_id']
            with contextlib.suppress(RecError):
                await self.tick_owner(owner, settings, now)

    async def tick_owner(self, owner, settings, now):
        # Active-run counts gate new runs; a row left active by a failed finalize must not hold a slot.
        await self.reconcile_orphans(owner)
        folder = await self.rec_folder(owner)
        budget = await self.budget_left(owner, settings)
        # Follow checks never call the model; they only record a pending change.
        await follow_checks.check_due(self, owner, settings, now)
        if budget['exhausted']:
            return
        activity = await sources.activity(self.db.conn, owner, now_ms=now, tz_name=settings.timezone)
        due = self.daily_due_at(settings, sources.quiet_time(activity['hourHistogram']), now)
        last_daily = await one(self.db.conn, "SELECT MAX(started_at_ms) n FROM rec_runs WHERE owner_chat_id=? AND kind='daily'", (owner,))
        if (last_daily['n'] or 0) < due and not await self.active_run(owner, 'daily'):
            if await self.daily_has_work(owner, settings, folder, now):
                await self.start_run(owner, 'daily', trigger='scheduled')
            else:
                await self.record_skip(owner, settings, 'daily', now)
            return
        slots = settings.follow_concurrency - await self.active_count(owner, 'follow') if settings.follow_enabled else 0
        if slots > 0:
            busy = {r['source_conversation_uuid'] for r in await many(
                self.db.conn, "SELECT source_conversation_uuid FROM rec_runs WHERE owner_chat_id=? AND kind='follow' AND status IN (?,?)",
                (owner, *ACTIVE))}
            candidates = await sources.follow_candidates(self.db.conn, owner, settings, folder, now_ms=now)
            for candidate in candidates:
                if slots <= 0:
                    break
                if candidate['conversation_uuid'] in busy:
                    continue
                if self.host.runs.is_running(await self.internal_chat_id(candidate['conversation_uuid'])):
                    continue  # Wait until the user's own turn ends.
                if self.host.runs.is_running(await self.internal_chat_id(follow_conversation_id(owner, candidate['conversation_uuid']))):
                    continue  # The user is talking in this follow's recommendation conversation.
                await self.start_run(owner, 'follow', trigger='conversation', source_conversation=candidate['conversation_uuid'])
                slots -= 1

    async def internal_chat_id(self, conversation_uuid):
        row = await one(self.db.conn, 'SELECT internal_chat_id FROM web_conversations WHERE conversation_uuid=?', (conversation_uuid,))
        return int(row['internal_chat_id']) if row else 0

    async def daily_has_work(self, owner, settings, folder, now):
        since = now - settings.initial_days * 86_400_000
        if await sources.candidates(self.db.conn, owner, settings, folder, since_ms=since):
            return True
        if await one(self.db.conn, "SELECT 1 FROM rec_follows WHERE owner_chat_id=? AND active=1 AND (kind='topic' OR pending_change<>'')", (owner,)):
            return True
        if await one(self.db.conn, 'SELECT 1 FROM rec_feedback WHERE owner_chat_id=? AND created_at_ms>=?', (owner, now - 86_400_000)):
            return True
        # The external lane still researches active interests even on a quiet day,
        # but never on an owner with no profile yet (nothing to research for).
        return bool(await one(self.db.conn, 'SELECT 1 FROM rec_profile WHERE owner_chat_id=? AND removed_at_ms IS NULL LIMIT 1', (owner,)))

    async def record_skip(self, owner, settings, kind, now):
        async with self.db.write_transaction(label='recommendation-skip') as conn:
            await insert(conn, 'rec_runs', run_id=uid(), owner_chat_id=owner, kind=kind, trigger='scheduled',
                         root_turn_uuid=uid(), status='skipped', error='没有新对话、待处理的关注变化或反馈',
                         local_date=self.local_date(settings, now), started_at_ms=now, finished_at_ms=now)

    # ------------------------------------------------------------ user-message feedback

    async def observe_user_message(self, conversation, manifest, turn_uuid):
        """Map a real human send to card feedback: sent (card attached) / continued (later follow-up)."""
        owner = int(conversation['owner_chat_id'])
        conv = str(conversation['conversation_uuid'])
        now = self.clock()
        card_ids = [str(item.get('id')) for item in manifest if isinstance(item, dict) and item.get('kind') == 'rec']
        if not card_ids and not await one(self.db.conn, "SELECT 1 FROM rec_feedback WHERE owner_chat_id=? AND conversation_uuid=? AND action='sent' LIMIT 1",
                                          (owner, conv)):
            return  # An ordinary conversation: no write transaction on the send path.
        async with self.db.write_transaction(label='recommendation-observe') as conn:
            if card_ids:
                for card_id in dict.fromkeys(card_ids):
                    card = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=? AND owner_chat_id=?', (card_id, owner))
                    if card:
                        await store.feedback(conn, owner, card, 'sent', now, detail='', conversation_uuid=conv,
                                             request_id=f'sent:{card_id}:{conv}')
                return
            linked = await many(conn, '''SELECT DISTINCT card_id FROM rec_feedback WHERE owner_chat_id=? AND conversation_uuid=?
                AND action='sent' ''', (owner, conv))
            for item in linked:
                card = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=? AND owner_chat_id=?', (item['card_id'], owner))
                if card:
                    await store.feedback(conn, owner, card, 'continued', now, conversation_uuid=conv,
                                         request_id=f'continued:{item["card_id"]}:{conv}')

    # ------------------------------------------------------------ tool context

    async def run_for_context(self, ctx):
        run_id = getattr(ctx, 'recommendation_run_id', '') or ''
        if not run_id:
            raise RecError('not_recommendation_run', 'Recommendation 工具只能在推荐运行中使用', status=403)
        row = await one(self.db.conn, 'SELECT * FROM rec_runs WHERE run_id=?', (run_id,))
        if not row or row['status'] not in ACTIVE:
            raise RecError('run_not_active', '推荐运行已结束', status=409)
        return row

    async def status(self, run):
        owner = run['owner_chat_id']
        settings, _ = await self.settings_row(owner)
        now = self.clock()
        await self._park(owner, now)
        profile = await store.profile_list(self.db.conn, owner)
        recent = await store.card_list(self.db.conn, owner, since_ms=now - 14 * 86_400_000, limit=60)
        return {
            'run': {'id': run['run_id'], 'kind': run['kind'], 'sourceConversationUuid': run['source_conversation_uuid']},
            'now': datetime.fromtimestamp(now / 1000, ZoneInfo(settings.timezone)).isoformat(),
            'budget': await self.budget_left(owner, settings),
            'profile': [{**{k: p[k] for k in ('id', 'kind', 'topic', 'category', 'status', 'signal', 'detail', 'location', 'userEdited')},
                         'lastMentioned': self.format_when(p['lastMentionedMs'], settings)} for p in profile],
            'recentCards': [{k: c[k] for k in ('id', 'title', 'topic', 'category', 'lane', 'state', 'createdAtMs', 'dedupeKey')}
                            for c in recent['items']],
            'feedback': await store.recent_feedback(self.db.conn, owner, now - 14 * 86_400_000),
            'misreads': await store.misreads(self.db.conn, owner, now - 30 * 86_400_000),
            # A few per day: each review reads a whole conversation inside the same run timeout.
            'openedCards': await store.opened_unreviewed(self.db.conn, owner, now - 14 * 86_400_000, limit=6)
            if run['kind'] == 'daily' else None,
            'weights': await store.weights(self.db.conn, owner, now),
            'askedQuestions': await store.asked_questions(self.db.conn, owner),
            'follows': [{k: f[k] for k in ('id', 'kind', 'target', 'topic', 'note', 'lastUpdate', 'pendingChange', 'changeToken', 'error', 'lastValue')}
                        for f in await store.follows(self.db.conn, owner)],
            'pendingSources': len(await sources.candidates(self.db.conn, owner, settings, await self.rec_folder(owner),
                                                           since_ms=now - settings.initial_days * 86_400_000))
            if run['kind'] == 'daily' else None,
        }

    def format_when(self, ms, settings):
        if not ms:
            return ''
        return datetime.fromtimestamp(ms / 1000, ZoneInfo(settings.timezone)).strftime('%Y-%m-%d %H:%M')

    def parse_when(self, value, settings):
        """'YYYY-MM-DD[ HH:MM]' or the material's 'MM-DD HH:MM' (user message time) → ms."""
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        text = str(value or '').strip()
        if not text:
            return None
        tz = ZoneInfo(settings.timezone)
        now = datetime.fromtimestamp(self.clock() / 1000, tz)
        for pattern in ('%Y-%m-%d %H:%M', '%Y-%m-%d', '%m-%d %H:%M', '%m-%d'):
            try:
                parsed = datetime.strptime(text, pattern)
            except ValueError:
                continue
            if '%Y' not in pattern:
                parsed = parsed.replace(year=now.year)
                if parsed.replace(tzinfo=tz) > now + timedelta(days=1):
                    parsed = parsed.replace(year=now.year - 1)
            return int(parsed.replace(tzinfo=tz).timestamp() * 1000)
        raise RecError('invalid_parameters', 'lastMentioned 格式应为 YYYY-MM-DD HH:MM 或材料里的 MM-DD HH:MM')

    async def _park(self, owner, now):
        async with self.db.write_transaction(label='recommendation-park') as conn:
            await store.park_unanswered(conn, owner, now)

    async def source_next(self, run):
        owner = run['owner_chat_id']
        settings, _ = await self.settings_row(owner)
        now = self.clock()
        # Reserve JSON envelope space; escaping is accounted inside each page.
        page_chars = max(128, min(sources.BATCH_CHARS, sources.result_chars.get()) - 1024)
        if run['kind'] == 'follow':
            follow_from = json.loads(run['batch_json'] or '{}').get('followFrom')
            batch = await sources.build_follow_batch(self.db.conn, owner, settings, run['source_conversation_uuid'], now_ms=now,
                                                     from_seq=follow_from, rec_folder=await self.rec_folder(owner), max_chars=page_chars)
        else:
            batch = await sources.build_batch(self.db.conn, owner, settings, await self.rec_folder(owner),
                                              since_ms=now - settings.initial_days * 86_400_000, max_chars=page_chars)
        batch_id = uid()
        self.batches[run['run_id']] = {batch_id: batch['conversations']}  # Only the latest delivered page may commit.
        previous = json.loads(run['batch_json'] or '{}')
        await self.patch(run['run_id'], batch_json=dumps({**previous, 'batchId': batch_id, 'conversations': len(batch['conversations'])}))
        if not batch['analyzable'] and batch['conversations']:
            # Nothing to analyze, cursors only: commit immediately.
            await self.source_commit(run, batch_id)
            return {'batchId': None, 'text': '', 'remainingConversations': batch['remaining'], 'done': not batch['remaining'],
                    'note': '没有新的对话内容。'}
        return {'batchId': batch_id if batch['conversations'] else None, 'text': batch['text'],
                # A resumed long message may have its heading on the previous
                # page/run. Keep its identity outside the sliced source text.
                'continuation': next(({'conversationUuid': c['conversationUuid'], 'offset': c['offset']}
                                      for c in batch['conversations'] if c.get('offset')), None),
                'conversations': len(batch['conversations']), 'remainingConversations': batch['remaining'],
                'done': not batch['conversations'],
                'note': '分析完这一批并写好画像后，调用 source.commit 确认，再读取下一批。' if batch['conversations'] else '没有新的对话材料。'}

    async def source_commit(self, run, batch_id):
        covered = self.batches.get(run['run_id'], {}).pop(batch_id, None)
        if covered is None:
            raise RecError('unknown_batch', 'batchId 无效或已提交', status=409)
        now = self.clock()
        async with self.db.write_transaction(label='recommendation-cursor') as conn:
            if run['kind'] == 'follow':
                await sources.commit_follow(conn, run['owner_chat_id'], covered, now)
            else:
                await sources.commit(conn, run['owner_chat_id'], covered, now)
        return {'committed': len(covered)}

    async def source_read(self, run, params):
        settings, _ = await self.settings_row(run['owner_chat_id'])
        result = await sources.read_conversation(self.db.conn, run['owner_chat_id'], str(params.get('conversationUuid') or ''),
                                                 settings, await self.rec_folder(run['owner_chat_id']),
                                                 offset=params.get('offset') or 0, max_chars=max(128, min(sources.BATCH_CHARS, sources.result_chars.get()) - 1024))
        if result is None:
            raise RecError('not_found', '会话不存在或不在推荐范围内', status=404)
        return result


def follow_conversation_id(owner, source_conversation):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'openbear:recommendation:follow:{owner}:{source_conversation}'))


async def folder_owned(conn, owner, folder_id):
    # one() closes its cursor; an unclosed LIMIT 1 cursor pins the shared reader snapshot.
    return bool(await one(conn, 'SELECT 1 FROM web_conversation_folders WHERE owner_chat_id=? AND folder_uuid=? LIMIT 1',
                          (owner, folder_id)))


def run_card(row):
    """Trusted presentation of the system message that starts a recommendation run."""
    return {'runId': row['run_id'], 'kind': row['kind'], 'trigger': row['trigger'],
            'startedAtMs': row['started_at_ms'], 'sourceConversationUuid': row['source_conversation_uuid'] or ''}


def run_public(row):
    return {'id': row['run_id'], 'kind': row['kind'], 'trigger': row['trigger'], 'status': row['status'],
            'error': row['error'], 'conversationUuid': row['conversation_uuid'],
            'sourceConversationUuid': row['source_conversation_uuid'], 'models': json.loads(row['models_json'] or '[]'),
            'costUsd': round(float(row['cost_usd'] or 0), 4), 'tokens': row['tokens'], 'modelCalls': row['model_calls'],
            'cards': row['cards'], 'localDate': row['local_date'], 'startedAtMs': row['started_at_ms'],
            'finishedAtMs': row['finished_at_ms'], 'notificationState': row['notification_state']}
