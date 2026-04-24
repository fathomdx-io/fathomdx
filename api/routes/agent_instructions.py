"""Agent instructions endpoint.

Serves the canonical voice / tool-guide block for a given surface
(claude-code, dashboard chat, future surfaces). One source of truth in
api/agent_instructions.py — clients fetch on boot so updates ship without
republishing hook scripts.
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import agent_instructions

router = APIRouter()


@router.get("/v1/agent-instructions")
async def get_agent_instructions(surface: str = agent_instructions.DEFAULT_SURFACE):
    return {
        "surface": surface,
        "text": agent_instructions.get(surface),
    }
