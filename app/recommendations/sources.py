"""Incremental conversation material for recommendation cycles.

Only real human turns (and the assistant replies that answer them) count. The
recommendation folder itself, Cron/Webhook/automatic inputs, hidden/internal
operations and unfinished messages never enter the material. Cursors advance
only after the model commits a batch it actually analyzed. A content digest of
the analyzed prefix detects later edits/deletions; the conversation revision is
only a cheap prefilter (it also moves for every new message).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.webhooks.repository import many, one

USER_CUT = 400
ASSIST_CUT = 160
LAST_ASSIST_CUT = 500
BATCH_CHARS = 24_000
# Escaped JSON budget; the tool wrapper lowers this to the actual runtime cap.
from contextvars import ContextVar
result_chars = ContextVar("recommendation_result_chars", default=BATCH_CHARS)
CONTEXT_TURNS = 2
# A follow reads one conversation and judges the user's situation from it: the assistant's
# conclusions carry what was found, decided and rejected, so they must not shrink to a headline.
FOLLOW_CUTS = {'user': 1200, 'assistant': 1500, 'last': 4000, 'context_user': 600, 'context_assistant': 800}
FOLLOW_CONTEXT_TURNS = 4
HUMAN_SOURCES = {'', 'user', 'web', 'telegram', 'queued_steering'}
AUTOMATIC_SOURCES = {'cron', 'webhook', 'recommendation', 'agent', 'system'}
_SCAFFOLD = ('[protocol: role-alternation bridge]',)


def clip(text, n):
    text = re.sub(r'\s*\n\s*', ' ⏎ ', str(text or '').strip())
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text if len(text) <= n else text[:n].rstrip() + f' …(+{len(text) - n}字)'


def _payload(row):
    try:
        value = json.loads(row['payload_json'] or '{}')
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


async def automatic_roots(conn, owner):
    roots = set()
    for sql in ("SELECT root_turn_uuid FROM cron_runs WHERE owner_chat_id=?",
                "SELECT a.root_turn_uuid FROM webhook_assignments a JOIN web_conversations c ON c.conversation_uuid=a.conversation_uuid "
                "WHERE c.owner_chat_id=? AND a.origin_kind='webhook'",
                "SELECT root_turn_uuid FROM rec_runs WHERE owner_chat_id=?"):
        try:
            roots.update(r['root_turn_uuid'] for r in await many(conn, sql, (owner,)))
        except Exception:  # A reduced schema may lack an optional subsystem.
            continue
    return roots


async def visible_messages(conn, owner, conversation_uuid, *, auto_roots=None):
    """Human-owned visible messages, oldest first."""
    rows = await many(conn, '''SELECT o.*, EXISTS(SELECT 1 FROM web_hidden_operations h
          WHERE h.conversation_uuid=o.conversation_uuid AND h.op_id=o.op_id) AS user_hidden
        FROM web_operations o WHERE o.conversation_uuid=?
          AND o.op_type IN ('user_message','assistant_message') ORDER BY o.display_seq,o.id''', (conversation_uuid,))
    roots = auto_roots if auto_roots is not None else await automatic_roots(conn, owner)
    out, human_turns = [], set()
    for row in rows:
        payload = _payload(row)
        if row['internal'] or row['user_hidden'] or payload.get('internal') or payload.get('hidden'):
            continue
        if row['lifecycle'] and row['lifecycle'] != 'terminal':
            continue
        text = payload.get('text') or payload.get('summary') or ''
        if not isinstance(text, str) or not text.strip() or text.strip() in _SCAFFOLD \
                or text.lstrip().startswith('<openbear-task-memory-state'):
            continue
        turn = row['run_root_turn_uuid'] or row['turn_uuid']
        role = 'user' if row['op_type'] == 'user_message' else 'assistant'
        if role == 'user':
            sources = {str(row['source'] or ''), str(payload.get('source') or '')}
            automatic = (turn in roots or payload.get('eventTriggered') or payload.get('cronCard') or payload.get('webhookCard')
                         or payload.get('cronRunId') or payload.get('assignmentId')
                         or sources & AUTOMATIC_SOURCES)
            if automatic or not sources <= HUMAN_SOURCES:
                continue
            human_turns.add(turn)
        elif turn not in human_turns:
            continue
        out.append({'role': role, 'text': user_voice(text, payload) if role == 'user' else text,
                    'ms': int(row['created_at_ms']), 'turn': turn, 'seq': int(row['display_seq'])})
    return out


def user_voice(text, payload):
    """Recommendation-card material attached to a message is context, not the user's words."""
    for ref in payload.get('references') or []:
        if isinstance(ref, dict) and ref.get('kind') == 'rec':
            label = str(ref.get('label') or '')
            text = re.sub(r'\[' + re.escape(label) + r'\]\(openbear://ref/rec/[^)]*\)', '', text) if label else text
    text = re.sub(r'\[[^\]\n]*\]\(openbear://ref/rec/[^)]*\)', '', text)
    return text.strip()


