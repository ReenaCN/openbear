from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.db.engine import DB
from app.web_console.operation_store import WebAdminOperationsMixin


class Store(WebAdminOperationsMixin):
    def __init__(self, db):
        self.db = db


async def seed(db, conv, count, *, timestamp=9_999_999_999_999):
    async with db.write_transaction(label="seed") as conn:
        await conn.executemany(
            """INSERT INTO web_event_frames
            (conversation_uuid, frame_seq, op_id, op_type, action, revision,
             display_seq, payload_json, created_at_ms, updated_at_ms)
            VALUES (?, ?, 'stream', 'assistant_message', 'patch', ?, 1, '{}', ?, ?)""",
            [(conv, i, i, timestamp, timestamp) for i in range(1, count + 1)],
        )


@pytest.mark.parametrize("scope", ["a", ""])
async def test_retention_preserves_recent_window_latest_and_scope(tmp_path, scope):
    db = DB(str(tmp_path / "retention.db"))
    await db.connect()
    try:
        await seed(db, "a", 350)
        await seed(db, "b", 2, timestamp=1)
        # Old operations with only one frame are still recoverable. Freshness
        # is MAX(id), not revision, matching the original retention contract.
        async with db.write_transaction(label="seed-extra") as conn:
            await conn.execute(
                """INSERT INTO web_event_frames
                (conversation_uuid, frame_seq, op_id, op_type, action, revision,
                 display_seq, payload_json, created_at_ms, updated_at_ms)
                VALUES ('a',351,'only','notice','end',1,2,'{}',1,1)"""
            )
        deleted = await Store(db)._prune_web_event_frames(scope, keep_recent=100)
        assert deleted == (251 if scope == "a" else 252)
        cur = await db.conn.execute(
            "SELECT MIN(frame_seq),COUNT(*) FROM web_event_frames WHERE conversation_uuid='a'"
        )
        assert tuple(await cur.fetchone()) == (252, 100)
        cur = await db.conn.execute(
            "SELECT frame_seq FROM web_event_frames WHERE conversation_uuid='b' ORDER BY frame_seq"
        )
        assert [r[0] for r in await cur.fetchall()] == ([1, 2] if scope else [2])
    finally:
        await db.close()


async def test_foreground_writer_runs_between_retention_batches(tmp_path):
    db = DB(str(tmp_path / "foreground.db"))
    await db.connect()
    foreground = None
    try:
        await seed(db, "a", 1000)
        original = db.write_transaction
        first_commit = asyncio.Event()
        foreground_done = asyncio.Event()
        batch_sizes = []

        async def write_foreground():
            await first_commit.wait()
            async with original(label="foreground", wait_timeout_s=0.5) as conn:
                cur = await conn.execute("SELECT COUNT(*) FROM web_event_frames")
                assert (await cur.fetchone())[0] > 100
                await conn.execute(
                    "UPDATE web_event_frames SET payload_json='{}' WHERE frame_seq=1000"
                )
            foreground_done.set()

        @asynccontextmanager
        async def observed_transaction(**kwargs):
            async with original(**kwargs) as conn:
                before = conn.total_changes
                yield conn
                batch_sizes.append(conn.total_changes - before)
            if len(batch_sizes) == 1:
                first_commit.set()
                await asyncio.wait_for(foreground_done.wait(), timeout=2)

        db.write_transaction = observed_transaction
        foreground = asyncio.create_task(write_foreground())
        assert await Store(db)._prune_web_event_frames("a", keep_recent=100) == 900
        await asyncio.wait_for(foreground, timeout=2)
        assert len(batch_sizes) == 9
        assert max(batch_sizes) <= 100
    finally:
        if foreground is not None and not foreground.done():
            foreground.cancel()
            await asyncio.gather(foreground, return_exceptions=True)
        await db.close()


async def test_cancelled_retention_releases_writer_and_can_resume(tmp_path):
    db = DB(str(tmp_path / "cancel.db"))
    await db.connect()
    try:
        await seed(db, "a", 1000)
        original = db.write_transaction
        committed = asyncio.Event()

        @asynccontextmanager
        async def pause_after_commit(**kwargs):
            async with original(**kwargs) as conn:
                yield conn
            committed.set()
            await asyncio.Event().wait()

        db.write_transaction = pause_after_commit
        pruning = asyncio.create_task(Store(db)._prune_web_event_frames("a", keep_recent=100))
        await asyncio.wait_for(committed.wait(), timeout=2)
        pruning.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pruning
        db.write_transaction = original
        async with original(label="after-cancellation", wait_timeout_s=0.5) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM web_event_frames")
            assert (await cur.fetchone())[0] == 900
        assert await Store(db)._prune_web_event_frames("a", keep_recent=100) == 800
    finally:
        await db.close()
