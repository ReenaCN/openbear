"""Recommendation business state: profile, cards, feedback, weights and follows."""
from __future__ import annotations

import hashlib
import json
import math
import re

from app.recommendations.contracts import (
    CARD_STATES,
    CATEGORIES,
    FOLLOW_KINDS,
    LANES,
    PROFILE_KINDS,
    PROFILE_SIGNALS,
    PROFILE_STATUSES,
    RecError,
)
from app.webhooks.contracts import dumps
from app.webhooks.repository import insert, many, one, uid

HALF_LIFE_DAYS = 14
WEIGHT_CAP = 3.0
# Feedback → (topic delta, category delta). Silence is not negative evidence.
FEEDBACK_WEIGHTS = {
    'open_source': (0.15, 0.05),
    'ask': (0.25, 0.1),
    'sent': (0.6, 0.2),
    'continued': (1.0, 0.3),
    'follow': (0.5, 0.1),
    'unfollow': (-0.3, 0.0),
    'known': (0.0, -0.3),
    'resolved': (0.0, 0.0),
    'dismiss': (-1.5, -0.2),
    'adopted': (0.8, 0.3),
    'note': (0.0, 0.0),
}
# Review of an opened card → (topic delta, category delta). Engagement already credited the
# topic; a card whose premise did not hold must not keep pulling more of the same.
REVIEW_WEIGHTS = {
    'held': (0.0, 0.0),
    'partial': (-0.6, -0.1),
    'misread': (-1.8, -0.3),
}
STATE_FOR = {'ask': 'engaged', 'sent': 'sent', 'continued': 'continued', 'follow': 'followed', 'known': 'known',
             'resolved': 'resolved', 'dismiss': 'dismissed', 'adopted': 'adopted'}
STATE_RANK = {state: index for index, state in enumerate(
    ('new', 'seen', 'parked', 'engaged', 'followed', 'sent', 'continued', 'known', 'resolved', 'adopted', 'dismissed'))}


def text(value, limit, *, field='value', required=False):
    value = str(value or '').strip()
    if required and not value:
        raise RecError('invalid_parameters', f'{field} 不能为空')
    return value[:limit]


def topic_key(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().lower())[:200]


def profile_public(row):
    return {'id': row['item_id'], 'kind': row['kind'], 'topic': row['topic'], 'category': row['category'],
            'detail': row['detail'], 'status': row['status'], 'signal': row['signal'],
            'location': json.loads(row['location_json'] or '{}'), 'evidence': evidence_public(row['evidence_json']),
            'firstSeenMs': row['first_seen_ms'], 'lastMentionedMs': row['last_mentioned_ms'],
            'userEdited': bool(row['user_edited']), 'revision': row['revision'], 'updatedAtMs': row['updated_at_ms'],
            'removed': row['removed_at_ms'] is not None}


def card_public(row, *, material=False):
    out = {'id': row['card_id'], 'runId': row['run_id'], 'lane': row['lane'], 'category': row['category'],
           'topic': row['topic'], 'title': row['title'], 'body': row['body'], 'reason': row['reason'],
           'questions': json.loads(row['questions_json'] or '[]'), 'sources': json.loads(row['sources_json'] or '[]'),
           'location': json.loads(row['location_json'] or '{}'), 'originConversationUuid': row['origin_conversation_uuid'],
           'state': row['state'], 'createdAtMs': row['created_at_ms'], 'seenAtMs': row['seen_at_ms'],
           'dedupeKey': row['dedupe_key'], 'follow': json.loads(row['follow_json'] or '{}'),
           'toCheck': json.loads(row.get('to_check_json') or '[]'), 'review': row.get('review') or '',
           'reviewIssue': row.get('review_issue') or ''}
    if material:
        out['material'] = row['material']
    return out


def follow_token(row):
    # The pending value protects two checks finishing in the same millisecond.
    import hashlib
    fields = [row.get(k) for k in ('pending_change', 'last_changed_ms', 'last_value', 'active', 'note', 'last_update', 'last_update_ms')]
    return hashlib.sha256(dumps(fields).encode()).hexdigest()