def prefix_digest(messages, last_seq):
    data = [(m['seq'], m['role'], m['text']) for m in messages if m['seq'] <= last_seq]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def in_scope(folder_id, settings, rec_folder):
    folder_id = str(folder_id or '')
    if rec_folder and folder_id == rec_folder:
        return False
    if settings.scope_mode == 'include':
        return folder_id in settings.scope_folders
    if settings.scope_mode == 'exclude':
        return folder_id not in settings.scope_folders
    return True


async def scoped_conversations(conn, owner, settings, rec_folder, *, since_ms=0, words=()):
    """Coarse indexed selection only; visible_messages remains the material boundary."""
    sql = 'SELECT c.* FROM web_conversations c WHERE c.owner_chat_id=?'
    args = [owner]
    if words:
        matching = ' OR '.join("(lower(c.title) LIKE ? OR lower(json_extract(o.payload_json,'$.text')) LIKE ?)" for _ in words)
        sql += f" AND EXISTS(SELECT 1 FROM web_operations o WHERE o.conversation_uuid=c.conversation_uuid AND o.created_at_ms>=? AND o.op_type IN ('user_message','assistant_message') AND ({matching}))"
        args += [since_ms, *(value for word in words for value in (f'%{word.lower()}%',) * 2)]
    rows = await many(conn, sql, args)
    return [r for r in rows if in_scope(r['folder_uuid'], settings, rec_folder)]


async def candidates(conn, owner, settings, rec_folder, *, since_ms, only=None):
    """Conversations whose content may have changed since their cursor."""
    rows = await many(conn, '''SELECT c.conversation_uuid,c.title,c.model,c.folder_uuid,c.reference_revision,
          f.name folder_name,k.last_op_seq,k.reference_revision cursor_revision,k.digest,k.daily_page_json,
          (SELECT MAX(o.created_at_ms) FROM web_operations o WHERE o.conversation_uuid=c.conversation_uuid
             AND o.op_type='user_message') last_user_ms
        FROM web_conversations c
        LEFT JOIN web_conversation_folders f ON f.folder_uuid=c.folder_uuid
        LEFT JOIN rec_cursors k ON k.owner_chat_id=c.owner_chat_id AND k.conversation_uuid=c.conversation_uuid
        WHERE c.owner_chat_id=? AND (? IS NULL OR c.conversation_uuid=?)
        ORDER BY COALESCE(c.last_interaction_at_ms,0), c.id''', (owner, only, only))
    out = []
    for row in rows:
        if not in_scope(row['folder_uuid'], settings, rec_folder):
            continue
        if row['last_op_seq'] is None:
            if (row['last_user_ms'] or 0) >= since_ms:
                out.append(row)
        elif row['digest'] == '' and (row['last_user_ms'] or 0) >= since_ms:
            out.append(row)  # Only follow-lane state exists; the daily lane has not read it yet.
        elif row['daily_page_json'] or row['reference_revision'] != row['cursor_revision']:
            out.append(row)
    return out


def _fmt(ms, tz, pattern='%m-%d %H:%M'):
    return datetime.fromtimestamp(ms / 1000, tz).strftime(pattern)


