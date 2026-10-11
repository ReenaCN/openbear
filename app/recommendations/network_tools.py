"""User-assigned network roles; no tool-name or description capability guessing."""
from __future__ import annotations

from app.recommendations.contracts import NetworkTools

# Compatibility ONLY for tools already allowed in an existing recommendation folder.
# New installations never auto-select these (or any other) tools.
_LEGACY = {'search': 'mcp__parrot__web_search', 'fetch': 'mcp__parrot__web_fetch'}


def legacy_selection(allowed):
    return NetworkTools(**{role: [name] if name in allowed else [] for role, name in _LEGACY.items()})


def tool_options(registry):
    """Main registry is authoritative; MCP connection/deny states refine availability.

    This is not a network probe or an approval grant. Dispatch keeps the original
    registry handlers and all their existing permission/confirmation checks.
    """
    if registry is None:
        return []
    summaries = registry.summaries(scope='main')
    mcp_names = set(registry.names(scope='main', source='mcp'))
    manager = getattr(registry, 'mcp_manager', None)
    states, metas, enabled = {}, {}, False
    if manager is not None:
        snapshot = manager.status_snapshot()
        enabled = snapshot.enabled
        states = {server.key: server for server in snapshot.servers}
        metas = {meta.public_name: meta for meta in manager.available_tools()}
    out = []
    for name, description in summaries.items():
        if name == 'Recommendation':
            continue  # Always required, not a user-selectable network role.
        available = True
        if name in mcp_names:
            meta = metas.get(name)
            state = states.get(meta.server_key) if meta else None
            identity = getattr(registry, 'mcp_tool_identities', {}).get(name)
            available = bool(enabled and meta and not meta.filtered and meta.approval != 'deny'
                             and state and state.status == 'connected' and state.approval != 'deny'
                             and identity == (meta.server_key, meta.original_tool_name))
        out.append({'name': name, 'description': description, 'source': 'mcp' if name in mcp_names else 'builtin',
                    'available': available})
    return out
