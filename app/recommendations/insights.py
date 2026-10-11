"""Read-only OpenBear usage summary for the internal-suggestion lane.

The model never reads raw tables. Credentials are not in scope; memory and
prompt material is limited to titles/refs so suggestions can de-duplicate.
"""
from __future__ import annotations

from app.recommendations.sources import automatic_roots, visible_messages, scoped_conversations, in_scope
from app.recommendations.contracts import Settings
from app.webhooks.repository import many, one

CORRECTION_WORDS = ('不要', '又', '我说过', '不是让你', '按之前', '每次都', '下次', '别再', '还是没', '提醒过')


async def summary(host, owner, *, now_ms, days=14, rec_folder='', settings=None):
    settings = settings or Settings()
    conn = host.db.conn
    since_s = now_ms // 1000 - days * 86400
    errors = await many(conn, '''SELECT model,COALESCE(NULLIF(error_type,''),'未分类') error_type,COUNT(*) n,
          MIN(created_at) first_at,MAX(created_at) last_at FROM model_calls
        WHERE created_at>=? AND status!='ok' GROUP BY model,error_type ORDER BY n DESC LIMIT 30''', (since_s,))
    volume = await many(conn, '''SELECT model,COUNT(*) calls,SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) failed
        FROM model_calls WHERE created_at>=? GROUP BY model ORDER BY calls DESC LIMIT 20''', (since_s,))
    crons = await many(conn, '''SELECT j.name,j.enabled,j.folder_id,f.name folder FROM cron_jobs j
        LEFT JOIN web_conversation_folders f ON f.folder_uuid=j.folder_id
        WHERE j.owner_chat_id=? AND j.deleted_at_ms IS NULL ORDER BY j.created_at_ms''', (owner,))
    try:
        hooks = await many(conn, '''SELECT e.endpoint_id,e.enabled,json_extract(r.config_json,'$.name') name FROM webhook_endpoints e
            JOIN webhook_revisions r ON r.endpoint_id=e.endpoint_id AND r.revision=e.current_revision
            WHERE e.owner_chat_id=? AND e.deleted_at_ms IS NULL''', (owner,))
    except Exception:
        hooks = []
    memories = await many(conn, '''SELECT e.ref,e.title,e.expanded FROM memory_entries e
        WHERE e.enabled=1 AND e.archived=0 ORDER BY e.updated_at DESC LIMIT 80''')
    docs = await many(conn, 'SELECT name,title FROM memory_docs WHERE enabled=1 AND archived=0 ORDER BY updated_at DESC LIMIT 60')
    disabled = await many(conn, '''SELECT ref,title,updated_at FROM memory_entries WHERE enabled=0 AND archived=0
        ORDER BY updated_at DESC LIMIT 40''')
    folders = await host._tree_folders(owner, include_properties=True)
    folder_list = []
    for fid, row in folders.items():
        if not in_scope(fid, settings, rec_folder):
            continue
        defaults = _json_object(row.get('run_defaults_json'))
        folder_list.append({'folder': host._tree_folder_path_text(fid, folders), 'folderId': fid,
                            'promptChars': len((row.get('prompt_markdown') or '').strip()),
                            'defaultModel': defaults.get('mainModel') or '', 'workspaceDir': row.get('workspace_dir') or ''})
    skills = [{'name': s.name, 'description': s.description[:160]} for s in getattr(host, 'skills', []) or []]
    template = await one(conn, 'SELECT name,length(content) chars,updated_at FROM memory_templates WHERE is_active=1 LIMIT 1')
    habits = await many(conn, '''SELECT kind,topic,signal,detail,last_mentioned_ms FROM rec_profile
        WHERE owner_chat_id=? AND removed_at_ms IS NULL AND kind IN ('habit','preference') AND signal IN ('explicit','repeated')
        ORDER BY last_mentioned_ms DESC LIMIT 40''', (owner,))
    return {
        'windowDays': days,
        'modelErrors': errors,
        'modelVolume': volume,
        'cronJobs': crons,
        'webhooks': hooks,
        'skills': skills,
        'memoryEntries': [{'ref': m['ref'], 'title': m['title'], 'expanded': bool(m['expanded'])} for m in memories],
        'disabledMemoryEntries': [{'ref': m['ref'], 'title': m['title'], 'lastEditedAt': _when(m['updated_at'])} for m in disabled],
        'memoryDocs': docs,
        'folders': folder_list,
        'activeTemplate': {'name': template['name'], 'chars': template['chars'], 'lastEditedAt': _when(template['updated_at'])} if template else {},
        'habits': [{'kind': h['kind'], 'topic': h['topic'], 'signal': h['signal'], 'detail': h['detail'][:600]} for h in habits],
        'possibleCorrections': await _corrections(conn, owner, since_ms=now_ms - days * 86_400_000, rec_folder=rec_folder, settings=settings),
        'note': ('habits 是画像里他反复或明确表达的做事方式；possibleCorrections 只是关键词粗筛的他本人消息，需要自行判断。'
                 '一种做法是否已经写在某处，用 insights.locate 查，不要凭标题推测；凭证不在提供范围内。'),
    }


