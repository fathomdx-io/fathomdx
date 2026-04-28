"""Memory operations as function-calling tools."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta

from . import delta_client
from . import messages as messages_mod
from . import routines as routines_mod
from ._engagement import build_engagement_payload
from ._tags import tag_suffix
from ._tool_explain import _execute_explain
from ._tool_schema import TOOLS

__all__ = ["TOOLS", "execute", "heartbeat_age_seconds", "heartbeat_is_fresh"]


# write_chat_event used to live in api/chat_listener.py and paint a
# `chat-event` delta into the lake so the dashboard's chat detail view
# could show "tool used", "routine proposed", "silence", etc. inline.
# Chat sessions are retired (Grand Loop cutover), so the events have no
# consumer and this is a no-op. The single live caller is the routine-
# proposal flow below; once /n is wired through the puddle, that flow
# should write a puddle delta the witness can render instead.
# TODO(grand-loop): replace with a puddle write of `route:routine-proposal`
# when the /n endpoint moves off chat sessions.
async def write_chat_event(*_args, **_kwargs) -> None:
    return None

# How long a routine-proposal event survives in the lake before the
# delta-store reaps it. Longer than the default chat-event TTL because
# the user may wander off for a while before confirming the form.
ROUTINE_PROPOSAL_TTL_SECONDS = 6 * 3600

# A heartbeat is considered "fresh" (agent connected) if it was emitted
# within this window. Heartbeats fire every ~60s, so 90s tolerates a
# single missed beat without flipping the UI to disconnected. Heartbeat
# deltas themselves live for 24h so the dashboard can still show a
# disconnected card after the connected window elapses.
HEARTBEAT_STALE_SECONDS = 90


def heartbeat_age_seconds(delta: dict) -> float | None:
    """Seconds since the given heartbeat delta was emitted, or None if unparseable."""
    ts = delta.get("timestamp", "")
    if not ts:
        return None
    try:
        hb = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(UTC) - hb).total_seconds()


def heartbeat_is_fresh(delta: dict) -> bool:
    age = heartbeat_age_seconds(delta)
    return age is not None and age < HEARTBEAT_STALE_SECONDS


# ── Tool execution ──────────────────────────────


async def _session_contact_slug(session_id: str) -> str | None:
    """Return the contact slug associated with a chat session, or None.

    Reads the contact: tag off any participant:user delta in the
    session — every chat turn is stamped with one. Used by send_message
    to default `to` to the session's human when the LLM omits it.
    """
    try:
        results = await delta_client.query(
            tags_include=[f"chat:{session_id}", "participant:user"],
            limit=1,
        )
    except Exception:
        return None
    for d in results:
        slug = tag_suffix(d.get("tags") or [], "contact:")
        if slug:
            return slug
    return None


def _slim_search_results(raw: dict) -> dict:
    """Strip embeddings, cap content length for context window."""
    hits = raw.get("results", [])
    slim = []
    for h in hits:
        d = h.get("delta", {})
        entry = {
            "id": d.get("id"),
            "content": d.get("content", "")[:1500],
            "tags": d.get("tags", []),
            "source": d.get("source"),
            "timestamp": d.get("timestamp"),
            "distance": round(h.get("distance", 0), 3),
        }
        if d.get("media_hash"):
            entry["media_hash"] = d["media_hash"]
        slim.append(entry)
    return {"count": len(slim), "results": slim}


def _slim_query_results(raw: list) -> dict:
    """Same slimming for query results."""
    slim = []
    for d in raw:
        entry = {
            "id": d.get("id"),
            "content": d.get("content", "")[:1500],
            "tags": d.get("tags", []),
            "source": d.get("source"),
            "timestamp": d.get("timestamp"),
        }
        if d.get("media_hash"):
            entry["media_hash"] = d["media_hash"]
        slim.append(entry)
    return {"count": len(slim), "results": slim}


async def execute(name: str, arguments: dict, session_id: str | None = None) -> str:
    """Execute a tool call, return result as JSON string.

    `session_id` is injected from the API — the caller knows the current
    chat session and passes it in so tools that need it (route_to_agent)
    don't have to ask the model to pass it back as a parameter. The model
    wouldn't know anyway, and asking the user is always wrong.
    """
    try:
        if name == "remember":
            raw = await delta_client.search(
                query=arguments["query"],
                limit=arguments.get("limit", 20),
                radii=arguments.get("radii"),
                tags_include=arguments.get("tags_include"),
            )
            return json.dumps(_slim_search_results(raw))

        if name == "write":
            # image_b64 routes through upload_media so the model can attach
            # a picture to a write in one call. image_path is registry-only
            # (staging-volume path gated by the HTTP sandbox); chat ignores
            # it — the LLM should reach for image_b64 when it has pixels.
            image_b64 = arguments.get("image_b64")
            if image_b64:
                file_bytes = base64.b64decode(image_b64)
                result = await delta_client.upload_media(
                    file_bytes=file_bytes,
                    filename="upload.bin",
                    content=arguments["content"],
                    tags=arguments.get("tags", []),
                    source=arguments.get("source", "consumer-api"),
                )
            else:
                result = await delta_client.write(
                    content=arguments["content"],
                    tags=arguments.get("tags", []),
                    source=arguments.get("source", "consumer-api"),
                )
            return json.dumps(result)

        if name == "recall":
            # LAKE_TOOLS exposes the model-facing param as `tags`;
            # delta_client.query takes it as `tags_include`. The registry's
            # request_map handles that translation for HTTP callers (MCP);
            # in-process callers (chat) translate here.
            raw = await delta_client.query(
                limit=arguments.get("limit", 50),
                tags_include=arguments.get("tags"),
                source=arguments.get("source"),
                time_start=arguments.get("time_start"),
            )
            return json.dumps(_slim_query_results(raw))

        if name == "deep_recall":
            result = await delta_client.plan(arguments["steps"])
            return json.dumps(result)

        if name == "mind_tags":
            result = await delta_client.tags()
            return json.dumps(result)

        if name == "mind_stats":
            result = await delta_client.stats()
            return json.dumps(result)

        if name == "see_image":
            return await _fetch_image_as_tool_result(arguments.get("media_hash", ""))

        if name == "routines":
            return await _execute_routines(arguments, session_id=session_id)

        if name == "propose_contact":
            from . import contacts as contacts_mod

            written = await contacts_mod.propose(
                candidate_slug=(arguments.get("candidate_slug") or "").strip() or None,
                display_name=arguments["display_name"],
                rationale=arguments["rationale"],
                source_context=arguments.get("source_context") or {},
                # In the chat tool path, Fathom writes the proposal as
                # Fathom (no contact: tag) — the admin just needs to
                # know it's a proposal, not who proposed it.
                proposer_slug=None,
            )
            return json.dumps(
                {
                    "ok": True,
                    "proposal_id": written.get("id"),
                    "candidate_slug": written.get("candidate_slug"),
                    "display_name": written.get("display_name"),
                    "note": (
                        "Proposal written. Admin will see it in Settings → "
                        "Contacts and can Accept (creates the contact) or "
                        "Reject (keeps the proposal as sediment)."
                    ),
                }
            )

        if name == "engage":
            kind = (arguments.get("kind") or "").lower()
            if kind not in ("refutes", "affirms", "reply-to"):
                return json.dumps({"error": f"unknown engagement kind: {kind!r}"})
            target_id = (arguments.get("target_id") or "").strip()
            if not target_id:
                return json.dumps({"error": "target_id required"})
            reason = (arguments.get("reason") or "").strip()
            content, media_hash = await build_engagement_payload(target_id, reason)
            written = await delta_client.write(
                content=content,
                tags=[f"{kind}:{target_id}"],
                source="fathom-engagement",
                media_hash=media_hash,
            )
            return json.dumps(
                {
                    "ok": True,
                    "id": written.get("id"),
                    "kind": kind,
                    "target_id": target_id,
                }
            )

        if name == "send_message":
            recipient = (arguments.get("to") or "").strip()
            if not recipient and session_id:
                # Default-to-requestor: read the session's contact: tag from
                # any user delta in the thread. This is the LLM-in-chat
                # path; the human in the session is the natural recipient
                # for "alert me" / "remind me" instructions.
                recipient = await _session_contact_slug(session_id) or ""
            if not recipient:
                return json.dumps(
                    {
                        "error": (
                            "no recipient — pass `to` with a contact slug, or "
                            "call this tool inside a chat session so the "
                            "requestor can be inferred"
                        ),
                    }
                )
            body = arguments.get("body") or ""
            try:
                result = await messages_mod.send_message(
                    recipient_slug=recipient,
                    body=body,
                    writer_slug="fathom",
                    session_slug=arguments.get("session") or None,
                )
            except ValueError as e:
                return json.dumps({"error": str(e)})
            return json.dumps(result)

        if name == "rename_session":
            if not session_id:
                return json.dumps(
                    {
                        "error": "rename_session can only be called inside a chat session",
                    }
                )
            new_name = (arguments.get("name") or "").strip()
            if not new_name:
                return json.dumps({"error": "name is required"})
            await delta_client.write(
                content=new_name,
                tags=["fathom-chat", f"chat:{session_id}", "chat-name"],
                source="consumer-api",
            )
            return json.dumps({"ok": True, "session_id": session_id, "name": new_name})

        if name == "explain":
            return await _execute_explain(arguments)

        return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# Sentinel prefix for multimodal image results — the tool loop
# in server.py detects this and converts to a content block.
IMAGE_RESULT_PREFIX = "__IMAGE__:"


async def _fetch_image_as_tool_result(media_hash: str) -> str:
    """Fetch image from delta store, return as a sentinel string.

    The tool loop in server.py detects the IMAGE_RESULT_PREFIX and
    converts this into a multimodal content block (image_url with
    base64 data URI) so the LLM actually sees the pixels.
    """
    if not media_hash:
        return json.dumps({"error": "No media_hash provided"})
    try:
        c = await delta_client._get()
        r = await c.get(f"/media/{media_hash}", timeout=15)
        r.raise_for_status()
        img_bytes = r.content
        b64 = base64.b64encode(img_bytes).decode("ascii")
        # Return sentinel so the tool loop can build a multimodal message
        return f"{IMAGE_RESULT_PREFIX}data:image/webp;base64,{b64}"
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch image: {e}"})


# ── Routines tool — action-dispatched CRUD ──────────────────────────────


ROUTINE_SPEC_HELP_STATIC = """ROUTINE SPEC — quick reference

