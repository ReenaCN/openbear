"""Explicit amended approval, from user submission through the real tool gate."""
import asyncio
import json

import pytest

from app.interaction_data import (
    canonical_answer,
    explicit_authorization,
    normalize_definition,
    redact_result,
)
from app.tools.base import ToolRegistry, ToolRuntimeContext, current_tool_context
from app.tools.confirmation import APPROVAL_ARGUMENT
from app.tools.openbear_control import register_openbear_control_tool
from app.user_interactions import InteractionService
from tests import test_interaction_telegram as tg
from tests import test_user_interactions as ui
from tests.test_openbear_control_tool import FakeSvc

interactions = ui.interactions
pending = ui.pending
db, config, capture = tg.db, tg.config, tg.capture


@pytest.mark.parametrize("source", ["web", "telegram"])
@pytest.mark.parametrize("action", ["confirm", "select"])
async def test_explicit_amended_approval_keeps_original_text_and_blocks_stale_gate(interactions, source, action):
    item, waiter = await pending(interactions, action, _requiresAuthorization=True)
    text = " 第一个不要加软链接，其他照做。\n不需要再次确权。 "
    answer = {"decision": "confirm_with_changes", "text": text}
    if action == "select":
        answer["selectedValues"] = ["a"]
    submitted = await interactions.submit(item["interactionId"], 123, answer, source=source)
    assert submitted["ok"]
    result = await waiter
    assert result["authorizationGranted"] is True
    assert result["requiresAmendment"] is True
    assert result["decision"] == "confirm_with_changes"
    assert result["text"] == text
    assert not explicit_authorization(result), "the waiting tool must not run its OLD arguments"
    assert (await interactions.get(item["interactionId"]))["result"]["decision"] == "confirm_with_changes"


@pytest.mark.parametrize("decision", ["feedback", "reject"])
def test_text_does_not_override_explicit_feedback_or_rejection(decision):
    item, _ = normalize_definition({"action": "confirm"})
    item["interactionId"] = "test"
    result, errors = canonical_answer(item, {"decision": decision, "text": "无需再次确认", "confirmed": True}, source="web")
    assert not errors
    assert result["decision"] == decision
    assert not result["confirmed"]
    assert not explicit_authorization(result)


@pytest.mark.parametrize("action", ["confirm", "select"])
def test_amended_approval_requires_text(action):
    item, _ = normalize_definition({"action": action, "options": ["a"], "_requiresAuthorization": True})
    item["interactionId"] = "test"
    _, errors = canonical_answer(item, {"decision": "confirm_with_changes", "text": "  ", "selectedValues": ["a"]}, source="web")
    assert errors


async def test_real_restart_gate_applies_change_without_second_prompt_and_cannot_replay(interactions):
    svc = FakeSvc()
    reg = ToolRegistry()
    register_openbear_control_tool(reg, svc)
    shown = []

    async def on_created(event, item):
        if event == "created":
            shown.append(item)
            await interactions.submit(item["interactionId"], 123, {
                "decision": "confirm_with_changes", "text": "改为延迟 9 秒重启，其余照做",
            })
    interactions.add_listener(on_created)

    async def confirm(payload):
        return await interactions.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn")
    ctx = ToolRuntimeContext(chat_id=123, session_uuid="s1", source="web", web_confirm=confirm)
    first = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "restart"}), context=ctx))
    assert first["status"] == "amendments_required"
    assert first["outcome"] == "not_started"
    assert svc.control_actions.pending_count(123) == 0
    cid = first["approvalId"]

    # Same ID cannot authorize a different action; a failed check does not spend it.
    wrong = json.loads(await reg.dispatch("OpenBearControl", json.dumps({"action": "new", APPROVAL_ARGUMENT: cid}), context=ctx))
    assert wrong["confirmation"]["error"] == "amended_approval_invalid_or_out_of_scope"
    assert svc.control_actions.pending_count(123) == 0
    amended = {"action": "restart", "args": {"delayS": 9}, APPROVAL_ARGUMENT: cid}
    second = json.loads(await reg.dispatch("OpenBearControl", json.dumps(amended), context=ctx))
    assert second["status"] == "scheduled"
    assert svc.control_actions.pending_count(123) == 1
    assert len(shown) == 1, "one user click, not a second confirmation"
    queued = svc.control_actions._after_turn[123]
    assert queued[0].args["delayS"] == 9

    third = json.loads(await reg.dispatch("OpenBearControl", json.dumps(amended), context=ctx))
    assert third["confirmation"]["error"] == "amended_approval_invalid_or_out_of_scope"
    assert svc.control_actions.pending_count(123) == 1


