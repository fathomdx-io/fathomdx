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
    """Fetch the original proposal-card delta. 404 if missing or wrong shape.

    Tries the lake first (the durable home). Falls back to the puddle so
    proposal cards work even when the witness's `lake-id:<full>` cross-
    pointer didn't land — e.g. a transient lake-write hiccup that still
    reached the puddle, or older puddle entries written before the
    proposal route gained its lake_id surfacing. The puddle copy carries
    the same tool/tool_args payload, so approve/deny can still reach
    the right tool handler.
    """
    try:
        d = await delta_client.get_delta(delta_id)
    except Exception:
        d = None
    if d is None:
        from ..loop.puddle import puddle as _puddle  # lazy — avoids cycle

        d = _puddle.get(delta_id)
    if d is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    tags = d.get("tags") or []
    if "kind:proposal" not in tags:
        raise HTTPException(status_code=400, detail="delta is not a proposal card")
    return d


def _parse_args_from_card(d: dict) -> dict:
    """The witness writes tool_args inside the payload JSON content."""
    try:
        payload = json.loads(d.get("content") or "{}")
    except json.JSONDecodeError:
        return {}
    args = payload.get("tool_args")
    return args if isinstance(args, dict) else {}


def _produced_by_from_tags(tags: list[str], default: str = "unknown") -> str:
    """Pull the producer name off a proposal card's tags."""
    for t in tags or []:
        if isinstance(t, str) and t.startswith("produced-by:"):
            return t.split(":", 1)[1] or default
    return default


def _provenance_level_from_tags(tags: list[str]) -> int | None:
    """Pull the proposed provenance level off the tags. Returns None when
    no `provenance-level:<n>` tag is present."""
    for t in tags or []:
        if isinstance(t, str) and t.startswith("provenance-level:"):
            try:
                return int(t.split(":", 1)[1])
            except (TypeError, ValueError):
                continue
    return None


async def _approve_provenance_create(
    args: dict,
    *,
    proposal: dict | None,
    produced_by: str = "reflective-agent",
    source: str | None = None,
) -> dict:
    """Write the real kind:provenance delta with the args being approved
    (which may have been edited from the proposal's original args).

    Mirrors the producer-maker workspace's write shape: kind:provenance
    + provenance-level:<n> + provenance-version:v1-experimental + a
    title slug + from:<id> for each constituent. Returns the new delta's
    id and tag list so the decision delta can record what landed.

    `produced_by` flows through to a `produced-by:<value>` tag — caller
    should pass the actual producer (harness, topical-agent, etc.) so
    later analysis can tell apart auto-approved batches by source.
    """
    title = (args.get("title") or "").strip()
    summary = (args.get("summary") or "").strip()
    level_raw = args.get("level")
    try:
        level = int(level_raw) if level_raw is not None else 1
    except (TypeError, ValueError):
        level = 1
    if level < 0 or level > 3:
        level = max(0, min(3, level))
    from_ids = [
        str(x).strip()
        for x in (args.get("from_ids") or [])
        if isinstance(x, (str, int)) and str(x).strip()
    ]

    if not title:
        raise ValueError("title is required")
    if not summary:
        raise ValueError("summary is required")
    if not from_ids:
        raise ValueError("from_ids must contain at least one constituent")

    title_slug = title.lower().replace(" ", "-")[:80]
    proposal_id = (proposal or {}).get("id") or ""

    tags = [
        "kind:provenance",
        f"provenance-level:{level}",
        "provenance-version:v1-experimental",
        f"produced-by:{produced_by}",
        f"title:{title_slug}",
    ]
    for fid in from_ids[:60]:  # generous cap; 60 sources is more than enough
        tags.append(f"from:{fid}")
    if proposal_id:
        tags.append(f"approved-from-proposal:{proposal_id}")

    content = f"{title}\n\n{summary}"
    if len(content) > 4000:
        content = content[:4000] + "…"

    # Compute the constituent centroid — average of from_ids' content
    # embeddings — so this provenance lives in the same neighborhood
    # as what it's associated with. Stored in the provenance_embedding
    # column; combined with the title+summary embedding (in the
    # `embedding` column) at search time via min(d_dist, p_dist) so
    # the provenance ranks on either substantive OR meta queries.
    from .. import provenance_centroid

    centroid = await provenance_centroid.compute_centroid(from_ids)

    written = await delta_client.write(
        content=content,
        tags=tags,
        source=source or f"{produced_by}-approved",
        provenance_embedding=centroid,
    )
    return {
        "delta_id": (written or {}).get("id") or "",
        "title": title,
        "level": level,
        "from_count": len(from_ids),
        "centroid_dim": len(centroid) if centroid else 0,
    }