def follow_public(row):
    return {'id': row['follow_id'], 'cardId': row['card_id'], 'topic': row['topic'], 'kind': row['kind'],
            'target': row['target'], 'note': row.get('note') or '', 'lastValue': row['last_value'], 'lastCheckedMs': row['last_checked_ms'],
            'lastChangedMs': row['last_changed_ms'], 'changeToken': follow_token(row), 'pendingChange': row['pending_change'], 'error': row['error'],
            'active': bool(row['active']), 'createdAtMs': row['created_at_ms'],
            'lastUpdate': row.get('last_update') or '', 'lastUpdateMs': row.get('last_update_ms') or 0}


def clean_evidence(value):
    """A list of short quotes. A bare string is ONE piece of evidence, never a list of characters."""
    if isinstance(value, str):
        value = [line for line in value.splitlines() if line.strip()] or [value]
    if not isinstance(value, list):
        return []
    return [text(x, 300) for x in value if isinstance(x, (str, int, float)) and str(x).strip()][:8]


def evidence_public(raw):
    """Rows saved before clean_evidence hold the first 8 characters of one split string;
    that fragment (e.g. "10-06~08") is meaningless, so it is not shown."""
    items = json.loads(raw or '[]')
    if items and all(isinstance(x, str) and len(x) <= 1 for x in items):
        return []
    return items


def clean_location(value):
    value = value if isinstance(value, dict) else {}
    return {k: text(value.get(k), 200) for k in ('conversationUuid', 'folderId', 'model') if value.get(k)}


def clean_sources(value):
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            url = text(item.get('url'), 1000)
            if url.startswith(('http://', 'https://')):
                out.append({'title': text(item.get('title') or url, 200), 'url': url})
        elif isinstance(item, str) and item.startswith(('http://', 'https://')):
            out.append({'title': item[:200], 'url': item[:1000]})
    return out[:8]


# ---------------------------------------------------------------- profile

async def profile_list(conn, owner, *, kind=None, include_removed=False):
    where, args = ['owner_chat_id=?'], [owner]
    if kind:
        where.append('kind=?')
        args.append(kind)
    if not include_removed:
        where.append('removed_at_ms IS NULL')
    rows = await many(conn, 'SELECT * FROM rec_profile WHERE ' + ' AND '.join(where)
                      + ' ORDER BY kind,last_mentioned_ms DESC,updated_at_ms DESC', args)
    return [profile_public(r) for r in rows]


async def profile_upsert(conn, owner, params, now, *, by_user=False):
    # PATCH omissions preserve fields; an explicit empty value still clears them.
    if params.get('id'):
        previous = await one(conn, 'SELECT * FROM rec_profile WHERE item_id=? AND owner_chat_id=?', (params['id'], owner))
        if previous:
            params = {**profile_public(previous), **params}
    kind = params.get('kind')
    if kind not in PROFILE_KINDS:
        raise RecError('invalid_parameters', 'kind 必须是 ' + '/'.join(PROFILE_KINDS))
    signal = params.get('signal') or 'weak'
    status = params.get('status') or ''
    if signal not in PROFILE_SIGNALS or status not in PROFILE_STATUSES:
        raise RecError('invalid_parameters', 'signal/status 取值无效')
    category = params.get('category') or ''
    if category and category not in CATEGORIES:
        category = ''
    values = {'kind': kind, 'topic': text(params.get('topic'), 200, field='topic', required=True),
              'category': category, 'detail': text(params.get('detail'), 2000), 'status': status, 'signal': signal,
              'location_json': dumps(clean_location(params.get('location'))),
              'evidence_json': dumps(clean_evidence(params.get('evidence')))}
    item_id = params.get('id')
    row = await one(conn, 'SELECT * FROM rec_profile WHERE item_id=? AND owner_chat_id=?', (item_id, owner)) if item_id else None
    if item_id and not row:
        raise RecError('not_found', '画像条目不存在', status=404)
    if row is None:
        row = await one(conn, 'SELECT * FROM rec_profile WHERE owner_chat_id=? AND kind=? AND lower(topic)=lower(?) AND (removed_at_ms IS NULL OR user_edited=1) ORDER BY (removed_at_ms IS NOT NULL AND user_edited=1) DESC,updated_at_ms DESC LIMIT 1',
                        (owner, kind, values['topic']))
    if not by_user and await one(conn, '''SELECT 1 FROM rec_profile WHERE owner_chat_id=? AND kind=? AND lower(topic)=lower(?)
            AND removed_at_ms IS NOT NULL AND user_edited=1 LIMIT 1''', (owner, kind, values['topic'])):
        raise RecError('removed_by_user', '该条目已被移除，不要原样加回', status=409)
    last = params.get('lastMentionedMs')
    last = int(last) if isinstance(last, (int, float)) and last > 0 else None
    if row:
        if params.get('expectedRevision') is not None and params['expectedRevision'] != row['revision']:
            raise RecError('revision_conflict', '画像已被修改，请重新读取', status=409, currentRevision=row['revision'])
        if row['removed_at_ms'] is not None and row['user_edited'] and not by_user:
            raise RecError('removed_by_user', '该条目已被移除，不要原样加回', status=409)
        if row['user_edited'] and not by_user:
            # The user's own correction wins; the model may only refresh recency/evidence.
            await conn.execute('UPDATE rec_profile SET last_mentioned_ms=MAX(last_mentioned_ms,?),evidence_json=?,updated_at_ms=? WHERE item_id=?',
                               (last or 0, values['evidence_json'], now, row['item_id']))
            return row['item_id'], 'kept_user_edit'
        if row['removed_at_ms'] is not None and not by_user:
            raise RecError('removed_by_user', '该条目已被移除，不要原样加回', status=409)
        await conn.execute('''UPDATE rec_profile SET kind=?,topic=?,category=?,detail=?,status=?,signal=?,location_json=?,evidence_json=?,
            last_mentioned_ms=MAX(last_mentioned_ms,?),user_edited=?,removed_at_ms=NULL,revision=revision+1,updated_at_ms=? WHERE item_id=?''',
                           (*values.values(), last or 0, int(by_user or row['user_edited']), now, row['item_id']))
        return row['item_id'], 'updated'
    first = params.get('firstSeenMs')
    # Unknown stays unknown: re-reading an old conversation is not a new mention.
    first = int(first) if isinstance(first, (int, float)) and first > 0 else (last or 0)
    item_id = uid()
    await insert(conn, 'rec_profile', item_id=item_id, owner_chat_id=owner, **values, first_seen_ms=first,
                 last_mentioned_ms=last or first, user_edited=int(by_user), created_at_ms=now, updated_at_ms=now)
    return item_id, 'created'


