"""One owner-bound interaction lifecycle shared by Web and Telegram.

Durable records arbitrate terminal answers; process-local Futures only wake the
original tool invocation. On restart pending records are interrupted, not resumed.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.db.engine import DB
from app.interaction_data import canonical_answer, empty_result, normalize_definition, redact_result
from app.logging import get_logger
from app.tools.confirmation import constraints_within

log = get_logger("user_interactions")
Listener = Callable[[str, dict[str, Any]], Awaitable[None]]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class InteractionService:
    def __init__(self, db: DB) -> None:
        self.db = db
        self.pending: dict[str, dict[str, Any]] = {}
        self.by_conversation: dict[str, set[str]] = {}
        self._listeners: list[Listener] = []
        # A single short submission lock avoids an unbounded per-ID lock cache.
        # No network or notification callback is awaited under this lock.
        self._lock = asyncio.Lock()
        self._closed = False
        # Raw conditional answers remain process-local (including sensitive text).
        # They cannot survive a restart or authorize another conversation/turn.
        self._amended_approvals: dict[str, dict[str, Any]] = {}

    def add_listener(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    @staticmethod
    def _public(item: dict[str, Any], *, redact: bool = False) -> dict[str, Any]:
        out = copy.deepcopy({key: value for key, value in item.items() if key not in {
            "future", "expiresAtMono", "approvalScope", "approvalConstraints", "approvalPolicy",
        }})
        if redact and out.get("sensitive"):
            out.update(title="敏感交互", body="", conversationTitle="", options=[], questions=[], defaultValue="", defaultValues=[], defaultIndexes=[])
            if "result" in out:
                out["result"] = redact_result(out["result"])
        return out

    async def _emit(self, event: str, item: dict[str, Any]) -> None:
        # Notifications never receive sensitive question bodies or answer values.
        public = self._public(item, redact=True)
        for listener in self._listeners:
            try:
                await listener(event, copy.deepcopy(public))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log exception text: transports may include response data.
                log.warning("交互事件投递失败", interaction_event=event, interaction_id=item["interactionId"], error_type=type(exc).__name__)

    async def start(self) -> None:
        self._closed = False
        # Recovery changes only records owned by a previous process. Never adopt
        # a dead waiter's authorization or accept an answer for a vanished run.
        async with self.db.write_transaction(label="interaction_recovery") as conn:
            cur = await conn.execute("SELECT * FROM user_interactions WHERE status='pending'")
            rows = [dict(row) for row in await cur.fetchall()]
            for row in rows:
                item = self._from_row(row)
                result = empty_result(item, "interrupted")
                await conn.execute(
                    "UPDATE user_interactions SET status='interrupted', revision=revision+1, result_json=?, resolved_at_ms=? WHERE interaction_id=? AND status='pending'",
                    (_json(result), _now_ms(), item["interactionId"]),
                )
        for row in rows:
            item = self._from_row(row)
            item.update(status="interrupted", revision=int(item["revision"]) + 1, result=empty_result(item, "interrupted"))
            await self._emit("resolved", item)

    async def stop(self) -> None:
        events = []
        async with self._lock:
            self._closed = True
            self._amended_approvals.clear()
            for interaction_id in list(self.pending):
                item = await self._terminate_locked(interaction_id, "interrupted")
                if item is not None:
                    events.append(item)
        for item in events:
            await self._emit("resolved", item)

    def pending_for(self, conversation_uuid: str) -> list[dict[str, Any]]:
        now = _now_ms()
        items = [self.pending[key] for key in self.by_conversation.get(conversation_uuid, ()) if key in self.pending]
        return sorted(
            [self._public(item) for item in items if item["status"] == "pending" and item["expiresAtMs"] > now],
            key=lambda item: item["expiresAtMs"],
        )

    @staticmethod
    def _from_row(row: dict[str, Any]) -> dict[str, Any]:
        item = json.loads(row["payload_json"])
        item.update(
            interactionId=row["interaction_id"], confirmationId=row["interaction_id"],
            ownerChatId=int(row["owner_chat_id"]), conversationUuid=row["conversation_uuid"],
            turnUuid=row["turn_uuid"], toolCallId=row["tool_call_id"],
            status=row["status"], revision=int(row["revision"]), expiresAtMs=int(row["expires_at_ms"]),
        )
        if row.get("result_json"):
            item["result"] = json.loads(row["result_json"])
        return item

    async def _row(self, interaction_id: str, conn: Any = None) -> dict[str, Any] | None:
        cur = await (conn or self.db.conn).execute("SELECT * FROM user_interactions WHERE interaction_id=?", (interaction_id,))
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def get(self, interaction_id: str, *, owner_chat_id: int | None = None) -> dict[str, Any] | None:
        # Even while pending, adapters only see the safe projection. Web obtains
        # its editable input through authenticated pending_for(conversation).
        item = self.pending.get(interaction_id)
        if item is None:
            row = await self._row(interaction_id)
            if row is None:
                return None
            item = self._from_row(row)
        if owner_chat_id is not None and int(item["ownerChatId"]) != int(owner_chat_id):
            return None
        return self._public(item, redact=True)

    async def request(
        self, payload: dict[str, Any], *, owner_chat_id: int, conversation_uuid: str,
        conversation_title: str = "", turn_uuid: str = "", tool_call_id: str = "",
    ) -> dict[str, Any]:
        if self._closed:
            return {"status": "error", "error": "interaction_service_stopped", "confirmed": False}
        item, errors = normalize_definition(payload)
        if errors:
            return {"status": "error", "error": "invalid_questionnaire" if payload.get("action") == "questionnaire" else "invalid_interaction", "details": errors}
        if not conversation_uuid or not owner_chat_id:
            return {"status": "error", "error": "missing_interaction_owner", "confirmed": False}
        if payload.get("_approvalId") is not None:
            return await self._resume_approval(payload, item, owner_chat_id, conversation_uuid, turn_uuid)
        cid = secrets.token_urlsafe(12).replace("-", "_")
        created = _now_ms()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        item.update(
            interactionId=cid, confirmationId=cid, ownerChatId=int(owner_chat_id),
            conversationUuid=conversation_uuid, conversationTitle=conversation_title,
            turnUuid=turn_uuid, toolCallId=tool_call_id, revision=1, status="pending",
            expiresAtMs=created + max(1, int(item["timeoutSeconds"] * 1000)),
            expiresAtMono=time.monotonic() + item["timeoutSeconds"], future=future,
        )
        if item.get("requiresAuthorization") and payload.get("_approvalScope"):
            item["approvalScope"] = copy.deepcopy(payload["_approvalScope"])
            item["approvalConstraints"] = copy.deepcopy(payload.get("_approvalConstraints", {}))
            item["approvalPolicy"] = payload.get("_approvalPolicy", "native_scope")
        async with self._lock:
            if self._closed:
                return {"status": "error", "error": "interaction_service_stopped", "confirmed": False}
            async with self.db.write_transaction(label="interaction_create") as conn:
                await conn.execute(
                    "INSERT INTO user_interactions(interaction_id,owner_chat_id,conversation_uuid,turn_uuid,tool_call_id,payload_json,status,revision,expires_at_ms,created_at_ms) VALUES(?,?,?,?,?,?,'pending',1,?,?)",
                    (cid, int(owner_chat_id), conversation_uuid, turn_uuid, tool_call_id,
                     _json(self._public(item, redact=True)), item["expiresAtMs"], created),
                )
            self.pending[cid] = item
            self.by_conversation.setdefault(conversation_uuid, set()).add(cid)
        try:
            await self._emit("created", item)
            remaining = max(0, item["expiresAtMono"] - time.monotonic())
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
            except TimeoutError:
                await self.terminate(cid, "timeout")
                # A valid submit may have committed while the timer fired.
                return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(self.terminate(cid, "interrupted"))
            raise
        finally:
            # Covers cancellation of event publication or unexpected waiter errors.
            if cid in self.pending:
                await asyncio.shield(self.terminate(cid, "interrupted"))

    async def _resume_approval(self, payload, item, owner, conversation, turn):
        """Consume a trusted user receipt, never a model-supplied boolean."""
        cid = payload.get("_approvalId")
        if not isinstance(cid, str):
            return {"status": "error", "confirmed": False, "error": "invalid_amended_approval"}
        async with self._lock:
            self._prune_approvals()
            receipt = self._amended_approvals.get(cid)
            if (self._closed or not receipt or not item.get("requiresAuthorization") or not turn
                    or receipt["ownerChatId"] != owner or receipt["conversationUuid"] != conversation
                    or receipt["turnUuid"] != turn or receipt["sourceTool"] != item["sourceTool"]
                    or receipt["action"] != item["action"]):
                return {"status": "error", "confirmed": False, "error": "amended_approval_invalid_or_out_of_scope"}
            if (receipt["approvalScope"] != payload.get("_approvalScope")
                    or not constraints_within(receipt["approvalConstraints"], payload.get("_approvalConstraints", {}))):
                exact = receipt["approvalPolicy"] == "exact_arguments"
                return {"status": "error", "confirmed": False,
                        "error": "amended_approval_arguments_changed" if exact else "amended_approval_invalid_or_out_of_scope",
                        "message": ("通用 MCP 回执仅限原业务参数。保留全部附言，用修订参数不带 _openbearApprovalId 重新调用工具取得确认；不得为复用回执恢复冲突旧参数。"
                                    if exact else "修订请求超出此回执可验证的范围。保留全部附言，不要恢复冲突旧方案；需明确确认新的范围。")}
            self._amended_approvals.pop(cid)
            # Old text is retained in the original answer; the caller has now
            # explicitly resubmitted after applying it. Do not carry permanent
            # trust choices from an amended MCP request into this one-use grant.
            result = copy.deepcopy(receipt["result"])
            result.update(confirmed=True, decision="confirm", choice="confirm", text="",
                          requiresAmendment=False, authorizationResumed=True,
                          appliedConditions=result["text"])
            result.pop("instruction", None)
            if item["sourceTool"] == "MCP":
                result.update(selectedValues=["once"], selectedIndexes=[0], selectedLabels=["仅本次允许"])
            return result

    def _prune_approvals(self):
        now = _now_ms()
        self._amended_approvals = {cid: receipt for cid, receipt in self._amended_approvals.items()
                                  if receipt["approvalExpiresAtMs"] > now}

    @staticmethod
    def _digest(result: dict[str, Any]) -> str:
        value = {key: val for key, val in result.items() if key not in {"source"}}
        return hashlib.sha256(_json(value).encode()).hexdigest()

    def _complete_local(self, item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        cid = item["interactionId"]
        item.update(status=result["status"], revision=int(item["revision"]) + 1, result=result)
        self.pending.pop(cid, None)
        ids = self.by_conversation.get(item["conversationUuid"])
        if ids is not None:
            ids.discard(cid)
            if not ids:
                self.by_conversation.pop(item["conversationUuid"], None)
        future = item.get("future")
        if future is not None and not future.done():
            future.set_result(copy.deepcopy(result))
        return item

    async def submit(
        self, interaction_id: str, owner_chat_id: int, answer: dict[str, Any], *, source: str = "web",
    ) -> dict[str, Any]:
        if source not in {"web", "telegram"}:
            return {"ok": False, "statusCode": 400, "error": "invalid_interaction_source"}
        event_item: dict[str, Any] | None = None
        async with self._lock:
            async with self.db.write_transaction(label="interaction_submit") as conn:
                row = await self._row(interaction_id, conn)
                if row is None or int(row["owner_chat_id"]) != int(owner_chat_id):
                    return {"ok": False, "statusCode": 404, "error": "confirmation_not_found"}
                item = self.pending.get(interaction_id) or self._from_row(row)
                if item.get("sensitive") and source == "telegram":
                    return {"ok": False, "statusCode": 403, "error": "sensitive_interaction_web_only"}
                result, errors = canonical_answer(item, answer, source=source)
                digest = "" if item.get("sensitive") else self._digest(result)
                if row["status"] != "pending":
                    if not errors and digest and row["answer_digest"] == digest:
                        return {"ok": True, "statusCode": 200, "result": json.loads(row["result_json"]), "replayed": True}
                    return {"ok": False, "statusCode": 409, "error": "confirmation_expired" if row["status"] in {"timeout", "interrupted"} else "confirmation_already_resolved", "result": json.loads(row["result_json"] or "{}")}
                if interaction_id not in self.pending or _now_ms() >= int(row["expires_at_ms"]):
                    status = "interrupted" if interaction_id not in self.pending else "timeout"
                    result = empty_result(item, status)
                    digest = ""
                    response = {"ok": False, "statusCode": 409, "error": "confirmation_expired", "result": result}
                else:
                    if not isinstance(answer, dict):
                        return {"ok": False, "statusCode": 400, "error": "invalid_interaction_answer", "details": errors}
                    if "revision" in answer and (type(answer["revision"]) is not int or answer["revision"] != int(row["revision"])):
                        return {"ok": False, "statusCode": 409, "error": "confirmation_revision_conflict"}
                    if errors:
                        return {"ok": False, "statusCode": 400, "error": "invalid_questionnaire_answer" if item["action"] == "questionnaire" else "invalid_interaction_answer", "details": errors, "message": errors[0]["message"]}
                    response = {"ok": True, "statusCode": 200, "result": result}
                public_result = redact_result(result) if item.get("sensitive") else result
                cur = await conn.execute(
                    "UPDATE user_interactions SET status=?,revision=revision+1,result_json=?,answer_digest=?,resolved_at_ms=? WHERE interaction_id=? AND status='pending' AND revision=?",
                    (result["status"], _json(public_result), digest, _now_ms(), interaction_id, row["revision"]),
                )
                if cur.rowcount != 1:
                    return {"ok": False, "statusCode": 409, "error": "confirmation_already_resolved"}
            if (result.get("decision") == "confirm_with_changes" and result.get("authorizationGranted")
                    and item.get("requiresAuthorization") and item.get("approvalScope") and item.get("turnUuid")):
                self._prune_approvals()
                receipt = self._public(item)
                receipt.update(approvalScope=copy.deepcopy(item["approvalScope"]),
                               approvalConstraints=copy.deepcopy(item["approvalConstraints"]),
                               approvalPolicy=item["approvalPolicy"],
                               result=copy.deepcopy(result), approvalExpiresAtMs=_now_ms() + 3_600_000)
                self._amended_approvals[interaction_id] = receipt
            event_item = self._complete_local(item, result)
        if event_item is not None:
            await self._emit("resolved", event_item)
        return response

    async def terminate(self, interaction_id: str, status: str = "cancelled") -> None:
        if status not in {"cancelled", "timeout", "interrupted"}:
            raise ValueError("invalid interaction terminal status")
        async with self._lock:
            event_item = await self._terminate_locked(interaction_id, status)
        if event_item is not None:
            await self._emit("resolved", event_item)

    async def _terminate_locked(self, interaction_id: str, status: str):
        """Caller owns _lock. Notification callbacks must run after releasing it."""
        item = self.pending.get(interaction_id)
        if item is None:
            return None
        result = empty_result(item, status)
        async with self.db.write_transaction(label="interaction_terminate") as conn:
            cur = await conn.execute(
                "UPDATE user_interactions SET status=?,revision=revision+1,result_json=?,resolved_at_ms=? WHERE interaction_id=? AND status='pending'",
                (status, _json(redact_result(result) if item.get("sensitive") else result), _now_ms(), interaction_id),
            )
            if cur.rowcount != 1:
                return None
        return self._complete_local(item, result)

    async def cancel_conversation(self, conversation_uuid: str, *, status: str = "cancelled") -> None:
        if status not in {"cancelled", "timeout", "interrupted"}:
            raise ValueError("invalid interaction terminal status")
        events = []
        async with self._lock:
            # Linearize revocation with submit/consume, including a submit that
            # held the lock while waiting for its DB transaction to finish.
            self._amended_approvals = {cid: receipt for cid, receipt in self._amended_approvals.items()
                                      if receipt["conversationUuid"] != conversation_uuid}
            for cid in list(self.by_conversation.get(conversation_uuid, ())):
                item = await self._terminate_locked(cid, status)
                if item is not None:
                    events.append(item)
        for item in events:
            await self._emit("resolved", item)

    async def prune(self, *, before_ms: int | None = None) -> int:
        before = before_ms if before_ms is not None else _now_ms() - 30 * 86_400_000
        async with self.db.write_transaction(label="interaction_prune") as conn:
            cur = await conn.execute("DELETE FROM user_interactions WHERE status!='pending' AND resolved_at_ms<?", (before,))
            return max(0, cur.rowcount)