A routine is a prompt + a cron schedule + a workspace, pinned to a specific
machine. When its cron fires, the named machine's local `fathom-agent` picks
it up, spawns a kitty window with claude in the named workspace, and injects
the prompt. Claude runs, writes a summary delta, and the dashboard pairs it
back to the fire.

Fields (used with action=create or action=update):
  id              (required, immutable)   stable identifier, e.g. "gold-check"
  name            (required)              human label, e.g. "Gold Price Pulse"
  host            (which machine)         hostname of the connected agent that runs this;
                                          empty = fleet-wide (every connected agent fires)
  schedule        (cron, 5 fields)        "0 * * * *" hourly · "*/5 * * * *" every 5 min
  prompt          (the work)              what claude should do when fired
  permission_mode auto | normal           auto = classifier guardrails · normal = user approves each tool
  workspace                                directory under ~/Dropbox/Work/ (e.g. "fathom"). Leave
                                           blank — the target agent advertises a default_workspace
                                           in its heartbeat (set during `fathom-agent init`) that
                                           fills in automatically. Only ask the user when no
                                           default exists and you can't infer one from context.
  enabled         bool (default true)
  single_fire     bool (default false, not yet honored by scheduler)

Actions (via this single `routines` tool):
  help             ← you just called this
  list             all current routines + last-run summaries
  get id=X         single routine spec
  create ...       new routine (id + name required, schedule strongly recommended)
  update id=X ...  modify fields; omitted fields inherit from existing
  delete id=X      soft-delete (writes a tombstone delta; history stays in the lake)
  fire id=X        trigger the routine to run now
  preview_schedule schedule="..." count=N    next N fire times for a cron