async def profile_remove(conn, owner, item_id, now, *, by_user=False):
    row = await one(conn, 'SELECT * FROM rec_profile WHERE item_id=? AND owner_chat_id=?', (item_id, owner))
    if not row:
        raise RecError('not_found', '画像条目不存在', status=404)
    if row['user_edited'] and not by_user:
        raise RecError('user_edited', '用户亲手修改过的条目不能由模型移除', status=409)
    await conn.execute('UPDATE rec_profile SET removed_at_ms=?,user_edited=?,revision=revision+1,updated_at_ms=? WHERE item_id=?',
                       (now, int(by_user or row['user_edited']), now, item_id))


# ---------------------------------------------------------------- cards

def dedupe_key(params):
    raw = params.get('dedupeKey') or (params.get('topic', '') + '|' + params.get('title', ''))
    return hashlib.sha256(topic_key(raw).encode()).hexdigest()[:32]


async def card_save(conn, owner, run, params, now):
    lane = params.get('lane') or 'external'
    category = params.get('category') or ('openbear' if lane == 'openbear' else 'update')
    if lane not in LANES or category not in CATEGORIES:
        raise RecError('invalid_parameters', 'lane/category 取值无效')
    questions = [text(q, 40) for q in (params.get('questions') or []) if str(q or '').strip()][:3]
    to_check = params.get('toCheck') or []
    to_check = [to_check] if isinstance(to_check, str) else to_check if isinstance(to_check, list) else []
    to_check = [text(q, 120) for q in to_check if isinstance(q, str) and q.strip()][:3]
    key = dedupe_key(params)
    previous = await one(conn, '''SELECT * FROM rec_cards WHERE owner_chat_id=? AND dedupe_key=? AND removed_at_ms IS NULL
        ORDER BY created_at_ms DESC LIMIT 1''', (owner, key))
    if previous and not params.get('newReason'):
        reason = {'dismissed': '用户标记不感兴趣', 'known': '用户已经知道', 'resolved': '用户标记已解决',
                  'parked': '推荐过一次未得到回应，已搁置'}.get(previous['state'], '同一件事已经推荐过')
        raise RecError('duplicate', f'不保存：{reason}。如确有实质不同的新理由，在 newReason 中写明后重试。',
                       status=409, previousCardId=previous['card_id'], previousState=previous['state'])
    follow = {}
    if isinstance(params.get('follow'), dict) and params['follow'].get('kind'):
        kind = params['follow']['kind']
        follow = {'kind': kind, 'target': follow_target(kind, params['follow'].get('target') or params.get('topic'))}
    card_id = uid()
    body = text(params.get('body'), 1200, field='body', required=True)
    if params.get('newReason'):
        body = body + '\n\n（再次推荐：' + text(params['newReason'], 200) + '）'
    await insert(conn, 'rec_cards', card_id=card_id, owner_chat_id=owner, run_id=run['run_id'], lane=lane, category=category,
                 topic=text(params.get('topic'), 200), dedupe_key=key, title=text(params.get('title'), 120, field='title', required=True),
                 body=body, reason=text(params.get('reason'), 400), questions_json=dumps(questions),
                 sources_json=dumps(clean_sources(params.get('sources'))), location_json=dumps(clean_location(params.get('location'))),
                 material=text(params.get('material'), 20000), follow_json=dumps(follow), to_check_json=dumps(to_check),
                 origin_conversation_uuid=run.get('source_conversation_uuid') or '',
                 priority=int(params.get('priority') or 0), created_at_ms=now)
    if previous:
        await conn.execute("UPDATE rec_cards SET state='parked' WHERE card_id=? AND state IN ('new','seen')", (previous['card_id'],))
    return card_id


