"""Feed ordering uses the full result, not just the current browser page."""
from app.recommendations import store
from tests.test_recommendations import OWNER, card_args, rec_env, base_env  # noqa: F401
from tests.test_web_admin import _login_cookie


async def test_feed_unread_first_before_pagination_and_after_feedback(rec_env):
    e = rec_env
    now = e.rec.clock()
    ids = []
    async with e.db.write_transaction() as conn:
        for index in range(45):
            ids.append(await store.card_save(conn, OWNER, {'run_id': 'feed-test'},
                card_args(dedupeKey=f'feed-{index}', title=f'推荐 {index}', priority=index % 3)['params'],
                now - index * 86_400_000))
        # Forty newest cards are already read; the oldest five must still lead.
        await conn.executemany("UPDATE rec_cards SET seen_at_ms=?,state='seen' WHERE card_id=?",
                               [(now, card_id) for card_id in ids[:40]])
        # Other lanes and owners cannot leak into filtered results.
        await store.card_save(conn, OWNER, {'run_id': 'feed-test'},
                              card_args(dedupeKey='openbear', lane='openbear', category='openbear')['params'], now + 1)
        await store.card_save(conn, OWNER + 1, {'run_id': 'feed-test'},
                              card_args(dedupeKey='other-owner')['params'], now + 2)
    cookie = {'openbear_web_session': await _login_cookie(e)}

    async def page(query):
        response = await e.client.get('/api/recommendations/cards?' + query, cookies=cookie)
        assert response.status == 200
        return await response.json()

    first = await page('lane=external&limit=3')
    assert first['total'] == 45
    assert [c['id'] for c in first['items']] == ids[40:43]
    second = await page('lane=external&limit=3&offset=3')
    assert [c['id'] for c in second['items']] == ids[43:] + ids[:1]
    # Model-side history remains chronological.
    tool_list = await store.card_list(e.db.conn, OWNER, lane='external', limit=3)
    assert [c['id'] for c in tool_list['items']] == ids[:3]

    response = await e.client.post(f'/api/recommendations/cards/{ids[40]}/feedback',
                                   cookies=cookie, json={'action': 'note', 'detail': '补充说明'})
    assert response.status == 200
    refreshed = await page('lane=external&limit=5')
    assert [c['id'] for c in refreshed['items']] == ids[41:] + ids[:1]
    read = await e.client.post('/api/recommendations/cards/seen', cookies=cookie, json={'ids': [ids[41]]})
    assert read.status == 200
    unread = await page('lane=external&unread=1')
    assert [c['id'] for c in unread['items']] == ids[42:]
    read = await e.client.post('/api/recommendations/cards/seen', cookies=cookie, json={'all': True})
    assert read.status == 200
    refreshed = await page('lane=external&limit=5')
    assert [c['id'] for c in refreshed['items']] == ids[:5]