def _render(row, new, context, tz, *, edited, cuts=None):
    cuts = cuts or {'user': USER_CUT, 'assistant': ASSIST_CUT, 'last': LAST_ASSIST_CUT, 'context_user': 200,
                    'context_assistant': ASSIST_CUT}
    closing = {}
    for m in context + new:
        if m['role'] == 'assistant':
            closing[m['turn']] = m
    last_assistant = next((m for m in reversed(new) if m['role'] == 'assistant'), None)
    users = [m for m in new if m['role'] == 'user']
    note = ('｜会话内容有编辑或删除，以下为重新读取的版本' if edited else
            '｜本批只有上一轮的后续回复' if not users else '')
    lines = [f"## 会话 {row['conversation_uuid']}｜{row['title'] or '(无标题)'}",
             f"目录：{row['folder_name'] or '(临时会话)'}（folderId={row['folder_uuid'] or ''}）｜模型：{row['model'] or '(默认)'}｜"
             f"{_fmt(new[0]['ms'], tz)} → {_fmt(new[-1]['ms'], tz)}｜用户消息 {len(users)} 条{note}"]
    if context:
        lines.append('（之前的上下文）')
        for m in context:
            if m['role'] == 'user':
                lines.append(f"- [{_fmt(m['ms'], tz)}] 用户：{clip(m['text'], cuts['context_user'])}")
            elif closing.get(m['turn']) is m:
                lines.append(f"  - 助手：{clip(m['text'], cuts['context_assistant'])}")
        lines.append('（新内容）')
    for m in new:
        if m['role'] == 'user':
            lines.append(f"- [{_fmt(m['ms'], tz)}] **用户**：{clip(m['text'], cuts['user'])}")
        elif closing.get(m['turn']) is m:
            lines.append(f"  - 助手：{clip(m['text'], cuts['last'] if m is last_assistant else cuts['assistant'])}")
    if any('…(+' in line for line in lines):
        lines.append('（以上是摘录，标“…(+N字)”的已截短；要判断他的情况时，用 source.read 读原文。）')
    return '\n'.join(lines) + '\n'


def bounded_text(text, max_chars):
    """Bound the JSON representation, not just raw text (quotes/control bytes escape)."""
    lo, hi = 0, min(len(text), max_chars)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(json.dumps(text[:mid], ensure_ascii=False)) <= max_chars:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _page(row, messages, new, earlier, state, *, tz, max_chars, edited=False):
    # Pin the page's end while new messages arrive. Edits/hiding reset the page,
    # never cause an offset in old text to skip different newly-visible text.
    end = int(state.get('endSeq') or max((m['seq'] for m in messages), default=0))
    new = [m for m in new if m['seq'] <= end]
    parts = [f"## 会话 {row['conversation_uuid']}｜{row['title'] or '(无标题)'}\n"
             f"目录：{row['folder_name'] or '(临时会话)'}（folderId={row['folder_uuid'] or ''}）｜模型：{row['model'] or '(默认)'}\n"]
    if edited:
        parts.append('会话内容有编辑或删除，以下为重新读取的版本\n')
    # Context is explicitly an excerpt; all NEW messages below are paged in full.
    if earlier:
        parts.append('（之前的上下文摘录）\n')
        for m in earlier[-4:]:
            parts.append(f"{m['role']}：{clip(m['text'], 400)}\n")
    parts.append('（新内容原文，长消息可跨页；提交本页后继续 source.next）\n')
    for m in new:
        parts.append(f"[{_fmt(m['ms'], tz)}] {'用户' if m['role'] == 'user' else '助手'}：{m['text']}\n")
    full = ''.join(parts)
    signature = hashlib.sha256(full.encode()).hexdigest()
    offset = int(state.get('offset') or 0) if state.get('signature') == signature else 0
    chunk = bounded_text(full[offset:], max_chars)
    next_offset = offset + len(chunk)
    page = {'endSeq': end, 'signature': signature, 'offset': next_offset} if next_offset < len(full) else {}
    return chunk, page, end, offset


