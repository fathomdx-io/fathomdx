"""Proposal-card endpoints — Edit / Deny / Approve for witness-emitted
state-change proposals.

When the witness picks `route:tool:<name>`, it writes a feed-card with
tags `[kind:proposal, tool:<name>, proposal-status:pending]` and the
structured `tool_args` in the payload. The dashboard renders these as
cards with three buttons. Approve → call the tool handler with
`confirm:true` (using either the original or user-edited args). Deny
→ write a decision delta. Both write a `proposal-decision` delta
linked to the original via `decides:<proposal-delta-id>` so subsequent
renders can collapse the card.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, delta_client
from .. import routines as routines_mod
from .._tags import tag_suffix

router = APIRouter()


def _proposal_tool(tags: list[str]) -> str:
    """Pull the tool name off a proposal card's tags."""
    return tag_suffix(tags, "tool:") or ""


def _proposal_action(tags: list[str]) -> str:
    return tag_suffix(tags, "action:") or ""


async def _load_proposal(delta_id: str) -> dict:
    """Fetch the original proposal-card delta. 404 if missing or wrong shape."""
    try:
        d = await delta_client.get_delta(delta_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail="proposal not found") from e
    tags = d.get("tags") or []
    if "kind:proposal" not in tags:
        raise HTTPException(
            status_code=400, detail="delta is not a proposal card"
        )
    return d


def _parse_args_from_card(d: dict) -> dict:
    """The witness writes tool_args inside the payload JSON content."""
    try:
        payload = json.loads(d.get("content") or "{}")
    except json.JSONDecodeError:
        return {}
    args = payload.get("tool_args")
    return args if isinstance(args, dict) else {}


async def _write_decision(*, proposal_id: str, status: str,
                          reason: str = "", result: dict | None = None) -> dict:
    """Write the decision delta linked to the original proposal."""
    tags = [
        "proposal-decision",
        f"decides:{proposal_id}",
        f"proposal-status:{status}",
    ]
    body = {"status": status, "reason": reason}
    if result:
        body["result"] = result
    return await delta_client.write(
        content=json.dumps(body, ensure_ascii=False),
        tags=tags,
        source="proposal-decision",
    )


@router.post(
    "/v1/proposals/{delta_id}/approve",
    dependencies=[Depends(auth.require_admin)],
)
async def approve_proposal(delta_id: str, body: dict | None = None):
    """Approve a proposal — call the tool handler with confirm:true.

    Optional body: `{"tool_args": {...}}` to apply edited args. Without
    that, the original args from the card are used as-is.
    """
    proposal = await _load_proposal(delta_id)
    tags = proposal.get("tags") or []
    tool = _proposal_tool(tags)
    args = (body or {}).get("tool_args")
    if not isinstance(args, dict) or not args:
        args = _parse_args_from_card(proposal)
    action = (args.get("action") or _proposal_action(tags) or "").strip()

    result: dict
    if tool == "routines":
        if action == "create":
            args.pop("action", None)
            args.pop("confirm", None)
            try:
                result = await routines_mod.create(args)
            except FileExistsError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        elif action == "update":
            rid = (args.get("id") or "").strip()
            if not rid:
                raise HTTPException(status_code=400, detail="id required")
            args = {k: v for k, v in args.items() if k not in ("action", "id", "confirm")}
            try:
                result = await routines_mod.update(rid, args)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        elif action == "delete":
            rid = (args.get("id") or "").strip()
            if not rid:
                raise HTTPException(status_code=400, detail="id required")
            try:
                result = await routines_mod.soft_delete(rid)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
        else:
            raise HTTPException(
                status_code=400, detail=f"unknown action: {action!r}"
            )
    else:
        raise HTTPException(
            status_code=400, detail=f"unknown tool: {tool!r}"
        )

    decision = await _write_decision(
        proposal_id=delta_id, status="approved", result=result
    )
    return {
        "approved": True,
        "tool": tool,
        "action": action,
        "result": result,
        "decision_delta_id": decision.get("id") if isinstance(decision, dict) else None,
    }


@router.post(
    "/v1/proposals/{delta_id}/deny",
    dependencies=[Depends(auth.require_admin)],
)
async def deny_proposal(delta_id: str, body: dict | None = None):
    """Deny a proposal — write a decision delta with status=denied."""
    await _load_proposal(delta_id)  # 404 if missing / wrong shape
    reason = ((body or {}).get("reason") or "").strip()
    decision = await _write_decision(
        proposal_id=delta_id, status="denied", reason=reason
    )
    return {
        "denied": True,
        "decision_delta_id": decision.get("id") if isinstance(decision, dict) else None,
    }


@router.get("/v1/proposals/{delta_id}")
async def get_proposal(delta_id: str):
    """Read a proposal + any decision that's been recorded against it."""
    proposal = await _load_proposal(delta_id)
    try:
        decisions = await delta_client.query(
            tags_include=[f"decides:{delta_id}"],
            limit=5,
        )
    except Exception:
        decisions = []
    latest_decision: dict | None = None
    for d in decisions:
        if latest_decision is None or d.get("timestamp", "") > latest_decision.get("timestamp", ""):
            latest_decision = d
    return {
        "proposal": proposal,
        "tool_args": _parse_args_from_card(proposal),
        "tool": _proposal_tool(proposal.get("tags") or []),
        "action": _proposal_action(proposal.get("tags") or []),
        "decision": latest_decision,
    }
