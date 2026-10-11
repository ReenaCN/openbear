from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.db.dao import MessageDAO
from app.db.engine import DB
from app.web_console.chat_state import WebAdminChatStateMixin
from app.web_console.statistics_api import read_statistics
from tests.test_statistics_api import BEIJING, _database


@pytest.mark.parametrize("legacy_calls", [2, 0, None])
@pytest.mark.parametrize("with_physical", [False, True])
def test_legacy_aggregate_does_not_invent_stage_samples(tmp_path, legacy_calls, with_physical):
    path, connection = _database(tmp_path)
    now = int(datetime.now(BEIJING).timestamp())
    connection.execute("INSERT INTO web_conversations VALUES (?,?,?,?)", (101, "conv", 42, ""))
    connection.execute(
        "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (101, now, "model", "controller_run", "ok", "", legacy_calls, 2, 0, 0,
         100, 20, 0, 0, 0.5, 100, 400, 3000),
    )
    if with_physical:
        connection.execute(
            "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (101, now, "model", "controller_request", "ok", "", 1, 1, 0, 0,
             10, 2, 0, 0, 0.25, 10, 20, 100),
        )
    connection.commit()
    connection.close()

    result = read_statistics(path, 42, days=1)
    samples = int(with_physical)
    latency = result["latency"]["all"]
    assert latency["timingSamples"] == samples
    assert latency["avgConnectMs"] == (10 if with_physical else None)
    assert latency["avgFirstTokenMs"] == (20 if with_physical else None)
    assert latency["avgTimedTotalMs"] == (100 if with_physical else None)
    assert latency["percentiles"]["total"]["samples"] == samples
    for item in [result["summary"]["current"], result["models"][0]]:
        assert item["firstTokenSamples"] == samples
        assert item["avgFirstTokenMs"] == (20 if with_physical else None)
        assert item["modelCalls"] == (legacy_calls or 1) + samples
        assert item["costUsd"] == 0.5 + 0.25 * samples
        assert item["inputTokens"] == 100 + 10 * samples
        assert item["totalTimeMs"] == 3000 + 100 * samples


@pytest.mark.asyncio
async def test_chat_model_projection_preserves_request_origin(tmp_path):
    db = DB(str(tmp_path / "chat.db"))
    await db.connect()
    try:
        messages = MessageDAO(db)
        await messages.ensure_session(101)
        session = await messages.get_or_create_session_uuid(101)
        for kind, duration in [("controller_request", 100), ("agent_request", 1000), ("controller_run", 300)]:
            await messages.add_model_call(101, session_uuid=session, model="model", call_kind=kind,
                                          connect_ms=10, first_token_ms=20, total_time_ms=duration)
        await messages.add_model_call(101, session_uuid="other", call_kind="controller_request")
        rows = await WebAdminChatStateMixin._chat_model_calls(SimpleNamespace(db=db), 101, session)
        assert [row["call_kind"] for row in rows] == ["controller_request", "agent_request", "controller_run"]
        assert [row["total_time_ms"] for row in rows] == [100, 1000, 300]
    finally:
        await db.close()