async def build_batch(conn, owner, settings, rec_folder, *, since_ms, only=None, max_chars=BATCH_CHARS):
    """A bounded page, with only delivered material eligible for cursor commit."""
    roots = await automatic_roots(conn, owner)
    rows = await candidates(conn, owner, settings, rec_folder, since_ms=since_ms, only=only)
    covered, blocks, used = [], [], 0
    for index, row in enumerate(rows):
        if max_chars - used < 256:
            return {'text': '\n'.join(blocks), 'conversations': covered, 'remaining': len(rows) - index,
                    'chars': used, 'analyzable': len(blocks)}
        messages = await visible_messages(conn, owner, row['conversation_uuid'], auto_roots=roots)
        state = json.loads(row['daily_page_json'] or '{}')
        since = state.get('sinceMs', since_ms)
        last_seq = row['last_op_seq']
        edited = last_seq is not None and bool(row['digest']) and prefix_digest(messages, last_seq) != row['digest']
        new = [m for m in messages if m['ms'] >= since] if last_seq is None or edited or not row['digest'] else [m for m in messages if m['seq'] > last_seq]
        earlier = [m for m in messages if m not in new]
        top = max((m['seq'] for m in messages), default=last_seq or 0)
        chunk, page, offset = '', {}, 0
        if new:
            chunk, page, top, offset = _page(row, messages, new, earlier, state, tz=ZoneInfo(settings.timezone),
                                     max_chars=max_chars - used, edited=edited)
            if page:
                page['sinceMs'] = since
        target = {'conversationUuid': row['conversation_uuid'], 'seq': top, 'revision': row['reference_revision'] if top >= max((m['seq'] for m in messages), default=0) else -1,
                  'digest': prefix_digest(messages, top), 'page': page, 'offset': offset,
                  'expectedPage': row['daily_page_json'] or '', 'expectedSeq': last_seq or 0,
                  'lastUserMs': max((m['ms'] for m in messages if m['role'] == 'user'), default=0)}
        covered.append(target)
        if chunk:
            blocks.append(chunk)
            used += len(json.dumps(chunk, ensure_ascii=False)) + 2
            if page or offset:
                return {'text': '\n'.join(blocks), 'conversations': covered, 'remaining': len(rows) - index - 1 + bool(page),
                        'chars': used, 'analyzable': len(blocks)}
    return {'text': '\n'.join(blocks), 'conversations': covered, 'remaining': 0, 'chars': used, 'analyzable': len(blocks)}


async def read_conversation(conn, owner, conversation_uuid, settings, rec_folder, *, offset=0, max_chars=BATCH_CHARS):
    row = await one(conn, '''SELECT c.*,f.name folder_name FROM web_conversations c LEFT JOIN web_conversation_folders f
        ON f.folder_uuid=c.folder_uuid WHERE c.conversation_uuid=? AND c.owner_chat_id=?''', (conversation_uuid, owner))
    if not row or not in_scope(row['folder_uuid'], settings, rec_folder):
        return None
    tz = ZoneInfo(settings.timezone)
    msgs = await visible_messages(conn, owner, conversation_uuid)
    parts = [f"# {row['title'] or '(无标题)'}\n目录：{row['folder_name'] or '(临时会话)'}｜模型：{row['model'] or '(默认)'}｜会话 {conversation_uuid}\n"]
    for m in msgs:
        parts.append(f"### [{_fmt(m['ms'], tz, '%Y-%m-%d %H:%M')}] {'用户' if m['role'] == 'user' else '助手'}\n{m['text'].strip()}\n")
    full = '\n'.join(parts)
    offset = max(0, int(offset or 0))
    chunk = bounded_text(full[offset:], max_chars)
    return {'text': chunk, 'offset': offset, 'nextOffset': offset + len(chunk) if offset + len(chunk) < len(full) else None,
            'totalChars': len(full)}


async def commit(conn, owner, covered, now_ms):
    from app.recommendations.contracts import RecError
    for item in covered:
        row = await one(conn, 'SELECT * FROM rec_cursors WHERE owner_chat_id=? AND conversation_uuid=?', (owner, item['conversationUuid']))
        if 'expectedPage' in item and ((row or {}).get('daily_page_json', '') != item['expectedPage'] or
                                      int((row or {}).get('last_op_seq') or 0) != item['expectedSeq']):
            raise RecError('stale_batch', '来源游标已变化，请重新读取', status=409)
        if item.get('page'):
            await conn.execute("""INSERT INTO rec_cursors(owner_chat_id,conversation_uuid,daily_page_json) VALUES(?,?,?)
                ON CONFLICT(owner_chat_id,conversation_uuid) DO UPDATE SET daily_page_json=excluded.daily_page_json""",
                (owner, item['conversationUuid'], json.dumps(item['page'])))
        else:
            await conn.execute("""INSERT INTO rec_cursors(owner_chat_id,conversation_uuid,last_op_seq,reference_revision,digest,last_user_at_ms,analyzed_at_ms)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_chat_id,conversation_uuid) DO UPDATE SET
                last_op_seq=excluded.last_op_seq,reference_revision=excluded.reference_revision,digest=excluded.digest,daily_page_json='',
                last_user_at_ms=MAX(rec_cursors.last_user_at_ms,excluded.last_user_at_ms),analyzed_at_ms=excluded.analyzed_at_ms""",
                (owner, item['conversationUuid'], int(item['seq']), int(item['revision']), item['digest'], int(item.get('lastUserMs') or 0), now_ms))


