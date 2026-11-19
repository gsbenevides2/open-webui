"""Pure shaping of an artifact's tool surface.

Deliberately dependency-free: this is the part that decides *what an untrusted artifact
may see and how much of a result it may receive*, so it must be trivially testable and
auditable. Anything that needs the chat pipeline lives in ``utils/artifacts.py``.
"""

from __future__ import annotations

import json
from typing import Any

# A chat cannot offer more tools than the UI can select; keep the fan-out bounded.
MAX_ARTIFACT_TOOL_IDS = 64
MAX_TOOL_ID_LENGTH = 512
MAX_TOOL_NAME_LENGTH = 256
DEFAULT_MAX_RESULT_BYTES = 2_097_152

MCP_PREFIX = 'server:mcp:'

_TRUNCATED_RESULT = {
    'truncated': True,
    'notice': 'The tool returned more data than an artifact may receive.',
}


def normalize_tool_ids(tool_ids: Any) -> list[str]:
    """Deduplicate and drop malformed ids.

    This is not an authorization step: each surviving id is resolved later with the
    viewer's access grants and silently skipped when it is not allowed.
    """
    if not isinstance(tool_ids, (list, tuple)):
        return []

    seen: set[str] = set()
    normalized: list[str] = []
    for tool_id in tool_ids:
        if not isinstance(tool_id, str):
            continue
        tool_id = tool_id.strip()
        if not tool_id or len(tool_id) > MAX_TOOL_ID_LENGTH or tool_id in seen:
            continue
        seen.add(tool_id)
        normalized.append(tool_id)
        if len(normalized) >= MAX_ARTIFACT_TOOL_IDS:
            break

    return normalized


def mcp_server_id(tool_id: str) -> str | None:
    """Extract the server id from ``server:mcp:<id>`` / ``server:mcp:<id>|tool``."""
    if not isinstance(tool_id, str) or not tool_id.startswith(MCP_PREFIX):
        return None
    return tool_id[len(MCP_PREFIX) :].partition('|')[0].strip() or None


def is_valid_tool_name(name: Any) -> bool:
    return isinstance(name, str) and bool(name) and len(name) <= MAX_TOOL_NAME_LENGTH


def public_tool_spec(name: str, tool: dict) -> dict:
    """A tool description safe to hand to artifact code.

    No secret, no valve, no connection data: name, description, JSON schema and the
    originating server (when it is an MCP server).
    """
    spec = tool.get('spec') or {}
    parameters = spec.get('parameters')
    if not isinstance(parameters, dict):
        parameters = {'type': 'object', 'properties': {}}

    server_id = mcp_server_id(tool.get('tool_id') or '')

    return {
        'name': name,
        'label': spec.get('name') or name,
        'description': spec.get('description') or '',
        'parameters': parameters,
        'type': tool.get('type') or ('mcp' if server_id else 'tool'),
        'server_id': server_id,
    }


def serialize_tool_result(
    name: str,
    result: Any,
    max_bytes: int = DEFAULT_MAX_RESULT_BYTES,
) -> Any:
    """Make a tool result JSON-safe and bounded before it crosses the bridge.

    stdlib ``json`` on purpose: this is a size gate shared with untrusted output, and it
    must behave identically regardless of the ``ENABLE_ORJSON`` deployment flag.
    """
    try:
        payload = json.dumps({'result': result})
    except (TypeError, ValueError):
        payload = json.dumps({'result': str(result)})

    if len(payload.encode()) > max_bytes:
        return dict(_TRUNCATED_RESULT)

    return json.loads(payload)['result']
