"""Recommendation tool: the only write path of a recommendation cycle."""
from __future__ import annotations

from pydantic import ValidationError

from app.recommendations import insights, store
from app.recommendations.contracts import RecError
from app.runtime.tool_result import ToolOutcome
from app.tools.base import current_tool_context
from app.webhooks.contracts import dumps

ACTIONS = {
    'status': ('本轮范围、预算、画像索引、近期卡片、反馈、权重、用户点过的问题、有变化的持续关注；'
               'misreads 是过去前提被推翻的卡片，每日整理还有 openedCards（待复核的已点开卡片）'),
    'source.next': '读取下一页新对话材料（每日：全部范围；跟随：只有来源会话）。长会话和单条长消息也会分页；每页 commit 后继续 next，直到 done=true',
    'source.commit': '{batchId}：确认这一批已经分析并写好画像，推进增量位置',
    'source.read': '{conversationUuid, offset?}：读取某个会话的完整可见原文（分页）',
    'profile.list': '{kind?}：完整画像条目（含依据）',
    'profile.upsert': ('{id?, kind, topic, category?, detail?, status?, signal?, location?, evidence?, lastMentioned?}：'
                       'kind=interest|ongoing|preference|background|habit；signal=explicit|repeated|weak；'
                       'status=active|paused|done|dropped|unknown（只用于 ongoing，可省略）；'
                       'lastMentioned 是用户本人最近一次提到的消息时间，按材料里的时间写（MM-DD HH:MM 或 YYYY-MM-DD HH:MM）。'
                       '一次写一条；同一 kind+topic 会更新原条目'),
    'profile.remove': '{id}：移除过时条目（不能移除用户亲手改过的条目）',
    'card.save': ('{lane, category, topic, title, body, reason, questions[], toCheck[]?, sources[], location, material, dedupeKey, follow?, newReason?, priority?}：'
                  'toCheck 是 1–3 个没能从原文确认、需要接手时先核对的具体问题（每个不超过 120 字），会显示在卡片上'),
    'card.review': ('{id, verdict, issue?}：复核一张已点开的卡片。verdict=held（站住了）|partial（部分不对）|misread（前提有误）；'
                    'partial/misread 必须在 issue 里用一句话写清看错了什么'),
    'card.list': '{since?, limit?}：近期卡片',
    'follow.list': '持续关注项及最近检查结果',
    'follow.resolve': '{id, changeToken, update?}：使用 status/follow.list 返回的 changeToken 处理完一件关注的事。update 用一两句话写这件事目前的最新进展（没有新进展也写“截至某日无新进展”），会显示在用户的持续关注里；同时清除待处理变化',
    'insights.get': ('跨会话的 OpenBear 运行统计和配置清单（只读，用于 OpenBear 建议）：模型错误、定时任务、触发器、Skill、'
                     '记忆（含已停用）、文档、目录（提示词长度、默认模型）、当前激活模板、画像里的习惯与偏好、他本人的纠正类消息'),
    'insights.locate': ('{query}：查一种做法是否已经写在某处——目录提示词、当前激活的主控/Agent 模板、记忆（含已停用）、文档、Skill 正文；'
                        '关键词空格分隔、全部命中才算，返回命中的位置和摘录（只读）'),
    'internal.search': '{query, limit?}：在用户自己的历史会话、记忆条目和文档里查关键词（空格分隔，全部命中才算）。用来找内部的相关事实、之前的结论、相关项目；只读，不含凭证',
}
DESCRIPTION = ('推荐模块的读写工具。只在推荐运行中可用。actions: '
               + '; '.join(f'{k} {v}' for k, v in ACTIONS.items()))