async def issue_receipt(service, *, source="OpenBearControl", action="confirm", sensitive=False):
    scope = {"tool": source, "action": "restart", "target": "current_run"}
    item, waiter = await pending(service, action, _requiresAuthorization=True,
                                 _sourceTool=source, _approvalScope=scope, sensitive=sensitive)
    answer = {"decision": "confirm_with_changes", "text": "先落实这条完整附言"}
    if action == "select":
        answer["selectedValues"] = ["a"]
    await service.submit(item["interactionId"], 123, answer)
    await waiter
    return {"action": action, "title": "修订", "body": "请选择或填写", "options": ["a", "b"],
            "_sourceTool": source, "_requiresAuthorization": True, "_approvalScope": scope,
            "_approvalId": item["interactionId"]}


@pytest.mark.parametrize("override", [
    {"owner_chat_id": 456}, {"conversation_uuid": "other"}, {"turn_uuid": "later"},
])
async def test_amended_receipt_cannot_cross_owner_conversation_or_turn(interactions, override):
    payload = await issue_receipt(interactions)
    context = {"owner_chat_id": 123, "conversation_uuid": "conv", "turn_uuid": "turn", **override}
    result = await interactions.request(payload, **context)
    assert result["confirmed"] is False
    valid = await interactions.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn")
    assert explicit_authorization(valid)


async def test_amended_receipt_is_one_use_under_concurrency(interactions):
    payload = await issue_receipt(interactions)
    results = await asyncio.gather(*[
        interactions.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn") for _ in range(2)
    ])
    assert sum(explicit_authorization(result) for result in results) == 1


async def test_cancel_and_process_restart_cannot_resurrect_approval(interactions):
    payload = await issue_receipt(interactions)
    restarted = InteractionService(interactions.db)
    result = await restarted.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn")
    assert not explicit_authorization(result)
    await interactions.cancel_conversation("conv")
    result = await interactions.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn")
    assert not explicit_authorization(result)


async def test_mcp_amended_approval_never_grants_persistent_trust(interactions):
    payload = await issue_receipt(interactions, source="MCP", action="select", sensitive=True)
    result = await interactions.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn")
    assert result["selectedValues"] == ["once"]
    assert result["text"] == ""
    assert result["appliedConditions"] == "先落实这条完整附言"
    cur = await interactions.db.conn.execute("SELECT payload_json,result_json FROM user_interactions")
    assert "先落实这条完整附言" not in str([tuple(row) for row in await cur.fetchall()])
    assert "先落实这条完整附言" not in json.dumps(redact_result(result), ensure_ascii=False)


async def test_adapter_strips_receipt_from_business_arguments_and_binds_native_resource(interactions):
    reg = ToolRegistry()
    executed = []
    async def business(args):
        answer = await current_tool_context().web_confirm({
            "_sourceTool": "Cron", "_requiresAuthorization": True, "title": "删除任务", "body": "固定删除影响",
        })
        if explicit_authorization(answer):
            executed.append(args)
        return json.dumps({"confirmation": answer})
    reg.add("Cron", "test", {"type": "object", "properties": {}}, business)
    async def listener(event, item):
        if event == "created":
            await interactions.submit(item["interactionId"], 123, {"decision": "confirm_with_changes", "text": "先保存副本，再删除"})
    interactions.add_listener(listener)
    async def confirm(payload):
        return await interactions.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn")
    ctx = ToolRuntimeContext(source="web", web_confirm=confirm)
    args = {"action": "delete", "params": {"jobId": "job-a"}}
    first = json.loads(await reg.dispatch("Cron", json.dumps(args), context=ctx))
    assert not executed
    wrong = {"action": "delete", "params": {"jobId": "job-b"}, APPROVAL_ARGUMENT: first["approvalId"]}
    await reg.dispatch("Cron", json.dumps(wrong), context=ctx)
    assert not executed
    await reg.dispatch("Cron", json.dumps({**args, APPROVAL_ARGUMENT: first["approvalId"]}), context=ctx)
    assert executed == [args]


