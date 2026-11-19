"""Artifact access to the viewer's tools and MCP servers.

An artifact is HTML that the model wrote and the browser renders inside a sandboxed
``srcdoc`` iframe. It holds no session and no credentials, so it cannot call the API
itself. Instead it asks the host page for a capability, the host re-issues the request
over HTTP with the viewer's own session, and this module resolves and executes it
through the very same pipeline the chat uses.

Nothing here trusts the artifact:

* the tool allowlist comes with the request but every id is re-resolved with the same
  access-grant checks the chat performs;
* execution is delegated to ``execute_tool_call`` so argument validation, MCP handling
  and result normalization stay identical to a tool call made by the model;
* time, payload size and result size are bounded.

The pure, testable half lives in ``artifact_selection``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from open_webui.env import (
    ARTIFACT_TOOL_MAX_ARGUMENT_BYTES,
    ARTIFACT_TOOL_MAX_RESULT_BYTES,
    ARTIFACT_TOOL_TIMEOUT,
    ENABLE_ARTIFACT_TOOLS,
    ENABLE_TOOLS,
    ENABLE_TOOL_SERVERS,
)
from open_webui.models.users import UserModel
from open_webui.utils.artifact_selection import (
    MAX_ARTIFACT_TOOL_IDS,
    MCP_PREFIX,
    is_valid_tool_name,
    normalize_tool_ids,
    public_tool_spec,
    serialize_tool_result,
)
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.tools import connect_mcp_server, get_tools, mcp_tool_name

log = logging.getLogger(__name__)

__all__ = [
    'MAX_ARTIFACT_TOOL_IDS',
    'artifacts_available',
    'artifacts_enabled',
    'disconnect_mcp_clients',
    'execute_artifact_tool',
    'is_valid_tool_name',
    'normalize_tool_ids',
    'public_tool_spec',
    'resolve_artifact_tools',
    'serialize_tool_result',
]


def artifacts_enabled() -> bool:
    return ENABLE_ARTIFACT_TOOLS


def artifacts_available() -> bool:
    """Artifacts need at least one tool source enabled to be worth bridging."""
    return ENABLE_ARTIFACT_TOOLS and (ENABLE_TOOLS or ENABLE_TOOL_SERVERS)


def _extra_params(user: Any, request, metadata: dict) -> dict:
    """The subset of chat-time parameters ``get_tools``/``connect_mcp_server`` read."""
    return {
        '__user__': user.model_dump() if isinstance(user, UserModel) else dict(user),
        '__request__': request,
        '__metadata__': metadata,
        '__files__': [],
        '__messages__': [],
    }


def _make_mcp_tool_function(client, function_name: str):
    async def tool_function(**kwargs):
        return await client.call_tool(function_name, function_args=kwargs)

    return tool_function


def _filter_mcp_client(client, allowed_names: set[str]) -> None:
    """Mirror the chat-side ``function_name_filter_list`` for a single server id."""
    client.tool_definitions = {
        name: definition
        for name, definition in (client.tool_definitions or {}).items()
        if name in allowed_names
    }


async def resolve_artifact_tools(
    request,
    user: Any,
    tool_ids: list[str],
    metadata: dict | None = None,
) -> tuple[dict[str, dict], dict[str, Any]]:
    """Resolve an artifact's tool allowlist into callables.

    Returns ``(tools_dict, mcp_clients)``. The caller owns the MCP clients and must
    ``disconnect()`` them; anything denied by access control is skipped, never raised,
    so one stale id cannot break the whole artifact.
    """
    tool_ids = normalize_tool_ids(tool_ids)
    if not tool_ids:
        return {}, {}

    metadata = metadata or {}
    extra_params = _extra_params(user, request, metadata)

    tools_dict: dict[str, dict] = {}
    mcp_clients: dict[str, Any] = {}
    local_tool_ids: list[str] = []

    for tool_id in tool_ids:
        if not tool_id.startswith(MCP_PREFIX):
            local_tool_ids.append(tool_id)
            continue

        # 'server:mcp:<id>' or 'server:mcp:<id>|tool_a,tool_b'
        server_part = tool_id[len(MCP_PREFIX) :]
        server_id, _, filter_list = server_part.partition('|')
        server_id = server_id.strip()
        allowed_names = {name.strip() for name in filter_list.split(',') if name.strip()}

        if not server_id or server_id in mcp_clients:
            continue

        try:
            connection = await connect_mcp_server(request, server_id, user, extra_params)
        except HTTPException as e:
            # Auth-required surfaces to the user; everything else is skipped quietly.
            if e.status_code == status.HTTP_401_UNAUTHORIZED:
                raise
            log.warning('Artifact MCP server %s unavailable: %s', server_id, e.detail)
            continue
        except Exception:
            log.exception('Artifact MCP server %s failed to connect', server_id)
            continue

        if connection is None:
            continue

        client, tool_specs = connection
        if allowed_names:
            tool_specs = [spec for spec in tool_specs if spec.get('name') in allowed_names]
            _filter_mcp_client(client, allowed_names)

        mcp_clients[server_id] = client
        for tool_spec in tool_specs:
            function_name = tool_spec.get('name')
            if not isinstance(function_name, str) or not function_name:
                continue
            name = mcp_tool_name(server_id, function_name)
            tools_dict[name] = {
                'spec': {**tool_spec, 'name': name},
                'callable': _make_mcp_tool_function(client, function_name),
                'type': 'mcp',
                'client': client,
                'tool_id': tool_id,
            }

    if local_tool_ids and ENABLE_TOOLS:
        tools_dict.update(await get_tools(request, local_tool_ids, user, extra_params))

    return tools_dict, mcp_clients


async def disconnect_mcp_clients(mcp_clients: dict[str, Any]) -> None:
    """Close artifact-owned MCP sessions; one failure never skips the rest."""
    for client in reversed(list((mcp_clients or {}).values())):
        try:
            await client.disconnect()
        except BaseException as e:  # noqa: BLE001 - cancellation must still propagate below
            log.debug('Error disconnecting artifact MCP client: %s', e)


async def execute_artifact_tool(
    request,
    user: Any,
    tools: dict[str, dict],
    name: str,
    arguments: dict | None,
    metadata: dict | None = None,
) -> dict:
    """Run one already-resolved tool and return a bridge-friendly envelope."""
    # Imported here: middleware imports tools.py, which this module also imports.
    from open_webui.utils.middleware import execute_tool_call, process_tool_result

    if not is_valid_tool_name(name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid tool name')

    tool = tools.get(name)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Tool not available to this artifact')

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tool arguments must be an object')

    serialized_arguments = JSONCodec.dumps(arguments, ensure_ascii=False)
    if len(serialized_arguments.encode()) > ARTIFACT_TOOL_MAX_ARGUMENT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Tool arguments are too large')

    metadata = metadata or {}
    tool_metadata = {
        **metadata,
        'tools': tools,
        'files': [],
        # The host already asked the human; the artifact cannot approve itself.
        'params': {**metadata.get('params', {}), 'tool_approval_mode': 'full'},
    }
    form_data = {'messages': [], 'model': metadata.get('model_id') or ''}
    tool_call = {
        'name': name,
        'arguments': serialized_arguments,
        'call_id': str(uuid4()),
    }

    started = time.perf_counter()
    try:
        async with asyncio.timeout(ARTIFACT_TOOL_TIMEOUT):
            _, raw_result, _, tool_type = await execute_tool_call(form_data, tool_metadata, None, tool_call)
            result, files, embeds = await process_tool_result(
                request,
                name,
                raw_result,
                tool_type or '',
                tool_metadata,
                user,
            )
    except TimeoutError as e:
        log.warning('Artifact tool %s timed out after %ss', name, ARTIFACT_TOOL_TIMEOUT)
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail='Tool call timed out') from e

    duration = round(time.perf_counter() - started, 4)
    ok = tool_call.get('status') == 'completed'

    log.info(
        'Artifact tool call: user=%s chat=%s message=%s tool=%s ok=%s duration=%.3fs',
        getattr(user, 'id', ''),
        metadata.get('chat_id') or '',
        metadata.get('message_id') or '',
        name,
        ok,
        duration,
    )

    return {
        'name': name,
        'ok': ok,
        'result': serialize_tool_result(name, result, ARTIFACT_TOOL_MAX_RESULT_BYTES),
        'files': files or [],
        'embeds': embeds or [],
        'duration': duration,
    }
