"""Authenticated recommendation page API."""
from __future__ import annotations

import json
import sqlite3

from aiohttp import web
from pydantic import ValidationError

from app.recommendations import store
from app.recommendations.contracts import RecError
from app.recommendations.service import folder_owned, run_public
from app.webhooks.repository import many, one


def install(app, host):
    from app.web_console.core import _WEB_SESSION_KEY

    def service():
        value = getattr(host, 'recommendations', None)
        if value is None:
            raise RecError('recommendations_unavailable', '推荐模块不可用', status=503)
        return value

    def owner(request):
        session = request.get(_WEB_SESSION_KEY)
        if session is None:
            raise RecError('authentication_required', status=401)
        return int(session.chat_id)

    async def body(request):
        try:
            data = await request.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            raise RecError('invalid_json', '请求体必须是 JSON 对象')
        return data

    def wrap(fn):
        async def handler(request):
            try:
                return web.json_response(await fn(request, owner(request)), headers={'Cache-Control': 'no-store'})
            except RecError as exc:
                return web.json_response(exc.payload, status=exc.status)
            except (ValidationError, TypeError, ValueError, OverflowError) as exc:
                return web.json_response({'code': 'invalid_parameters', 'message': str(exc)}, status=422)
            except sqlite3.Error:
                return web.json_response({'code': 'storage_unavailable', 'message': '存储暂不可用'}, status=503)
        return handler

    def limit(request, default=30, cap=100):
        try:
            return max(1, min(cap, int(request.query.get('limit') or default)))
        except ValueError:
            return default

    async def runs_public(o, rows):
        """Run rows plus whether their conversations still exist (deleted ones are not linkable)."""
        ids = sorted({x[k] for x in rows for k in ('conversation_uuid', 'source_conversation_uuid') if x[k]})
        alive = {row['conversation_uuid'] for row in await many(
            service().db.conn, f"SELECT conversation_uuid FROM web_conversations WHERE owner_chat_id=? AND conversation_uuid IN ({','.join('?' for _ in ids)})",
            (o, *ids))} if ids else set()
        return [{**run_public(x), 'conversationExists': x['conversation_uuid'] in alive,
                 'sourceExists': x['source_conversation_uuid'] in alive} for x in rows]

    async def overview(r, o):
        s = service()
        settings, _ = await s.settings_row(o)
        await s.reconcile_orphans(o)
        runs = await many(s.db.conn, "SELECT * FROM rec_runs WHERE owner_chat_id=? AND status<>'skipped' ORDER BY started_at_ms DESC LIMIT 8", (o,))
        return {'enabled': settings.enabled, 'configured': bool(settings.models), 'budget': await s.budget_left(o, settings),
                'models': s.model_health(settings),
                'runs': await runs_public(o, runs), 'folderId': await s.rec_folder(o), 'unseen': await s.unseen(o)}

    async def badge(r, o):
        return {'unseen': await service().unseen(o)}

    async def cards(r, o):
        s = service()
        lane = r.query.get('lane') or None
        state = r.query.get('state') or None
        offset = max(0, int(r.query.get('offset') or 0))
        result = await store.card_list(s.db.conn, o, lane=lane, state=state, limit=limit(r, 40), offset=offset,
                                       unread=r.query.get('unread') == '1', unread_first=True)
        ids = [item['id'] for item in result['items']]
        feedback = await many(s.db.conn, f'''SELECT card_id,action,detail,created_at_ms FROM rec_feedback WHERE owner_chat_id=?
            AND card_id IN ({','.join('?' for _ in ids)}) ORDER BY created_at_ms''', (o, *ids)) if ids else []
        by_card = {}
        for item in feedback:
            by_card.setdefault(item['card_id'], []).append({'action': item['action'], 'detail': item['detail'], 'atMs': item['created_at_ms']})
        folders = await host._tree_folders(o)
        origins = sorted({item['originConversationUuid'] for item in result['items'] if item.get('originConversationUuid')})
        titles = {row['conversation_uuid']: row['title'] for row in await many(
            s.db.conn, f'''SELECT conversation_uuid,title FROM web_conversations WHERE owner_chat_id=?
                AND conversation_uuid IN ({','.join('?' for _ in origins)})''', (o, *origins))} if origins else {}
        for item in result['items']:
            item['feedback'] = by_card.get(item['id'], [])
            item['originTitle'] = titles.get(item.get('originConversationUuid') or '', '')
            # A deleted origin conversation must not render as a link to nowhere.
            item['originExists'] = bool(item.get('originConversationUuid')) and item['originConversationUuid'] in titles
            folder = item['location'].get('folderId')
            if folder in folders:
                item['location']['folderPath'] = host._tree_folder_path_text(folder, folders)
        return result

    async def seen(r, o):
        """Explicit read only: one card, a list, or {all: true}. Displaying a card never calls this."""
        s = service()
        data = await body(r)
        mark = "UPDATE rec_cards SET seen_at_ms=COALESCE(seen_at_ms,?),state=CASE WHEN state='new' THEN 'seen' ELSE state END"
        async with s.db.write_transaction(label='recommendation-seen') as conn:
            if data.get('all') is True:
                await conn.execute(mark + ' WHERE owner_chat_id=? AND seen_at_ms IS NULL AND removed_at_ms IS NULL', (s.clock(), o))
            else:
                for card_id in [str(x) for x in data.get('ids') or []][:200]:
                    await conn.execute(mark + ' WHERE card_id=? AND owner_chat_id=?', (s.clock(), card_id, o))
        return {'ok': True, 'unseen': await s.unseen(o)}

    async def feedback(r, o):
        s = service()
        data = await body(r)
        card = await store.card_row(s.db.conn, o, r.match_info['card_id'])
        action = str(data.get('action') or '')
        detail = str(data.get('detail') or '')
        async with s.db.write_transaction(label='recommendation-feedback') as conn:
            recorded = await store.feedback(conn, o, card, action, s.clock(), detail=detail,
                                            conversation_uuid=str(data.get('conversationUuid') or ''),
                                            request_id=str(data.get('requestId') or ''))
            if action == 'follow':
                # Follow the matter itself; the user's optional note says which aspect matters.
                subject = card['topic'] or card['title']
                await store.follow_add(conn, o, card['card_id'], subject, 'topic', subject, s.clock(), note=detail or None)
                signal = json.loads(card['follow_json'] or '{}')
                if signal.get('kind') and signal['kind'] != 'topic':
                    await store.follow_add(conn, o, card['card_id'], subject, signal['kind'], signal['target'], s.clock())
            if action == 'unfollow':
                await conn.execute('UPDATE rec_follows SET active=0 WHERE owner_chat_id=? AND card_id=?', (o, card['card_id']))
        return {'recorded': recorded, 'card': store.card_public(await store.card_row(s.db.conn, o, card['card_id']))}

    async def continue_card(r, o):
        """Prepare a draft conversation target; the browser fills the composer, never sends."""
        s = service()
        data = await body(r)
        card = await store.card_row(s.db.conn, o, r.match_info['card_id'])
        location = json.loads(card['location_json'] or '{}')
        folder_id = location.get('folderId') or ''
        if folder_id and not await folder_owned(s.db.conn, o, folder_id):
            folder_id = ''
        if not folder_id and location.get('conversationUuid'):
            origin = await one(s.db.conn, 'SELECT folder_uuid FROM web_conversations WHERE conversation_uuid=? AND owner_chat_id=?',
                               (location['conversationUuid'], o))
            folder_id = origin['folder_uuid'] if origin else ''
        model = location.get('model') or ''
        if model and not host.config.models.resolve(model):
            model = ''
        question = str(data.get('question') or '').strip()[:200]
        async with s.db.write_transaction(label='recommendation-ask') as conn:
            await store.feedback(conn, o, card, 'ask', s.clock(), detail=question, request_id=str(data.get('requestId') or ''))
        return {'folderId': folder_id, 'model': model, 'question': question,
                'reference': {'kind': 'rec', 'id': card['card_id'], 'label': card['title']},
                'originConversationUuid': location.get('conversationUuid') or ''}

    async def open_source(r, o):
        s = service()
        card = await store.card_row(s.db.conn, o, r.match_info['card_id'])
        async with s.db.write_transaction(label='recommendation-open') as conn:
            await store.feedback(conn, o, card, 'open_source', s.clock(), detail=str((await body(r)).get('url') or '')[:500])
        return {'ok': True}

    async def remove_card(r, o):
        s = service()
        await store.card_row(s.db.conn, o, r.match_info['card_id'])
        async with s.db.write_transaction(label='recommendation-card-remove') as conn:
            await conn.execute('UPDATE rec_cards SET removed_at_ms=? WHERE card_id=? AND owner_chat_id=?', (s.clock(), r.match_info['card_id'], o))
        return {'ok': True}

    async def profile(r, o):
        return {'items': await store.profile_list(service().db.conn, o, kind=r.query.get('kind') or None)}

    async def profile_save(r, o):
        s = service()
        data = await body(r)
        if r.match_info.get('item_id'):
            data['id'] = r.match_info['item_id']
        async with s.db.write_transaction(label='recommendation-profile-user') as conn:
            item_id, result = await store.profile_upsert(conn, o, data, s.clock(), by_user=True)
        row = await one(s.db.conn, 'SELECT * FROM rec_profile WHERE item_id=?', (item_id,))
        return {'item': store.profile_public(row), 'result': result}

    async def profile_remove(r, o):
        s = service()
        async with s.db.write_transaction(label='recommendation-profile-user-remove') as conn:
            await store.profile_remove(conn, o, r.match_info['item_id'], s.clock(), by_user=True)
        return {'ok': True}

    async def follows(r, o):
        return {'items': await store.follows(service().db.conn, o, active_only=r.query.get('all') != '1')}

    async def follow_add(r, o):
        s = service()
        data = await body(r)
        async with s.db.write_transaction(label='recommendation-follow-add') as conn:
            follow_id = await store.follow_add(conn, o, str(data.get('cardId') or ''), str(data.get('topic') or data.get('target') or ''),
                                               str(data.get('kind') or 'topic'), str(data.get('target') or ''), s.clock(),
                                               note=str(data['note']).strip() if isinstance(data.get('note'), str) else None)
        s.wake.set()
        return {'id': follow_id}

    async def follow_remove(r, o):
        s = service()
        async with s.db.write_transaction(label='recommendation-follow-remove') as conn:
            follow = await one(conn, 'SELECT * FROM rec_follows WHERE follow_id=? AND owner_chat_id=?', (r.match_info['follow_id'], o))
            await conn.execute('UPDATE rec_follows SET active=0 WHERE follow_id=? AND owner_chat_id=?', (r.match_info['follow_id'], o))
            if follow and follow['active'] and follow['card_id']:
                other = await one(conn, 'SELECT 1 FROM rec_follows WHERE owner_chat_id=? AND card_id=? AND active=1', (o, follow['card_id']))
                card = await one(conn, 'SELECT * FROM rec_cards WHERE owner_chat_id=? AND card_id=?', (o, follow['card_id']))
                if card and not other:
                    await store.feedback(conn, o, card, 'unfollow', s.clock())
        return {'ok': True}

    async def settings_get(r, o):
        return await service().get_settings(o)

    async def settings_put(r, o):
        return await service().save_settings(o, await body(r))

    async def run_stop(r, o):
        return await service().stop(o, r.match_info['run_id'])

    async def runs(r, o):
        rows = await many(service().db.conn, 'SELECT * FROM rec_runs WHERE owner_chat_id=? ORDER BY started_at_ms DESC LIMIT ?', (o, limit(r)))
        return {'items': await runs_public(o, rows)}

    for method, path, handler in [
        ('GET', '/overview', overview), ('GET', '/badge', badge), ('GET', '/cards', cards), ('POST', '/cards/seen', seen),
        ('POST', '/cards/{card_id}/feedback', feedback), ('POST', '/cards/{card_id}/continue', continue_card),
        ('POST', '/cards/{card_id}/open-source', open_source), ('DELETE', '/cards/{card_id}', remove_card),
        ('GET', '/profile', profile), ('POST', '/profile', profile_save), ('PATCH', '/profile/{item_id}', profile_save),
        ('DELETE', '/profile/{item_id}', profile_remove),
        ('GET', '/follows', follows), ('POST', '/follows', follow_add), ('DELETE', '/follows/{follow_id}', follow_remove),
        ('GET', '/settings', settings_get), ('PUT', '/settings', settings_put),
        # Runs are started only by the background scheduler; the page can watch and stop them.
        ('GET', '/runs', runs), ('POST', '/runs/{run_id}/stop', run_stop),
    ]:
        app.router.add_route(method, '/api/recommendations' + path, wrap(handler))