FOLLOW_WINDOW_MS = 6 * 3_600_000


async def follow_candidates(conn, owner, settings, rec_folder, *, now_ms):
    """Conversations the user is actively discussing: enough new messages, a short pause."""
    rows = await many(conn, '''SELECT c.conversation_uuid,c.folder_uuid,c.status,COALESCE(k.follow_seq,0) follow_seq,
          COALESCE(k.follow_at_ms,0) follow_at_ms,COALESCE(k.follow_page_json,'') follow_page_json,
          COUNT(o.id) fresh, MAX(o.created_at_ms) last_ms
        FROM web_conversations c
        LEFT JOIN web_operations o ON o.conversation_uuid=c.conversation_uuid AND o.op_type='user_message'
          AND COALESCE(o.internal,0)=0 AND COALESCE(o.source,'') IN ('','user','web','telegram','queued_steering')
        LEFT JOIN rec_cursors k ON k.owner_chat_id=c.owner_chat_id AND k.conversation_uuid=c.conversation_uuid
        WHERE c.owner_chat_id=? AND COALESCE(c.archived_at,0)=0 AND ((o.created_at_ms>=?
          AND o.display_seq>COALESCE(k.follow_seq,0)) OR COALESCE(k.follow_page_json,'')<>'')
        GROUP BY c.conversation_uuid''', (owner, now_ms - FOLLOW_WINDOW_MS))
    roots = await automatic_roots(conn, owner)
    out = []
    for row in rows:
        if not in_scope(row['folder_uuid'], settings, rec_folder):
            continue
        if not row['follow_page_json'] and row['fresh'] < settings.follow_min_new_user_messages:
            continue
        if now_ms - (row['last_ms'] or 0) < settings.follow_idle_minutes * 60_000:
            continue
        if now_ms - row['follow_at_ms'] < settings.follow_cooldown_minutes * 60_000:
            continue
        human = [m for m in await visible_messages(conn, owner, row['conversation_uuid'], auto_roots=roots)
                 if m['role'] == 'user' and m['seq'] > row['follow_seq'] and m['ms'] >= now_ms - FOLLOW_WINDOW_MS]
        if row['follow_page_json'] or len(human) >= settings.follow_min_new_user_messages:
            out.append({**row, 'humanCount': len(human)})
    return sorted(out, key=lambda r: r['last_ms'] or 0, reverse=True)