async def dispatch(service, action, params, ctx):
    run = await service.run_for_context(ctx)
    owner = run['owner_chat_id']
    now = service.clock()
    conn = service.db.conn
    if action == 'status':
        return await service.status(run)
    if action in ('source.next', 'source.read'):
        from app.recommendations.sources import result_chars
        if result_chars.get() < 1536:
            raise RecError('result_limit_too_small', '推荐来源分页需要至少 1536 字符的工具结果上限')
    if action == 'source.next':
        return await service.source_next(run)
    if action == 'source.commit':
        return await service.source_commit(run, str(params.get('batchId') or ''))
    if action == 'source.read':
        return await service.source_read(run, params)
    if action == 'profile.list':
        return {'items': await store.profile_list(conn, owner, kind=params.get('kind'))}
    if action == 'profile.upsert':
        settings, _ = await service.settings_row(owner)
        params = dict(params)
        when = service.parse_when(params.pop('lastMentioned', None) or params.get('lastMentionedMs'), settings)
        params['lastMentionedMs'] = when
        params['firstSeenMs'] = when
        async with service.db.write_transaction(label='recommendation-profile') as tx:
            item_id, result = await store.profile_upsert(tx, owner, params, now)
        return {'id': item_id, 'result': result}
    if action == 'profile.remove':
        async with service.db.write_transaction(label='recommendation-profile-remove') as tx:
            await store.profile_remove(tx, owner, str(params.get('id') or ''), now)
        return {'removed': True}
    if action == 'card.save':
        if run['kind'] == 'follow' and not (params.get('location') or {}).get('conversationUuid'):
            params = {**params, 'location': {**(params.get('location') or {}), 'conversationUuid': run['source_conversation_uuid']}}
        async with service.db.write_transaction(label='recommendation-card') as tx:
            card_id = await store.card_save(tx, owner, run, params, now)
        return {'id': card_id, 'saved': True}
    if action == 'card.review':
        card = await store.card_row(conn, owner, str(params.get('id') or ''))
        async with service.db.write_transaction(label='recommendation-card-review') as tx:
            await store.review(tx, owner, card, str(params.get('verdict') or ''), str(params.get('issue') or ''), now)
        return {'reviewed': True}
    if action == 'card.list':
        since = params.get('since')
        return await store.card_list(conn, owner, since_ms=int(since) if isinstance(since, (int, float)) else now - 14 * 86_400_000,
                                     limit=min(100, int(params.get('limit') or 50)))
    if action == 'follow.list':
        return {'items': await store.follows(conn, owner)}
    if action == 'follow.resolve':
        update = str(params.get('update') or '').strip()[:500]
        async with service.db.write_transaction(label='recommendation-follow-resolve') as tx:
            from app.webhooks.repository import one
            row = await one(tx, 'SELECT * FROM rec_follows WHERE follow_id=? AND owner_chat_id=? AND active=1',
                            (str(params.get('id') or ''), owner))
            if not row or params.get('changeToken') != store.follow_token(row):
                raise RecError('follow_changed', '关注已变化或未提供 changeToken，请重新读取后处理', status=409)
            await tx.execute("UPDATE rec_follows SET pending_change='',last_update=?,last_update_ms=? WHERE follow_id=?",
                             (update or row['last_update'], now if update else row['last_update_ms'], row['follow_id']))
        return {'resolved': True}
    if action in ('insights.get', 'insights.locate', 'internal.search'):
        settings, _ = await service.settings_row(owner)
    if action == 'insights.get':
        return await insights.summary(service.host, owner, now_ms=now, rec_folder=await service.rec_folder(owner), settings=settings)
    if action == 'insights.locate':
        return await insights.locate(service.host, owner, str(params.get('query') or ''), rec_folder=await service.rec_folder(owner), settings=settings)
    if action == 'internal.search':
        return await insights.internal_search(service.host, owner, str(params.get('query') or ''),
                                              rec_folder=await service.rec_folder(owner), now_ms=now, limit=params.get('limit') or 12, settings=settings)
    raise RecError('invalid_parameters', 'action 无效，可选：' + ', '.join(ACTIONS))


def register_recommendation_tool(registry, service):
    async def handler(args):
        try:
            params = args.get('params') or {}
            if not isinstance(params, dict):
                raise RecError('invalid_parameters', 'params 必须是对象')
            return dumps(await dispatch(service, str(args.get('action') or ''), params, current_tool_context()))
        except RecError as exc:
            return ToolOutcome(dumps(exc.payload), 'failed', 'not_started')
        except (ValidationError, ValueError, TypeError) as exc:
            return ToolOutcome(dumps({'code': 'invalid_parameters', 'message': str(exc)}), 'failed', 'not_started')
    registry.add('Recommendation', DESCRIPTION,
                 {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': list(ACTIONS)},
                                                   'params': {'type': 'object'}},
                  'required': ['action'], 'additionalProperties': False},
                 # Not advertised to ordinary conversations; recommendation runs add this scope.
                 handler, visibility={RUN_SCOPE}, effect_scope='recommendation_state')


RUN_SCOPE = 'recommendation'


class RecommendationRunTools:
    """Registry view for a recommendation run: main tools plus the run-only scope."""

    def __init__(self, registry):
        self.registry = registry
        self.file_state = getattr(registry, 'file_state', None)

    def __getattr__(self, name):
        return getattr(self.registry, name)

    def schemas(self, scope=None):
        schemas = list(self.registry.schemas(scope=scope))
        if scope == 'main':
            names = {s.get('name') for s in schemas}
            schemas += [s for s in self.registry.schemas(scope=RUN_SCOPE) if s.get('name') not in names]
        return schemas

    def names(self, scope=None, source=None):
        names = list(self.registry.names(scope=scope, source=source))
        if scope == 'main':
            names += [n for n in self.registry.names(scope=RUN_SCOPE, source=source) if n not in names]
        return names

    async def dispatch(self, name, arguments, **kwargs):
        from app.recommendations.sources import result_chars, BATCH_CHARS
        token = result_chars.set(min(BATCH_CHARS, int(kwargs.get('max_chars', BATCH_CHARS))))
        try:
            return await self.registry.dispatch(name, arguments, **kwargs)
        finally:
            result_chars.reset(token)
