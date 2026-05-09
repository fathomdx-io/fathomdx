"""Helper inbox API + scoped helper-token issuance.

Helpers (kitty plugin, ACP plugin, anything else under
addons/agent/plugins/) used to query the lake directly with a
lake-write token. That tied helpers tightly to delta-store query
semantics and granted them way more authority than they need. This
module replaces both with a small, host-bound API.

Two surfaces:

  · Admin: /v1/admin/helpers/{host}/tokens (POST/GET/DELETE)
    Mints/lists/revokes helper-scoped tokens bound to a single host.
    Requires tokens:manage. Same admin permissions as /v1/tokens.

  · Helper: /v1/helpers/{host}/inbox (GET)
            /v1/helpers/{host}/inbox/{corr}/reply (POST)
    Pull the host's pending dispatches and post replies for them.
    Requires the `helper` scope AND the token's helper_host binding
    must match the path host (enforced by require_helper_host). All
    lake writes go through this endpoint — the helper itself never
    holds a lake-write token.

Lake stays canonical: the inbox endpoint queries the lake on read and
writes lake deltas on reply. Helpers never see raw lake tags or
arbitrary delta shapes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from .. import auth, delta_client

router = APIRouter()


# ── Admin: helper-token management ───────────────────


class HelperTokenCreate(BaseModel):
    name: str = ""
    contact_slug: str | None = None


@router.post(
    "/v1/admin/helpers/{host}/tokens",
    dependencies=[Depends(auth.require_admin)],
    status_code=201,
)
async def create_helper_token(host: str, body: HelperTokenCreate, request: Request):
    """Mint a helper-scoped token bound to <host>.

    Returns the raw token ONCE; subsequent calls only return the
    metadata. The `helper` scope is the only scope granted — no
    lake:read, no lake:write. The host binding is enforced at request
    time via require_helper_host.
    """
    host = host.strip()
    if not host:
        raise HTTPException(400, "host is required")
    caller = getattr(request.state, "contact", None)
    default_slug = (caller or {}).get("slug", "")
    slug = body.contact_slug or default_slug
    if not slug:
        raise HTTPException(400, "contact_slug required")
    try:
        return auth.create_token(
            name=body.name or f"helper@{host}",
            scopes=["helper"],
            contact_slug=slug,
            helper_host=host,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get(
    "/v1/admin/helpers/{host}/tokens",
    dependencies=[Depends(auth.require_admin)],
)
async def list_helper_tokens(host: str):
    """List active helper tokens for <host>. Excludes hashes."""
    host = host.strip()
    return [t for t in auth.list_tokens() if (t.get("helper_host") or "") == host]


@router.delete(
    "/v1/admin/helpers/{host}/tokens/{token_id}",
    dependencies=[Depends(auth.require_admin)],
)
async def revoke_helper_token(host: str, token_id: str):
    """Revoke a helper token by its short id. 404 if no match for this host."""
    host = host.strip()
    # Only allow deletion when the token's binding matches the path
    # host — otherwise an admin-level GET on the wrong host could lead
    # to confused token deletion. Lookup-then-delete keeps it explicit.
    rows = [
        t
        for t in auth.list_tokens()
        if t.get("id") == token_id and (t.get("helper_host") or "") == host
    ]
    if not rows:
        raise HTTPException(404, "Token not found for this host")
    if not auth.delete_token(token_id):
        raise HTTPException(404, "Token not found")
    return {"deleted": True}


# ── Helper: inbox ────────────────────────────────────


# Inbox window — dispatches older than this drop out of view. Aligns
# with the witness's helper-dispatch lifecycle (a dispatch that hasn't
# been picked up in 24h is almost certainly orphaned).
_INBOX_LOOKBACK_HOURS = 24


def _slim_dispatch(d: dict) -> dict | None:
    """Project a lake delta into the helper-facing shape.

    Returns None if the delta is missing fields a helper needs to act
    on (corr, role) — those get filtered out rather than surfaced as
    half-formed inbox items.
    """
    tags = d.get("tags") or []
    role = ""
    corr = ""
    proposal_id = ""
    for t in tags:
        if t.startswith("route:helper:") and not role:
            # route:helper:<role> — the bare `route:helper` umbrella
            # also matches startswith("route:helper:") for "route:helper"
            # itself (no trailing colon), so this guards against the
            # umbrella tag stealing the role parse.
            suffix = t[len("route:helper:") :]
            if suffix:
                role = suffix
        elif t.startswith("to:helper:") and not corr:
            corr = t[len("to:helper:") :]
        elif t.startswith("task-corr:") and not corr:
            corr = t[len("task-corr:") :]
        elif t.startswith("approved-from-proposal:") and not proposal_id:
            proposal_id = t[len("approved-from-proposal:") :]
    if not role or not corr:
        return None
    raw = (d.get("content") or "").strip()
    task = raw
    # Witness payloads are JSON envelopes — pull `body`. Approval-path
    # dispatches (proposals.py:approve) write the task as plain content,
    # so the JSON probe is the only branch that needs to do any work.
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                body = (obj.get("body") or obj.get("task") or "").strip()
                if body:
                    task = body
        except Exception:
            pass
    return {
        "delta_id": d.get("id") or "",
        "corr": corr,
        "role": role,
        "task": task,
        "ts": d.get("timestamp") or "",
        "kind": "dispatch",
        **({"proposal_id": proposal_id} if proposal_id else {}),
    }


@router.get("/v1/helpers/{host}/inbox")
async def get_inbox(
    request: Request,
    host: str = Path(...),
    since: str | None = None,
    limit: int = 50,
    role: str | None = None,
):
    """Return pending helper dispatches for <host>.

    Helper polls this endpoint on a short interval. `since` is the
    cursor returned from the previous response — pass it back to skip
    items already seen. `role` is an optional filter for plugins that
    only handle one role.

    Auth: helper scope + helper_host == path host (require_helper_host).
    """
    auth.require_helper_host(host, request)
    if limit <= 0 or limit > 200:
        limit = 50

    cutoff_iso = (datetime.now(UTC) - timedelta(hours=_INBOX_LOOKBACK_HOURS)).isoformat()
    time_start = since or cutoff_iso

    try:
        # Lake query semantics: tags_include is AND, so pulling on
        # `host:<host>` alone and post-filtering by route is cheaper
        # than enumerating every role. Helper output stays cheap as
        # the role count grows.
        deltas = await delta_client.query(
            tags_include=["route:helper", f"host:{host}"],
            time_start=time_start,
            limit=max(limit * 2, 50),
        )
        # Pull completion markers in the same window so we can hide
        # already-finished corrs from the inbox. Without this, helper
        # plugins would re-dispatch every completed task on every poll
        # — the lake query is time-cursor based and a delta at exactly
        # `since` would re-appear. Using corr-set filtering is more
        # robust than tightening the cursor's > vs >= semantics.
        completes = await delta_client.query(
            tags_include=[f"host:{host}"],
            time_start=cutoff_iso,
            limit=500,
        )
    except Exception as e:
        raise HTTPException(502, f"lake query failed: {type(e).__name__}: {e}") from e

    completed_corrs: set[str] = set()
    for d in completes:
        tags = d.get("tags") or []
        is_done = (
            "task-complete" in tags
            or "task-abandoned" in tags
            or "kind:helper-complete" in tags
            or "kind:helper-error" in tags
        )
        if not is_done:
            continue
        for t in tags:
            if t.startswith("task-corr:"):
                completed_corrs.add(t[len("task-corr:") :])
                break

    items: list[dict] = []
    latest_ts = since or ""
    # Lake returns newest-first; flip for a stable inbox cursor.
    for d in sorted(deltas, key=lambda x: x.get("timestamp") or ""):
        slim = _slim_dispatch(d)
        if slim is None:
            continue
        if role and slim["role"] != role:
            continue
        if slim["corr"] in completed_corrs:
            # Already finished — advance the cursor past it but don't
            # surface it as a fresh dispatch.
            ts = slim.get("ts") or ""
            if ts > latest_ts:
                latest_ts = ts
            continue
        items.append(slim)
        ts = slim.get("ts") or ""
        if ts > latest_ts:
            latest_ts = ts
        if len(items) >= limit:
            break

    return {"items": items, "cursor": latest_ts}


# Tags a helper may add to its reply. Strict allowlist — any other tag
# is silently dropped. Anything authority-bearing (engages, affirms,
# refutes, kind:proposal, etc.) is NOT here on purpose.
_REPLY_EXTRA_TAG_ALLOWLIST = {
    "claude-code-session",  # claude-code-session:<sid>
    "helper-session",  # helper-session:<sid>
    "project",  # project:<path>
    "task-spawn",
    "task-abandoned",
}


def _filter_extra_tags(extras: list[str]) -> list[str]:
    """Keep only tags whose prefix is in the allowlist.

    Tags are expected as `<prefix>:<value>` or bare `<prefix>`. Anything
    that doesn't match is dropped silently — no error, no surface.
    """
    out: list[str] = []
    seen: set[str] = set()
    for t in extras or []:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if not t or t in seen:
            continue
        prefix = t.split(":", 1)[0]
        if prefix not in _REPLY_EXTRA_TAG_ALLOWLIST:
            continue
        seen.add(t)
        out.append(t)
    return out


class InboxReply(BaseModel):
    kind: str = Field(..., description="update | complete | error")
    content: str = Field(..., description="Reply body — natural-language or structured.")
    extra_tags: list[str] = Field(default_factory=list)


@router.post(
    "/v1/helpers/{host}/inbox/{corr}/reply",
    status_code=201,
)
async def post_reply(
    request: Request,
    body: InboxReply,
    host: str = Path(...),
    corr: str = Path(...),
):
    """Post a helper reply (update / complete / error) for a dispatch.

    The server constructs the actual lake delta with controlled tags;
    the helper has no general lake-write authority. Role is resolved
    server-side from the original dispatch so the helper can't claim
    it ran a different role than it was given.
    """
    auth.require_helper_host(host, request)
    if body.kind not in ("update", "complete", "error"):
        raise HTTPException(400, "kind must be one of: update, complete, error")
    if not (body.content or "").strip() and body.kind != "complete":
        raise HTTPException(400, "content required for update / error replies")

    # Resolve the original dispatch by corr + host — the role came from
    # there, and we don't trust the helper to name it.
    try:
        dispatches = await delta_client.query(
            tags_include=[f"task-corr:{corr}", f"host:{host}"],
            time_start=(datetime.now(UTC) - timedelta(hours=_INBOX_LOOKBACK_HOURS)).isoformat(),
            limit=10,
        )
    except Exception as e:
        raise HTTPException(502, f"lake lookup failed: {type(e).__name__}: {e}") from e
    role = ""
    for d in dispatches:
        for t in d.get("tags") or []:
            if t.startswith("helper-role:") and not role:
                role = t[len("helper-role:") :]
                break
        if role:
            break

    base_tags: list[str] = [
        "helper-reply",
        f"kind:helper-{body.kind}",
        f"to:helper:{corr}",
        f"task-corr:{corr}",
        f"host:{host}",
    ]
    if role:
        base_tags.append(f"helper-role:{role}")
    # `task-complete` is the existing close-window / closure-followup
    # signal that kitty.js and claude_code_watcher already react to.
    # Emit it on `complete` so the rest of the lifecycle keeps working
    # without changes elsewhere.
    if body.kind == "complete":
        base_tags.append("task-complete")
    base_tags.extend(_filter_extra_tags(body.extra_tags))

    try:
        delta = await delta_client.write(
            content=(body.content or "").strip(),
            tags=base_tags,
            source="helper-reply",
        )
    except Exception as e:
        raise HTTPException(502, f"lake write failed: {type(e).__name__}: {e}") from e

    return {"delta_id": (delta or {}).get("id") or ""}