async def build_follow_batch(conn, owner, settings, conversation_uuid, *, now_ms, from_seq=None, rec_folder='', max_chars=BATCH_CHARS):
    row = await one(conn, """SELECT c.conversation_uuid,c.title,c.model,c.folder_uuid,c.reference_revision,f.name folder_name,
          COALESCE(k.follow_seq,0) follow_seq,COALESCE(k.follow_page_json,'') follow_page_json FROM web_conversations c
        LEFT JOIN web_conversation_folders f ON f.folder_uuid=c.folder_uuid
        LEFT JOIN rec_cursors k ON k.owner_chat_id=c.owner_chat_id AND k.conversation_uuid=c.conversation_uuid
        WHERE c.conversation_uuid=? AND c.owner_chat_id=?""", (conversation_uuid, owner))
    if not row or not in_scope(row['folder_uuid'], settings, rec_folder):
        return {'text': '', 'conversations': [], 'remaining': 0, 'chars': 0, 'analyzable': 0}
    messages = await visible_messages(conn, owner, conversation_uuid)
    state = json.loads(row['follow_page_json'] or '{}')
    start = max(row['follow_seq'], int(from_seq or 0))
    since = state.get('sinceMs', now_ms - FOLLOW_WINDOW_MS)
    new = [m for m in messages if m['seq'] > start and m['ms'] >= since]
    earlier = [m for m in messages if m not in new]
    top = max((m['seq'] for m in messages), default=start)
    chunk, page, offset = '', {}, 0
    if new:
        chunk, page, top, offset = _page(row, messages, new, earlier, state, tz=ZoneInfo(settings.timezone), max_chars=max_chars)
        if page:
            page['sinceMs'] = since
    target = {'conversationUuid': conversation_uuid, 'followSeq': top, 'page': page, 'offset': offset,
              'expectedPage': row['follow_page_json'], 'expectedSeq': row['follow_seq']}
    return {'text': chunk, 'conversations': [target] if new or state or top > row['follow_seq'] else [],
            'remaining': int(bool(page)), 'chars': len(chunk), 'analyzable': int(bool(chunk))}


async def commit_follow(conn, owner, covered, now_ms):
    from app.recommendations.contracts import RecError
    for item in covered:
        row = await one(conn, 'SELECT * FROM rec_cursors WHERE owner_chat_id=? AND conversation_uuid=?', (owner, item['conversationUuid']))
        if 'expectedPage' in item and ((row or {}).get('follow_page_json', '') != item['expectedPage'] or
                                      int((row or {}).get('follow_seq') or 0) != item['expectedSeq']):
            raise RecError('stale_batch', '来源游标已变化，请重新读取', status=409)
        if item.get('page'):
            await conn.execute("""INSERT INTO rec_cursors(owner_chat_id,conversation_uuid,follow_page_json,follow_at_ms) VALUES(?,?,?,?)
                ON CONFLICT(owner_chat_id,conversation_uuid) DO UPDATE SET follow_page_json=excluded.follow_page_json,follow_at_ms=excluded.follow_at_ms""",
                (owner, item['conversationUuid'], json.dumps(item['page']), now_ms))
        else:
            await conn.execute("""INSERT INTO rec_cursors(owner_chat_id,conversation_uuid,follow_seq,follow_at_ms) VALUES(?,?,?,?)
                ON CONFLICT(owner_chat_id,conversation_uuid) DO UPDATE SET follow_seq=MAX(rec_cursors.follow_seq,excluded.follow_seq),
                follow_at_ms=excluded.follow_at_ms,follow_page_json=''""", (owner, item['conversationUuid'], int(item['followSeq']), now_ms))


async def mark_follow_started(conn, owner, conversation_uuid, now_ms):
    await conn.execute('''INSERT INTO rec_cursors(owner_chat_id,conversation_uuid,follow_at_ms) VALUES(?,?,?)
        ON CONFLICT(owner_chat_id,conversation_uuid) DO UPDATE SET follow_at_ms=excluded.follow_at_ms''',
                       (owner, conversation_uuid, now_ms))


async def activity(conn, owner, *, now_ms, days=28, tz_name='Asia/Shanghai'):
    """Observed human message timing only; never infer sleep from it."""
    tz = ZoneInfo(tz_name)
    rows = await many(conn, '''SELECT o.created_at_ms ms FROM web_operations o JOIN web_conversations c ON c.conversation_uuid=o.conversation_uuid
        WHERE c.owner_chat_id=? AND o.op_type='user_message' AND o.created_at_ms>=? AND COALESCE(o.internal,0)=0
          AND COALESCE(o.source,'') IN ('','user','web','telegram')''', (owner, now_ms - days * 86_400_000))
    hours = [0] * 24
    for r in rows:
        hours[datetime.fromtimestamp(r['ms'] / 1000, tz).hour] += 1
    return {'days': days, 'messages': len(rows), 'hourHistogram': hours}


def quiet_time(histogram):
    """Start of the quietest observed 3-hour window, as HH:MM (default 05:30)."""
    if not histogram or sum(histogram) < 50:
        return '05:30'
    best = min(range(24), key=lambda h: (sum(histogram[(h + i) % 24] for i in range(3)), abs(h - 5)))
    return f'{best:02d}:30'