async def card_list(conn, owner, *, run_id=None, since_ms=None, state=None, lane=None, limit=50, offset=0, material=False, unread=False, unread_first=False):
    where, args = ['owner_chat_id=?', 'removed_at_ms IS NULL'], [owner]
    if unread:
        where.append('seen_at_ms IS NULL')
    for column, value in (('run_id', run_id), ('state', state), ('lane', lane)):
        if value:
            where.append(column + '=?')
            args.append(value)
    if since_ms:
        where.append('created_at_ms>=?')
        args.append(since_ms)
    clause = ' AND '.join(where)
    total = (await one(conn, 'SELECT COUNT(*) n FROM rec_cards WHERE ' + clause, args))['n']
    # The feed orders the complete result before pagination; model/tool callers
    # retain their chronological list unless they explicitly request feed order.
    order = 'created_at_ms DESC,priority DESC'
    if unread_first:
        order = '(seen_at_ms IS NOT NULL) ASC,' + order + ',card_id DESC'
    rows = await many(conn, 'SELECT * FROM rec_cards WHERE ' + clause + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?',
                      (*args, limit, offset))
    return {'items': [card_public(r, material=material) for r in rows], 'total': total}


async def park_unanswered(conn, owner, now, *, older_than_ms=36 * 3_600_000):
    """One offer, no reply → park it; the dedupe key then blocks re-offers."""
    await conn.execute("UPDATE rec_cards SET state='parked' WHERE owner_chat_id=? AND state IN ('new','seen') AND created_at_ms<?",
                       (owner, now - older_than_ms))


# ---------------------------------------------------------------- feedback & weights

async def feedback(conn, owner, card, action, now, *, detail='', conversation_uuid='', request_id=''):
    if action not in FEEDBACK_WEIGHTS:
        raise RecError('invalid_parameters', 'action 无效')
    if request_id and await one(conn, 'SELECT 1 FROM rec_feedback WHERE owner_chat_id=? AND request_id=?', (owner, request_id)):
        return False
    await insert(conn, 'rec_feedback', owner_chat_id=owner, card_id=card['card_id'], action=action, detail=text(detail, 2000),
                 conversation_uuid=conversation_uuid, request_id=request_id, created_at_ms=now)
    # Acting on a card is reading it; merely listing it is not.
    await conn.execute('UPDATE rec_cards SET seen_at_ms=COALESCE(seen_at_ms,?) WHERE card_id=?', (now, card['card_id']))
    topic_delta, category_delta = FEEDBACK_WEIGHTS[action]
    if card['topic'] and topic_delta:
        await bump_weight(conn, owner, 'topic', topic_key(card['topic']), topic_delta, now)
    if category_delta:
        await bump_weight(conn, owner, 'category', card['category'], category_delta, now)
    new_state = STATE_FOR.get(action)
    if action == 'unfollow':
        new_state = 'engaged' if card['state'] == 'followed' else None
    if new_state and (STATE_RANK.get(new_state, 0) >= STATE_RANK.get(card['state'], 0) or action in ('dismiss', 'resolved', 'known', 'unfollow')):
        await conn.execute('UPDATE rec_cards SET state=? WHERE card_id=?', (new_state, card['card_id']))
    if action == 'resolved' and card['topic']:
        await conn.execute("""UPDATE rec_profile SET status='done',revision=revision+1,updated_at_ms=? WHERE owner_chat_id=? AND kind='ongoing'
            AND lower(topic)=lower(?) AND removed_at_ms IS NULL""", (now, owner, card['topic']))
    return True