@pytest.mark.parametrize("action", ["confirm", "select"])
async def test_telegram_explicit_amendment_button_reaches_real_service(db, config, capture, action):
    service = InteractionService(db)
    transport = tg.InteractionTelegram(config, db, capture.bot, service)
    service.add_listener(transport.on_interaction)
    await service.start()
    request_task = asyncio.create_task(service.request(
        {"action": action, "title": "迁移确认", "body": "原方案", "timeoutSeconds": 10,
         "_requiresAuthorization": True, "options": [{"label": "仅本次允许", "value": "once"}]},
        owner_chat_id=tg.OWNER, conversation_uuid="amended-tg", turn_uuid="turn",
    ))
    cid = await tg.wait_for_pending(service)
    await tg.wait_for_outbox(db, cid)
    await tg.drain(transport)
    draft, root = await tg.root_context(transport, cid)
    if action == "select":
        await tg.click(transport, capture, draft["callback_token"], "so:0", root["telegram_message_id"])
    reply = tg.FakeIncoming(transport.bot, text="不要软链接，其余执行", message_id=981,
                            reply_to=root["telegram_message_id"])
    await transport.handle_reply(reply, (await transport.match_reply(reply)) or {})
    draft = await transport._draft(cid)
    await tg.click(transport, capture, draft["callback_token"], "approve_changes", root["telegram_message_id"])
    result = await asyncio.wait_for(request_task, 1)
    assert result["decision"] == "confirm_with_changes"
    assert result["authorizationGranted"] is True
    assert result["text"] == "不要软链接，其余执行"
    assert not explicit_authorization(result)
    await service.stop()


async def test_real_mcp_gate_resumes_exact_arguments_once_without_trust_escalation(interactions):
    from app.mcp.manager import MCPManager
    from app.mcp.registry import register_mcp_tools
    from app.mcp.types import MCPRawResult, MCPToolMeta
    from tests.test_mcp import _cfg
    executed = []
    class Client:
        async def call_tool(self, name, arguments):
            executed.append((name, arguments))
            return MCPRawResult(content=[{"type": "text", "text": "done"}])
    manager = MCPManager(_cfg())
    meta = MCPToolMeta(public_name="mcp__serena__delete_symbol", server_key="serena",
                       original_tool_name="delete_symbol", normalized_tool_name="delete_symbol",
                       description="test", approval="ask", risk="destructive")
    manager._tools[meta.public_name] = meta
    manager._clients[meta.server_key] = Client()
    reg = ToolRegistry()
    register_mcp_tools(reg, manager)
    shown = []
    async def listener(event, item):
        if event == "created":
            shown.append(item)
            await interactions.submit(item["interactionId"], 123, {
                "decision": "confirm_with_changes", "selectedValues": ["always"],
                "text": "先保存副本，再删除 x 的指定重载，不修改别处",
            })
    interactions.add_listener(listener)
    async def confirm(payload):
        return await interactions.request(payload, owner_chat_id=123, conversation_uuid="conv", turn_uuid="turn")
    ctx = ToolRuntimeContext(chat_id=123, source="web", conversation_uuid="conv", web_confirm=confirm)
    first = json.loads(await reg.dispatch(meta.public_name, '{"name":"x","overload":"specific"}', context=ctx))
    assert first["status"] == "amendments_required"
    assert first["approvalPolicy"] == "exact_arguments"
    assert not executed
    assert not manager._has_conversation_grant(meta, ctx)
    amended = {"name": "x", "overload": "specific", APPROVAL_ARGUMENT: first["approvalId"]}
    assert await reg.dispatch(meta.public_name, json.dumps(amended), context=ctx) == "done"
    assert executed == [("delete_symbol", {"name": "x", "overload": "specific"})]
    assert len(shown) == 1
    assert not manager._has_conversation_grant(meta, ctx)
    replay = json.loads(await reg.dispatch(meta.public_name, json.dumps(amended), context=ctx))
    assert replay["status"] == "denied"
    assert len(executed) == 1
