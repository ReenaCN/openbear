"""MCP ToolRegistry adapter."""
from __future__ import annotations

from app.mcp.manager import MCPManager
from app.tools.base import ToolRegistry, current_tool_context


async def make_mcp_tool_call(manager: MCPManager, public_name: str, args: dict, identity: tuple[str, str] | None = None) -> str:
    ctx = current_tool_context()
    return await manager.call_tool(public_name, args or {}, ctx, expected_identity=identity)


def register_mcp_tools(registry: ToolRegistry, manager: MCPManager) -> int:
    registry.mcp_manager = manager
    count = 0
    for meta in manager.available_tools():
        identity = (meta.server_key, meta.original_tool_name)
        registry.mcp_tool_identities[meta.public_name] = identity
        server = manager.mcp_config.servers[meta.server_key]
        timeout_s = server.tool_call_timeout_s or manager.mcp_config.tool_call_timeout_s
        description = (
            f"{meta.description}\nOpenBear MCP transport deadline: {timeout_s:g} seconds per call. "
            "Tool timeout arguments cannot extend this limit; allow time for transport overhead. "
            "A transport timeout does not confirm remote cancellation; check the outcome before retrying. "
            "Amended approval receipts bind all business arguments exactly: after satisfying the user's full conditions, "
            "reuse only for identical arguments. If conditions require parameter changes, preserve them and call with "
            "the revised arguments without the receipt for a fresh tool-owned confirmation; never revert to conflicting old arguments."
        )
        registry.add(
            meta.public_name,
            description,
            meta.input_schema,
            lambda args, name=meta.public_name, identity=identity: make_mcp_tool_call(manager, name, args, identity),
            source="mcp",
            visibility={"main", "runtime"} | ({"agent"} if not manager.agent_tool_unavailable_reason(meta.public_name) else set()),
        )
        count += 1
    return count