async def review(conn, owner, card, verdict, issue, now):
    """The recommendation model's later verdict on whether an opened card's premise held."""
    if verdict not in REVIEW_WEIGHTS:
        raise RecError('invalid_parameters', 'verdict 必须是 ' + '/'.join(REVIEW_WEIGHTS))
    issue = text(issue, 400)
    if verdict != 'held' and not issue:
        raise RecError('invalid_parameters', 'partial/misread 需要在 issue 里写清看错了什么')
    # Re-reviewing replaces the earlier verdict; its weight is reverted first.
    if card.get('review') in REVIEW_WEIGHTS:
        old_topic, old_category = REVIEW_WEIGHTS[card['review']]
        if card['topic'] and old_topic:
            await bump_weight(conn, owner, 'topic', topic_key(card['topic']), -old_topic, now)
        if old_category:
            await bump_weight(conn, owner, 'category', card['category'], -old_category, now)
    await conn.execute('UPDATE rec_cards SET review=?,review_issue=?,reviewed_at_ms=? WHERE card_id=?',
                       (verdict, issue, now, card['card_id']))
    topic_delta, category_delta = REVIEW_WEIGHTS[verdict]
    if card['topic'] and topic_delta:
        await bump_weight(conn, owner, 'topic', topic_key(card['topic']), topic_delta, now)
    if category_delta:
        await bump_weight(conn, owner, 'category', card['category'], category_delta, now)


async def opened_unreviewed(conn, owner, since_ms, limit=20):
    """Cards the user carried into a conversation, with the conversations to read for the review."""
    rows = await many(conn, '''SELECT c.card_id,c.title,c.topic,c.category,c.body,c.reason,c.to_check_json,
          GROUP_CONCAT(DISTINCT f.conversation_uuid) conversations, MAX(f.created_at_ms) last_ms
        FROM rec_cards c JOIN rec_feedback f ON f.card_id=c.card_id AND f.owner_chat_id=c.owner_chat_id
        WHERE c.owner_chat_id=? AND c.review='' AND c.removed_at_ms IS NULL AND f.action IN ('sent','continued')
          AND f.conversation_uuid<>'' AND f.created_at_ms>=?
        GROUP BY c.card_id ORDER BY last_ms DESC LIMIT ?''', (owner, since_ms, limit))
    return [{'id': r['card_id'], 'title': r['title'], 'topic': r['topic'], 'body': r['body'], 'reason': r['reason'],
             'toCheck': json.loads(r['to_check_json'] or '[]'),
             'conversations': [c for c in (r['conversations'] or '').split(',') if c]} for r in rows]


async def misreads(conn, owner, since_ms, limit=20):
    rows = await many(conn, '''SELECT title,topic,category,review,review_issue,reviewed_at_ms FROM rec_cards
        WHERE owner_chat_id=? AND review IN ('misread','partial') AND reviewed_at_ms>=? ORDER BY reviewed_at_ms DESC LIMIT ?''',
                      (owner, since_ms, limit))
    return [{'card': r['title'], 'topic': r['topic'], 'verdict': r['review'], 'issue': r['review_issue']} for r in rows]


def decayed(weight, updated_ms, now):
    days = max(0.0, (now - updated_ms) / 86_400_000)
    return weight * math.pow(0.5, days / HALF_LIFE_DAYS)


async def bump_weight(conn, owner, scope, name, delta, now):
    if not name:
        return
    row = await one(conn, 'SELECT * FROM rec_weights WHERE owner_chat_id=? AND scope=? AND name=?', (owner, scope, name))
    current = decayed(row['weight'], row['updated_at_ms'], now) if row else 0.0
    value = max(-WEIGHT_CAP, min(WEIGHT_CAP, current + delta))
    await conn.execute('''INSERT INTO rec_weights(owner_chat_id,scope,name,weight,updated_at_ms) VALUES(?,?,?,?,?)
        ON CONFLICT(owner_chat_id,scope,name) DO UPDATE SET weight=excluded.weight,updated_at_ms=excluded.updated_at_ms''',
                       (owner, scope, name, value, now))


