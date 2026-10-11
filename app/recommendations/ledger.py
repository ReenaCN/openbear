"""Durable recommendation spend, independent of deletable conversation history.

One row per physical attempt. Legacy migration runs once, in the DB startup
transaction; surviving details use their actual call date, and missing history
retains only the positive remainder of the durable run totals on its known day.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import json

from app.webhooks.repository import many, one


def local_day(ms, timezone):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(timezone)).strftime('%Y-%m-%d')


async def migrate(conn):
    if await one(conn, "SELECT 1 FROM rec_usage_migrations WHERE version=1"):
        return
    for run in await many(conn, 'SELECT * FROM rec_runs'):
        settings = await one(conn, 'SELECT config_json FROM rec_settings WHERE owner_chat_id=?', (run['owner_chat_id'],))
        try:
            timezone = json.loads(settings['config_json']).get('timezone', 'Asia/Shanghai') if settings else 'Asia/Shanghai'
            ZoneInfo(timezone)
        except (ValueError, KeyError, TypeError):
            timezone = 'Asia/Shanghai'
        calls = await many(conn, '''SELECT m.*,a.created_at call_started_at FROM model_calls m JOIN runtime_actions a ON a.action_id=m.attempt_id
            JOIN runtime_runs r ON r.run_id=a.run_id WHERE r.root_turn_uuid=?''', (run['root_turn_uuid'],))
        for call in calls:
            tokens = sum(int(call.get(k) or 0) for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'))
            ms = int(call['call_started_at'] or call['created_at'] or 0) * 1000 or run['started_at_ms']
            await conn.execute('''INSERT OR IGNORE INTO rec_usage(attempt_id,run_id,owner_chat_id,called_at_ms,local_date,cost_usd,tokens,calls)
                VALUES(?,?,?,?,?,?,?,1)''', (call['attempt_id'], run['run_id'], run['owner_chat_id'], ms,
                    local_day(ms, timezone), max(0, call['cost_usd'] or 0), tokens))
        total = await one(conn, 'SELECT COALESCE(SUM(cost_usd),0) cost,COALESCE(SUM(tokens),0) tokens,COALESCE(SUM(calls),0) calls FROM rec_usage WHERE run_id=?', (run['run_id'],))
        cost, tokens, count = (max(0, run[k] - total[v]) for k, v in (('cost_usd', 'cost'), ('tokens', 'tokens'), ('model_calls', 'calls')))
        if cost or tokens or count:
            await conn.execute('''INSERT OR IGNORE INTO rec_usage(attempt_id,run_id,owner_chat_id,called_at_ms,local_date,cost_usd,tokens,calls)
                VALUES(?,?,?,?,?,?,?,?)''', ('legacy:' + run['run_id'], run['run_id'], run['owner_chat_id'], run['started_at_ms'],
                    run['local_date'], cost, tokens, count))
    await conn.execute('INSERT INTO rec_usage_migrations(version) VALUES(1)')


async def record(conn, run, outcome, timezone):
    # Invoked inside the same accounting claim/transaction as model_calls. Never
    # price aggregate usage, and never recount rows on refresh or after deletion.
    call = await one(conn, 'SELECT * FROM model_calls WHERE attempt_id=?', (outcome.attempt_id,))
    if call is None:
        raise RuntimeError('recommendation physical attempt has no model-call settlement')
    tokens = sum(int(call.get(k) or 0) for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'))
    await conn.execute('''INSERT OR IGNORE INTO rec_usage(attempt_id,run_id,owner_chat_id,called_at_ms,local_date,cost_usd,tokens,calls)
        VALUES(?,?,?,?,?,?,?,1)''', (outcome.attempt_id, run['run_id'], run['owner_chat_id'], outcome.started_at_ms,
            local_day(outcome.started_at_ms, timezone), max(0, call['cost_usd'] or 0), tokens))
    await conn.execute('''UPDATE rec_runs SET cost_usd=(SELECT COALESCE(SUM(cost_usd),0) FROM rec_usage WHERE run_id=?),
        tokens=(SELECT COALESCE(SUM(tokens),0) FROM rec_usage WHERE run_id=?),
        model_calls=(SELECT COALESCE(SUM(calls),0) FROM rec_usage WHERE run_id=?) WHERE run_id=?''', (run['run_id'],) * 4)