async def auto_approve_provenance(
    *,
    proposal_id: str,
    args: dict,
    produced_by: str,
    decided_by: str = "auto-policy:level<=2",
) -> dict:
    """Auto-approve an L1/L2 provenance proposal at write time.

    Skips the human-review step: writes the kind:provenance delta with
    `produced_by` matching the proposing producer, then writes a
    proposal-decision delta tagged `decided-by:<value>` so the audit
    trail distinguishes auto-approval from operator approval.

    The proposal itself stays in the lake — the proposals pane folds in
    decisions client-side and the row will render as approved.
    """
    result = await _approve_provenance_create(
        args,
        proposal={"id": proposal_id} if proposal_id else None,
        produced_by=produced_by,
    )
    decision = await _write_decision(
        proposal_id=proposal_id,
        status="approved",
        result=result,
        decided_by=decided_by,
    )
    return {
        "auto_approved": True,
        "decided_by": decided_by,
        "result": result,
        "decision_delta_id": (decision or {}).get("id") if isinstance(decision, dict) else None,
    }


async def _write_decision(
    *,
    proposal_id: str,
    status: str,
    reason: str = "",
    result: dict | None = None,
    decided_by: str = "operator",
) -> dict:
    """Write the decision delta linked to the original proposal.

    `decided_by` distinguishes operator approvals from auto-approved
    proposals (e.g. "auto-policy:level<=2"). Goes onto a
    `decided-by:<value>` tag for later filtering.
    """
    tags = [
        "proposal-decision",
        f"decides:{proposal_id}",
        f"proposal-status:{status}",
        f"decided-by:{decided_by}",
    ]
    body = {"status": status, "reason": reason, "decided_by": decided_by}
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

    Body shape (all optional, but at least one of tool / tool_args.action
    must be discoverable):
      · tool: name of the tool to dispatch ("routines"). Wins over the
        proposal delta's tool tag when both present.
      · tool_args: the structured payload, with `action` inside.
      · The wizard-driven Edit flow always sends both, so approve
        works even if the proposal delta is no longer reachable in
        the lake (TTL, restart, eviction). When neither is in the
        body, fall back to loading the proposal delta and parsing
        its tags + payload.
    """
    body = body or {}
    body_tool = (body.get("tool") or "").strip()
    body_args = body.get("tool_args")
    body_args = body_args if isinstance(body_args, dict) else None

    # Try to load the proposal for context. Tolerate misses: if the
    # body provides everything we need to dispatch, the proposal lookup
    # is informational. If the body is sparse, the proposal must load.
    proposal = None
    try:
        proposal = await _load_proposal(delta_id)
    except HTTPException:
        if not (body_tool and body_args and body_args.get("action")):
            raise

    tags = (proposal.get("tags") if proposal else []) or []
    tool = body_tool or _proposal_tool(tags)
    args = body_args
    if not args:
        args = _parse_args_from_card(proposal) if proposal else {}
    action = (args.get("action") or _proposal_action(tags) or "").strip()

    result: dict
    if tool == "provenance":
        if action == "create":
            args.pop("action", None)
            args.pop("confirm", None)
            produced_by = _produced_by_from_tags(tags, default="reflective-agent")
            try:
                result = await _approve_provenance_create(
                    args,
                    proposal=proposal,
                    produced_by=produced_by,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        else:
            raise HTTPException(status_code=400, detail=f"unknown provenance action: {action!r}")
    elif tool == "routines":
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
            raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")
    elif tool == "helper-dispatch":
        if action == "run":
            import uuid as _uuid

            host = (args.get("host") or "").strip()
            role = (args.get("role") or "").strip()
            task = (args.get("task") or "").strip()
            if not host:
                raise HTTPException(status_code=400, detail="host required")
            if not role:
                raise HTTPException(status_code=400, detail="role required")
            if not task:
                raise HTTPException(status_code=400, detail="task required")
            # Approval materializes the dispatch as a feed-card delta
            # with the role-namespaced route tag. Each helper plugin
            # (kitty for claude-code, openclaw for openclaw, etc.)
            # filters on `tags_include=route:helper:<role>,host:<myhost>`
            # so a dispatch only reaches the plugin that owns its role.
            # The bare `route:helper` umbrella tag rides alongside so
            # role-agnostic consumers (the OpenAI endpoint's pending-
            # turn check, claude_code_watcher's corr lookup) can still
            # find ANY helper dispatch.
            corr = _uuid.uuid4().hex[:12]
            # `to:helper:<corr>` is the addressing tag the host's plugin
            # requires (see addons/agent/plugins/kitty.js, openclaw.js).
            # Without it the dispatch is logged "missing to:helper:<corr>"
            # and skipped.
            dispatch_tags = [
                "feed-card",
                f"route:helper:{role}",
                "route:helper",
                f"host:{host}",
                f"helper-role:{role}",
                "channel:helper",
                f"to:helper:{corr}",
                f"task-corr:{corr}",
                "kind:helper-dispatch",
                f"approved-from-proposal:{delta_id}",
                "produced-by:harness",
            ]
            # Propagate the originating chat surface from the proposal
            # so the watcher's closure-followup intent inherits the
            # routing info, and the harness's chat-reply lands back in
            # the user's thread instead of vanishing into the dashboard
            # feed. tool_dispatch_helper stamps these onto the proposal
            # at draft time; we just forward them here.
            try:
                proposal = await delta_client.get_delta(delta_id)
            except Exception:
                proposal = None
            for t in (proposal or {}).get("tags") or []:
                if t.startswith(("originating-channel:", "originating-correlation:", "originating-intent:")):
                    dispatch_tags.append(t)
            dispatch = await delta_client.write(
                content=task,
                tags=dispatch_tags,
                source="harness-helper-dispatch",
            )
            result = {
                "dispatched": True,
                "host": host,
                "role": role,
                "task_corr": corr,
                "task_chars": len(task),
                "dispatch_delta_id": (dispatch or {}).get("id"),
            }
        else:
            raise HTTPException(
                status_code=400,
                detail=f"unknown helper-dispatch action: {action!r}",
            )
    else:
        raise HTTPException(status_code=400, detail=f"unknown tool: {tool!r}")

    decision = await _write_decision(proposal_id=delta_id, status="approved", result=result)
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
    decision = await _write_decision(proposal_id=delta_id, status="denied", reason=reason)
    return {
        "denied": True,
        "decision_delta_id": decision.get("id") if isinstance(decision, dict) else None,
    }


@router.post(
    "/v1/proposals/draft",
    dependencies=[Depends(auth.require_admin)],
)
async def draft_proposal(body: dict):
    """Submit a proposal draft from a producer that runs out-of-process.

    The reflective agent (and future producers — topical agent, manual
    proposers) builds a proposal payload but lives in a separate Python
    process from the api. Without this endpoint they'd write to the
    lake successfully but couldn't echo to the api's in-process puddle,
    which is what the dashboard feed reads.

    This handler does the dual-write inside the api process: lake-write
    + puddle-echo + (optionally) a session-tag so the proposal lands in
    the live feed.

    Body shape (only `payload` and `tags` are mandatory; the producer
    composes the witness-card-shaped payload itself):
      · payload: dict — the JSON object the dashboard parses (kicker,
        title, body, tail, route, tool, tool_args, etc.)
      · tags: list[str] — base tags (without CONVO/session prefixes,
        which the handler appends for puddle echo)
      · source: str — defaults to "draft-proposal"
      · session_tag: str — optional; defaults to a fresh
        session:draft-... tag used only for the puddle entry

    Returns the lake delta id so the producer can reference it for
    later approve/deny calls.
    """
    import uuid

    from ..loop.intents import CONVO_TAG
    from ..loop.puddle import puddle as _puddle

    payload = body.get("payload")
    tags = body.get("tags") or []
    source = (body.get("source") or "draft-proposal").strip() or "draft-proposal"
    session_tag = (body.get("session_tag") or "").strip()
    if not session_tag:
        session_tag = f"session:draft-{uuid.uuid4().hex[:8]}"

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise HTTPException(status_code=400, detail="tags must be a list of strings")

    payload_json = json.dumps(payload, ensure_ascii=False)

    lake_delta = await delta_client.write(
        content=payload_json,
        tags=list(tags),
        source=source,
    )
    lake_id = (lake_delta or {}).get("id") or ""

    puddle_tags = [CONVO_TAG, session_tag, *tags]
    if lake_id:
        puddle_tags.append(f"lake-id:{lake_id}")
        puddle_tags.append(f"recalled-id:{lake_id[:24]}")
    try:
        await _puddle.write(
            content=payload_json,
            tags=puddle_tags,
            source=source,
            ttl_seconds=7 * 24 * 60 * 60,
        )
    except Exception as e:
        # Non-fatal — the lake write is the source of truth; puddle
        # is just for live feed visibility.
        print(f"[proposals/draft] puddle echo failed: {type(e).__name__}: {e}")

    # Auto-approve gate: L1 (episodes) and L2 (topics) are bounded
    # enough that operator review just creates friction. L3 eras and
    # higher still require explicit approval — they make stronger
    # claims about identity/structure that warrant a human pass.
    auto_approved: dict | None = None
    try:
        level_int = _provenance_level_from_tags(tags)
        tool_args = (payload.get("tool_args") or {}) if isinstance(payload, dict) else {}
        if (
            level_int is not None
            and level_int <= 2
            and isinstance(tool_args, dict)
            and (payload.get("tool") or "") == "provenance"
            and lake_id
        ):
            produced_by = _produced_by_from_tags(tags, default="unknown")
            auto_approved = await auto_approve_provenance(
                proposal_id=lake_id,
                args=dict(tool_args),
                produced_by=produced_by,
            )
    except Exception as e:
        # Non-fatal — proposal is in the lake, just unapproved. Log
        # and let the operator approve manually.
        print(f"[proposals/draft] auto-approve failed: {type(e).__name__}: {e}")
        auto_approved = None

    return {
        "lake_id": lake_id,
        "session_tag": session_tag,
        "auto_approved": auto_approved,
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