async def weights(conn, owner, now):
    rows = await many(conn, 'SELECT * FROM rec_weights WHERE owner_chat_id=?', (owner,))
    out = {'topic': {}, 'category': {}}
    for r in rows:
        value = round(decayed(r['weight'], r['updated_at_ms'], now), 2)
        if abs(value) >= 0.05:
            out.setdefault(r['scope'], {})[r['name']] = value
    return out


async def recent_feedback(conn, owner, since_ms, limit=60):
    rows = await many(conn, '''SELECT f.card_id,f.action,f.detail,f.created_at_ms,c.title,c.topic,c.category,c.lane,c.review FROM rec_feedback f
        JOIN rec_cards c ON c.card_id=f.card_id WHERE f.owner_chat_id=? AND f.created_at_ms>=? ORDER BY f.created_at_ms DESC LIMIT ?''',
                      (owner, since_ms, limit))
    return [{'cardId': r['card_id'], 'action': r['action'], 'detail': r['detail'], 'atMs': r['created_at_ms'], 'card': r['title'],
             'topic': r['topic'], 'category': r['category'], 'lane': r['lane'], 'reviewed': bool(r['review'])} for r in rows]


async def asked_questions(conn, owner, limit=20):
    rows = await many(conn, "SELECT detail FROM rec_feedback WHERE owner_chat_id=? AND action='ask' AND detail<>'' ORDER BY created_at_ms DESC LIMIT ?",
                      (owner, limit))
    return [r['detail'] for r in rows]


# ---------------------------------------------------------------- follows

def follow_target(kind, target):
    target = text(target, 500, field='target', required=True)
    if kind not in FOLLOW_KINDS:
        raise RecError('invalid_parameters', 'follow kind 无效，可选 ' + '/'.join(FOLLOW_KINDS))
    if kind == 'topic':
        return target[:200]
    if kind in ('github_release', 'github_commits') and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', target):
        raise RecError('invalid_parameters', 'GitHub 目标格式为 owner/repo')
    if kind == 'npm' and not re.fullmatch(r'(@[a-z0-9._-]+/)?[a-z0-9._-]+', target):
        raise RecError('invalid_parameters', 'npm 包名无效')
    if kind == 'webpage' and not target.startswith('https://'):
        raise RecError('invalid_parameters', '网页关注只支持 https 地址')
    return target


async def follow_add(conn, owner, card_id, topic, kind, target, now, *, note=None):
    """Follow the MATTER itself (kind=topic). A repo/package/page is only an optional extra signal.
    note=None keeps an existing note; '' clears it."""
    target = follow_target(kind, target)
    existing = await one(conn, 'SELECT * FROM rec_follows WHERE owner_chat_id=? AND kind=? AND target=?', (owner, kind, target))
    if existing:
        await conn.execute("UPDATE rec_follows SET active=1,card_id=COALESCE(NULLIF(?,''),card_id) WHERE follow_id=?",
                           (card_id or '', existing['follow_id']))
        if note is not None:
            await conn.execute('UPDATE rec_follows SET note=? WHERE follow_id=?', (text(note, 500), existing['follow_id']))
        return existing['follow_id']
    follow_id = uid()
    await insert(conn, 'rec_follows', follow_id=follow_id, owner_chat_id=owner, card_id=card_id or '', topic=text(topic, 200),
                 kind=kind, target=target, note=text(note or '', 500), created_at_ms=now)
    return follow_id


async def follows(conn, owner, *, active_only=True):
    rows = await many(conn, 'SELECT * FROM rec_follows WHERE owner_chat_id=?' + (' AND active=1' if active_only else '')
                      + ' ORDER BY created_at_ms DESC', (owner,))
    return [follow_public(r) for r in rows]


async def card_row(conn, owner, card_id):
    row = await one(conn, 'SELECT * FROM rec_cards WHERE card_id=? AND owner_chat_id=?', (card_id, owner))
    if not row:
        raise RecError('not_found', '推荐卡片不存在', status=404)
    return row


__all__ = ['CARD_STATES']
