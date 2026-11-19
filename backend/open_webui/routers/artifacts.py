"""HTTP bridge between a rendered artifact and the viewer's tools/MCP servers.

The artifact iframe has no session, so the host page (``Artifacts.svelte``) calls these
endpoints on its behalf with the normal bearer token. Both endpoints resolve the tool
allowlist from scratch against the caller's access grants, so an artifact can never
reach a tool the viewer could not have selected in the chat composer.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import ARTIFACT_TOOL_RATE_LIMIT, ARTIFACT_TOOL_RATE_WINDOW, ARTIFACT_TOOL_TIMEOUT
from open_webui.internal.db import get_async_session
from open_webui.models.chats import Chats
from open_webui.utils.artifacts import (
    artifacts_available,
    disconnect_mcp_clients,
    execute_artifact_tool,
    public_tool_spec,
    resolve_artifact_tools,
)
from open_webui.utils.auth import get_verified_user
from open_webui.utils.rate_limit import RateLimiter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()

call_limiter = RateLimiter(limit=ARTIFACT_TOOL_RATE_LIMIT, window=ARTIFACT_TOOL_RATE_WINDOW)


def require_artifact_tools() -> None:
    if not artifacts_available():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Artifact tool access is disabled',
        )


class ArtifactToolListForm(BaseModel):
    # Artifacts are untrusted: reject anything the contract does not define.
    model_config = ConfigDict(extra='forbid')

    message_id: Optional[str] = None
    tool_ids: list[str] = Field(default_factory=list, max_length=64)


class ArtifactToolCallForm(ArtifactToolListForm):
    name: str = Field(min_length=1, max_length=256)
    arguments: dict = Field(default_factory=dict)


async def authorize_chat(chat_id: str, user, db: AsyncSession) -> None:
    """Only chats the viewer can read may drive its own tools.

    Temporary chats never reach the database; they still pass because the tool
    allowlist and every access grant are re-checked per user regardless.
    """
    chat = await Chats.get_chat_by_id(chat_id, db=db)
    if chat is None:
        return

    if not await Chats.has_chat_access(chat_id, user, db=db, permission='read', chat=chat):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )


async def limit_calls(request: Request, user) -> None:
    redis = getattr(request.app.state, 'redis', None)
    if await call_limiter.is_limited(redis, f'artifacts:tools:{user.id}'):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many artifact tool calls. Try again shortly.',
        )


def _metadata(chat_id: str, form_data: ArtifactToolListForm) -> dict:
    return {'chat_id': chat_id, 'message_id': form_data.message_id}


@router.post('/chats/{chat_id}/artifacts/tools/list')
async def list_artifact_tools(
    request: Request,
    chat_id: str,
    form_data: ArtifactToolListForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Describe the tools this artifact may call, without any credential material."""
    require_artifact_tools()
    await authorize_chat(chat_id, user, db)

    metadata = _metadata(chat_id, form_data)
    tools, mcp_clients = {}, {}
    try:
        tools, mcp_clients = await resolve_artifact_tools(request, user, form_data.tool_ids, metadata)
        return {
            'tools': [public_tool_spec(name, tool) for name, tool in tools.items()],
            'timeout': ARTIFACT_TOOL_TIMEOUT,
        }
    finally:
        await disconnect_mcp_clients(mcp_clients)


@router.post('/chats/{chat_id}/artifacts/tools/call')
async def call_artifact_tool(
    request: Request,
    chat_id: str,
    form_data: ArtifactToolCallForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Run one tool on behalf of the viewer and hand the result back to the artifact."""
    require_artifact_tools()
    await authorize_chat(chat_id, user, db)
    await limit_calls(request, user)

    metadata = _metadata(chat_id, form_data)
    tools, mcp_clients = {}, {}
    try:
        tools, mcp_clients = await resolve_artifact_tools(request, user, form_data.tool_ids, metadata)

        if form_data.name not in tools:
            log.warning(
                'Artifact asked for an unavailable tool: user=%s chat=%s tool=%s',
                user.id,
                chat_id,
                form_data.name,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Tool not available to this artifact',
            )

        return await execute_artifact_tool(
            request,
            user,
            tools,
            form_data.name,
            form_data.arguments,
            metadata,
        )
    finally:
        await disconnect_mcp_clients(mcp_clients)


@router.get('/artifacts/tools/config')
async def get_artifact_tools_config(user=Depends(get_verified_user)):
    """Cheap capability probe; the frontend also receives this through /api/config."""
    return {
        'enabled': artifacts_available(),
        'timeout': ARTIFACT_TOOL_TIMEOUT,
        'rate_limit': ARTIFACT_TOOL_RATE_LIMIT,
        'rate_window': ARTIFACT_TOOL_RATE_WINDOW,
    }