When mutation actions are called without a connected local agent, the tool
returns installation instructions instead. Tell the user to visit the main
dashboard and pick a platform under "Local Agent".
"""


async def _routine_help_text() -> str:
    """Static help + a live dump of currently-connected machines.

    The live section matters because `host` is a required-ish field and the
    LLM has to pick from the connected set. Including it in help means the
    LLM rarely has to make a second round trip just to see the machine list.
    """
    alive, agents = await _agent_alive()
    if not alive:
        live = "\nCONNECTED MACHINES — none right now. Mutation actions will fail until an agent connects."
    else:
        names = ", ".join(a["host"] for a in agents)
        live = (
            f"\nCONNECTED MACHINES — {names}\n"
            "When creating a routine: if only one machine is connected, use it as the "
            "default host without asking. If multiple are connected, ask the user which "
            "machine the routine should run on. The user may also name a machine that "
            "isn't currently connected — accept it; the routine sits until that machine "
            "comes back online."
        )
    return ROUTINE_SPEC_HELP_STATIC + live


# Backwards-compat alias so anything that references the old name still works.
ROUTINE_SPEC_HELP = ROUTINE_SPEC_HELP_STATIC


async def _agent_alive() -> tuple[bool, list[dict]]:
    """Return (alive, agent_summaries) for hosts with a fresh heartbeat.

    "Fresh" means the most recent heartbeat delta for that host was emitted
    within HEARTBEAT_STALE_SECONDS. Stale heartbeats are ignored — callers
    use this to decide whether mutation actions (routine dispatch, body
    routing) can reach a live agent, which stale heartbeats can't.
    """
    # Bound the query to the freshness window on the server side. Heartbeat
    # deltas linger for 24h so the dashboard can show disconnected cards —
    # without time_start we'd pull every heartbeat from every host.
    time_start = (datetime.now(UTC) - timedelta(seconds=HEARTBEAT_STALE_SECONDS)).isoformat()
    try:
        deltas = await delta_client.query(
            limit=50,
            tags_include=["agent-heartbeat"],
            time_start=time_start,
        )
    except Exception:
        return False, []
    agents = []
    seen_hosts = set()
    for d in deltas:
        tags = d.get("tags") or []
        host = tag_suffix(tags, "host:") or "unknown"
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        if not heartbeat_is_fresh(d):
            continue
        try:
            payload = json.loads(d.get("content", "{}"))
        except Exception:
            payload = {}
        agents.append({"host": host, "plugins": payload.get("plugins") or {}})
    return len(agents) > 0, agents


def _no_agent_response(action: str) -> str:
    return json.dumps(
        {
            "action": action,
            "error": "no_agent_connected",
            "message": (
                "No local fathom-agent is currently registered. Mutation actions "
                "(create/update/delete/fire) require a local agent to execute the "
                "resulting routine-fire deltas. Tell the user to visit the main "
                'Fathom dashboard and install a local agent from the "Local Agent" '
                "section (Linux / Mac / Windows), then try again."
            ),
            "dashboard_hint": "the main page of the Fathom app has the Local Agent install cards",
        }
    )


async def _known_workspaces() -> list[str]:
    """Scan existing spec deltas for the set of workspaces currently in use.

    Used by the clarification loop so the LLM can offer the user a menu
    instead of inventing a workspace name.
    """
    try:
        specs = await delta_client.query(limit=500, tags_include=["spec", "routine"])
    except Exception:
        return []
    seen: set[str] = set()
    for d in specs:
        tags = d.get("tags") or []
        ws = tag_suffix(tags, "workspace:")
        if ws:
            seen.add(ws)
    return sorted(seen)


async def _host_default_workspace(host: str) -> str:
    """Look up an agent's configured default_workspace from heartbeat.

    The kitty plugin surfaces this in its heartbeat summary when the user
    set one during `fathom-agent init`. Returns empty string when unknown
    or when the host hasn't configured one.
    """
    if not host:
        return ""
    _alive, agents = await _agent_alive()
    for a in agents:
        if a.get("host") != host:
            continue
        kitty = (a.get("plugins") or {}).get("kitty") or {}
        return (kitty.get("default_workspace") or "").strip()
    return ""


def _slugify(name: str, max_len: int = 48) -> str:
    """Lowercase, strip non-alphanumerics → hyphens, collapse runs.

    Used as the routine-id fallback when the LLM names a routine but
    forgets the slug. Keeps the result short enough to type and stable
    enough to reference. Empty input returns an empty string — caller
    decides whether that's a gap.
    """
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:max_len].rstrip("-")


async def _gather_create_gaps(args: dict) -> dict:
    """Return {missing: [...], hint: '...'} describing what's incomplete.

    Missing list is empty when everything needed is present. Hint is always
    a single human-readable sentence the LLM can use to ask the user.
    """
    missing: list[str] = []
    hints: list[str] = []

    if not (args.get("id") or "").strip():
        missing.append("id")
        hints.append(
            "No routine id. Ask the user for a stable short identifier (e.g. 'gold-check', 'daily-heartbeat')."
        )

    if not (args.get("name") or "").strip():
        missing.append("name")
        hints.append("No name. Ask the user for a human-readable label.")

    if not (args.get("schedule") or "").strip():
        missing.append("schedule")
        hints.append(
            "No schedule. Ask the user when the routine should fire "
            "(e.g. 'every hour' → '0 * * * *', 'every 5 minutes' → '*/5 * * * *', "
            "'daily at 9am' → '0 9 * * *'). Offer to preview with action=preview_schedule."
        )

    if not (args.get("prompt") or "").strip():
        missing.append("prompt")
        hints.append("No prompt. Ask the user what claude should do when this routine fires.")

    if not (args.get("workspace") or "").strip():
        # Before declaring a workspace gap, see if the target host has one
        # configured via `fathom-agent init`. That default travels with the
        # agent's heartbeat, so the LLM shouldn't have to ask if it's set.
        host_default = await _host_default_workspace((args.get("host") or "").strip())
        if host_default:
            args["workspace"] = host_default
        else:
            missing.append("workspace")
            known = await _known_workspaces()
            if known:
                hints.append(
                    f"No workspace. Known workspaces from existing routines: {', '.join(known)}. "
                    "Ask the user which directory under ~/Dropbox/Work/ the routine should run in."
                )
            else:
                hints.append(
                    "No workspace. Ask the user which directory under ~/Dropbox/Work/ "
                    "the routine should run in (e.g. 'fathom', 'applications')."
                )

    # `host` is only a "gap" when there are 2+ live machines — the user has
    # to pick. With exactly one, it's silently defaulted further down. With
    # zero live machines, other gates have already rejected the call. An
    # explicit host the user named (even if offline) is accepted as-is.
    if "host" not in args:
        _alive, agents = await _agent_alive()
        if len(agents) > 1:
            missing.append("host")
            names = ", ".join(a["host"] for a in agents)
            hints.append(
                f"No machine. Live machines right now: {names}. "
                "Ask the user which machine should run this routine. "
                "They can also name a machine that isn't currently connected; "
                "the routine will sit until that machine comes back."
            )

    return {"missing": missing, "hint": " ".join(hints) if hints else ""}


async def _execute_routines(args: dict, session_id: str | None = None) -> str:
    action = (args.get("action") or "help").strip().lower()

    # Informational actions — always work, even without an agent.
    if action == "help":
        alive, agents = await _agent_alive()
        return json.dumps(
            {
                "action": "help",
                "agent_connected": alive,
                "agents": agents,
                "spec": await _routine_help_text(),
            }
        )

    if action == "list":
        alive, agents = await _agent_alive()
        routines = await routines_mod.list_routines()
        # Slim each to keep context lean
        slim = [
            {
                "id": r["id"],
                "name": r["name"],
                "enabled": r["enabled"],
                "schedule": r.get("schedule"),
                "workspace": r.get("workspace"),
                "permission_mode": r.get("permission_mode"),
                "last_fire_at": r.get("last_fire_at"),
                "last_summary": (r.get("last_summary") or {}).get("content"),
            }
            for r in routines
        ]
        return json.dumps(
            {
                "action": "list",
                "agent_connected": alive,
                "count": len(slim),
                "routines": slim,
            }
        )

    if action == "get":
        rid = (args.get("id") or "").strip()
        if not rid:
            return json.dumps({"action": "get", "error": "id is required"})
        spec = await routines_mod.get_latest_spec(rid)
        if not spec or spec["meta"].get("deleted"):
            return json.dumps({"action": "get", "error": f"routine {rid} not found"})
        return json.dumps(
            {
                "action": "get",
                "routine": {
                    "id": spec["meta"].get("id"),
                    "meta": spec["meta"],
                    "body": spec["body"],
                    "workspace": spec["workspace"],
                },
            }
        )

    if action == "preview_schedule":
        sched = (args.get("schedule") or "").strip()
        if not sched:
            return json.dumps({"action": "preview_schedule", "error": "schedule is required"})
        fires = routines_mod.preview_fires(sched, count=int(args.get("count") or 5))
        return json.dumps(
            {
                "action": "preview_schedule",
                "schedule": sched,
                "fires": fires,
                "error": None if fires else "invalid cron",
            }
        )

    # Mutation actions — require an agent.
    if action in ("create", "update", "delete", "fire"):
        alive, _ = await _agent_alive()
        if not alive:
            return _no_agent_response(action)

    if action == "create":
        # Single-machine default: if exactly one agent is connected and the
        # caller didn't set `host`, silently pin to that machine. With two or
        # more live agents, the user picks from the form's host dropdown.
        # With zero live agents, the earlier _agent_alive gate already
        # rejected the call.
        if "host" not in args:
            _alive, agents = await _agent_alive()
            if len(agents) == 1:
                args = {**args, "host": agents[0]["host"]}

        # Slug fallback: the LLM almost always supplies a name but often
        # forgets the id. Derive the id from the name so the proposal form
        # arrives prefilled — the user can still edit it before saving.
        if not (args.get("id") or "").strip() and (args.get("name") or "").strip():
            args = {**args, "id": _slugify(args["name"])}

        confirm = bool(args.get("confirm"))

        # Proposal flow: inside a chat, paint the routine form in the stream
        # and let the human review/edit/save. Skipped when confirm=true
        # (user said "just make it") or outside chat (no session_id).
        if session_id and not confirm:
            proposal = {k: args[k] for k in args if k not in ("action", "confirm")}
            try:
                await write_chat_event(
                    session_id,
                    "routine-proposal",
                    {"proposal": proposal},
                    ttl_seconds=ROUTINE_PROPOSAL_TTL_SECONDS,
                )
            except Exception as e:
                return json.dumps(
                    {
                        "action": "create",
                        "status": "proposal_failed",
                        "message": f"couldn't paint review form: {e}",
                    }
                )
            return json.dumps(
                {
                    "action": "create",
                    "status": "needs_confirmation",
                    "hint": (
                        "The routine form is now in the chat for the user to "
                        "review and save. Reply briefly — do NOT restate the "
                        "fields in prose."
                    ),
                    "proposal": proposal,
                }
            )

        # Clarification loop: inspect args, return `needs_info` when gaps exist
        # so the LLM can go back to the user and ask before committing.
        gaps = await _gather_create_gaps(args)
        if gaps["missing"]:
            return json.dumps(
                {
                    "action": "create",
                    "status": "needs_info",
                    "missing": gaps["missing"],
                    "hint": gaps["hint"],
                    "partial": {k: args[k] for k in args if k not in ("action", "confirm")},
                }
            )
        try:
            body = {k: args[k] for k in args if k not in ("action", "confirm")}
            result = await routines_mod.create(body)
            return json.dumps({"action": "create", **result})
        except FileExistsError:
            # Upgrade dup-collision from hard error to conversational clarification
            rid = args.get("id", "")
            existing = await routines_mod.get_latest_spec(rid)
            existing_name = (existing or {}).get("meta", {}).get("name", "") if existing else ""
            return json.dumps(
                {
                    "action": "create",
                    "status": "needs_info",
                    "missing": ["id_or_intent"],
                    "hint": (
                        f"A routine with id '{rid}' already exists"
                        + (f" (name: '{existing_name}')" if existing_name else "")
                        + ". Ask the user: do they want to update the existing one "
                        + "(use action=update), replace it (delete first, then create), "
                        + "or pick a different id?"
                    ),
                    "partial": {k: args[k] for k in args if k not in ("action", "confirm")},
                }
            )
        except ValueError as e:
            return json.dumps({"action": "create", "error": "invalid", "message": str(e)})

    if action == "update":
        rid = (args.get("id") or "").strip()
        if not rid:
            return json.dumps({"action": "update", "error": "id is required"})
        try:
            body = {k: args[k] for k in args if k not in ("action", "id")}
            result = await routines_mod.update(rid, body)
            return json.dumps({"action": "update", **result})
        except FileNotFoundError as e:
            return json.dumps({"action": "update", "error": "not_found", "message": str(e)})
        except ValueError as e:
            return json.dumps({"action": "update", "error": "invalid", "message": str(e)})

    if action == "delete":
        rid = (args.get("id") or "").strip()
        if not rid:
            return json.dumps({"action": "delete", "error": "id is required"})
        try:
            result = await routines_mod.soft_delete(rid)
            return json.dumps({"action": "delete", **result})
        except FileNotFoundError as e:
            return json.dumps({"action": "delete", "error": "not_found", "message": str(e)})

    if action == "fire":
        rid = (args.get("id") or "").strip()
        if not rid:
            return json.dumps({"action": "fire", "error": "id is required"})
        try:
            result = await routines_mod.fire(rid, prompt_override=args.get("prompt"))
            return json.dumps({"action": "fire", **result})
        except FileNotFoundError as e:
            return json.dumps({"action": "fire", "error": "not_found", "message": str(e)})

    return json.dumps({"action": action, "error": f"unknown action: {action}"})
