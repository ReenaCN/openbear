"""Scripted follow checks: no model call, no cost. Changes wait for the next daily run."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re

import aiohttp

from app.logging import get_logger
from app.webhooks.repository import many

log = get_logger('recommendations.follows')
MAX_PAGE_BYTES = 400_000
_TAG_RE = re.compile(r'<(script|style)[^>]*>.*?</\1>|<[^>]+>', re.S | re.I)


def page_fingerprint(body: str) -> str:
    text = html.unescape(_TAG_RE.sub(' ', body))
    text = re.sub(r'\s+', ' ', text).strip()
    return hashlib.sha256(text.encode()).hexdigest()[:24]


async def probe(session, kind, target):
    """Return (value, display) for one follow target."""
    headers = {'User-Agent': 'OpenBear-Recommendation/1.0', 'Accept': 'application/json'}
    if kind == 'github_release':
        async with session.get(f'https://api.github.com/repos/{target}/releases/latest', headers=headers) as r:
            if r.status == 404:
                async with session.get(f'https://api.github.com/repos/{target}/tags?per_page=1', headers=headers) as t:
                    t.raise_for_status()
                    tags = await t.json()
                    name = tags[0]['name'] if tags else ''
                    return name, name
            r.raise_for_status()
            data = await r.json()
            return str(data.get('tag_name') or ''), str(data.get('tag_name') or '')
    if kind == 'github_commits':
        async with session.get(f'https://api.github.com/repos/{target}/commits?per_page=1', headers=headers) as r:
            r.raise_for_status()
            data = await r.json()
            if not data:
                return '', ''
            sha = str(data[0].get('sha') or '')
            message = str((data[0].get('commit') or {}).get('message') or '').split('\n')[0][:120]
            return sha, f'{sha[:7]} {message}'
    if kind == 'npm':
        async with session.get(f'https://registry.npmjs.org/{target}/latest', headers=headers) as r:
            r.raise_for_status()
            data = await r.json()
            return str(data.get('version') or ''), str(data.get('version') or '')
    if kind == 'webpage':
        async with session.get(target, headers={'User-Agent': headers['User-Agent']}, max_redirects=5) as r:
            r.raise_for_status()
            body = (await r.content.read(MAX_PAGE_BYTES)).decode(r.charset or 'utf-8', errors='replace')
            value = page_fingerprint(body)
            return value, '页面内容有变化'
    return '', ''


async def check_due(service, owner, settings, now, *, session_factory=None):
    rows = await many(service.db.conn, '''SELECT * FROM rec_follows WHERE owner_chat_id=? AND active=1 AND kind<>'topic'
        AND last_checked_ms<=?''', (owner, now - settings.tracking_interval_minutes * 60_000))
    if not rows:
        return 0
    factory = session_factory or (lambda: aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)))
    changed = 0
    async with factory() as session:
        for row in rows[:20]:
            value = display = error = ''
            try:
                value, display = await probe(session, row['kind'], row['target'])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'[:300]
            async with service.db.write_transaction(label='recommendation-follow-check') as conn:
                if error or not value:
                    await conn.execute('UPDATE rec_follows SET last_checked_ms=?,error=? WHERE follow_id=?',
                                       (now, error or '未取得结果', row['follow_id']))
                    continue
                if row['last_value'] and value != row['last_value']:
                    old = row['pending_change'] or ''
                    change = json.dumps({'from': row['fingerprint'] or row['last_value'][:40], 'to': display,
                                         'detectedAtMs': now, 'earlier': old[:400] if old else ''}, ensure_ascii=False)
                    await conn.execute('''UPDATE rec_follows SET last_value=?,fingerprint=?,last_checked_ms=?,last_changed_ms=?,
                        pending_change=?,error='' WHERE follow_id=?''', (value, display, now, now, change, row['follow_id']))
                    changed += 1
                else:
                    await conn.execute('UPDATE rec_follows SET last_value=?,fingerprint=?,last_checked_ms=?,error=\'\' WHERE follow_id=?',
                                       (value, display, now, row['follow_id']))
    if changed:
        log.info('recommendation follow changes', owner=owner, changed=changed)
    return changed