def _when(seconds):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(int(seconds), ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M') if seconds else ''


def _json_object(raw):
    import json
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _corrections(conn, owner, *, since_ms, rec_folder, limit=60, settings=None):
    rows = await scoped_conversations(conn, owner, settings or Settings(), rec_folder, since_ms=since_ms, words=CORRECTION_WORDS)
    roots = await automatic_roots(conn, owner)
    out = []
    for row in rows:
        for m in await visible_messages(conn, owner, row['conversation_uuid'], auto_roots=roots):
            if m['role'] == 'user' and m['ms'] >= since_ms and any(w in m['text'] for w in CORRECTION_WORDS):
                out.append({'conversation_uuid': row['conversation_uuid'], 'title': row['title'],
                            'ms': m['ms'], 'text': m['text'][:240]})
    return sorted(out, key=lambda x: x['ms'], reverse=True)[:limit]


async def locate(host, owner, query, *, rec_folder, settings=None):
    """Where a working convention is already written: folder prompts, the active templates,
    memory (also disabled entries), documents and Skills. Returns matching excerpts."""
    import asyncio
    terms = [t for t in str(query or '').lower().split() if t][:5]
    if not terms:
        return {'items': [], 'note': 'query 为空'}
    folders = await host._tree_folders(owner, include_properties=True)
    folder_texts = [('folder', host._tree_folder_path_text(fid, folders), row.get('prompt_markdown') or '')
                    for fid, row in folders.items() if in_scope(fid, settings or Settings(), rec_folder) and (row.get('prompt_markdown') or '').strip()]
    skills = [('skill', s.name, (s.description or '') + '\n' + _skill_text(s)) for s in getattr(host, 'skills', []) or []]
    items = await asyncio.to_thread(_locate, host.db.path, terms, folder_texts + skills)
    return {'query': query, 'items': items,
            'note': '只读。memory 的 enabled=false 表示这条记忆已停用，不会进入任何会话。没有命中不等于没写过，换个说法再查一次。'}


def _skill_text(skill):
    from pathlib import Path
    try:
        return Path(skill.location).read_text(encoding='utf-8', errors='replace')[:20000]
    except OSError:
        return ''


def _excerpt(text, terms, width=360):
    low = text.lower()
    pos = max(0, min((low.find(t) for t in terms if t in low), default=0) - 120)
    return ('…' if pos else '') + text[pos:pos + width]


def _locate(db_path, terms, extra):
    import sqlite3
    from pathlib import Path
    hits = []
    for kind, name, text in extra:
        if all(t in (name + '\n' + text).lower() for t in terms):
            hits.append({'kind': kind, 'name': name, 'excerpt': _excerpt(text, terms)})
    with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        first = '%' + terms[0] + '%'
        for row in conn.execute('''SELECT name,content,is_active,is_agent_active FROM memory_templates
                WHERE (is_active=1 OR is_agent_active=1) AND LOWER(content) LIKE ?''', (first,)):
            if all(t in str(row['content']).lower() for t in terms):
                hits.append({'kind': 'template', 'name': row['name'],
                             'scope': 'main' if row['is_active'] else 'agent', 'excerpt': _excerpt(str(row['content']), terms)})
        for row in conn.execute('''SELECT ref,title,enabled,expanded,COALESCE(NULLIF(trim(body),''),fields_json,'') b FROM memory_entries
                WHERE archived=0 AND (LOWER(title) LIKE ? OR LOWER(COALESCE(body,'')) LIKE ? OR LOWER(COALESCE(fields_json,'')) LIKE ?)''',
                                (first, first, first)):
            text = str(row['b'] or '')
            if all(t in (str(row['title'] or '') + '\n' + text).lower() for t in terms):
                hits.append({'kind': 'memory', 'name': row['ref'], 'title': row['title'], 'enabled': bool(row['enabled']),
                             'expanded': bool(row['expanded']), 'excerpt': _excerpt(text, terms)})
        for row in conn.execute('''SELECT name,title,content FROM memory_docs WHERE enabled=1 AND archived=0
                AND (LOWER(title) LIKE ? OR LOWER(content) LIKE ?)''', (first, first)):
            if all(t in (str(row['title'] or '') + '\n' + str(row['content'] or '')).lower() for t in terms):
                hits.append({'kind': 'doc', 'name': row['name'], 'title': row['title'], 'excerpt': _excerpt(str(row['content']), terms)})
    return hits[:30]


def _memory_search(db_path, terms):
    import sqlite3
    from pathlib import Path
    with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        first = '%' + terms[0].lower() + '%'
        hits = []
        for table, key, title, body in (('memory_entries', 'ref', 'title', "COALESCE(NULLIF(trim(body),''),fields_json,'')"),
                                        ('memory_docs', 'name', 'title', 'content')):
            for row in conn.execute(f'''SELECT {key} k,{title} t,{body} b FROM {table} WHERE enabled=1 AND archived=0
                    AND (LOWER({title}) LIKE ? OR LOWER({body}) LIKE ?) LIMIT 40''', (first, first)):
                text = str(row['b'] or '')
                if all(term in (str(row['t'] or '') + '\n' + text).lower() for term in terms):
                    low = text.lower()
                    pos = max(0, min((low.find(t) for t in terms if t in low), default=0) - 120)
                    hits.append({'kind': 'memory' if table == 'memory_entries' else 'doc', 'ref': row['k'], 'title': row['t'],
                                 'snippet': ('…' if pos else '') + text[pos:pos + 420]})
        return hits


async def internal_search(host, owner, query, *, rec_folder, now_ms, limit=12, days=365, settings=None):
    import asyncio
    terms = [t for t in str(query or '').lower().split() if t][:5]
    if not terms:
        return {'items': [], 'note': 'query 为空'}
    limit = max(1, min(int(limit or 12), 30))
    roots = await automatic_roots(host.db.conn, owner)
    items = []
    for row in await scoped_conversations(host.db.conn, owner, settings or Settings(), rec_folder,
                                          since_ms=now_ms - days * 86_400_000, words=terms[:1]):
        matches = []
        for m in await visible_messages(host.db.conn, owner, row['conversation_uuid'], auto_roots=roots):
            if m['ms'] < now_ms - days * 86_400_000:
                continue
            if all(t in (str(row['title'] or '') + '\n' + m['text']).lower() for t in terms):
                matches.append({'kind': 'conversation', 'conversationUuid': row['conversation_uuid'], 'title': row['title'],
                                'role': '用户' if m['role'] == 'user' else '助手', 'atMs': m['ms'],
                                'snippet': _excerpt(m['text'], terms, width=420)})
        items.extend(sorted(matches, key=lambda x: x['atMs'], reverse=True)[:2])
    items = sorted(items, key=lambda x: x['atMs'], reverse=True)[:limit]
    items.extend(await asyncio.to_thread(_memory_search, host.db.path, terms))
    return {'query': query, 'items': items,
            'note': '只读：你的历史会话（不含推荐目录）、记忆条目和文档；凭证不在范围内。conversation 结果可用 source.read 读全文。'}
