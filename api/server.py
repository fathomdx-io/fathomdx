"""Fathom Consumer API — OpenAI-compat chat completions with delta lake tools."""
from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    auth,
    auto_regen,
    crystal,
    crystal_anchor,
    db,
    delta_client,
    drift,
    mood,
)
from . import contacts as contacts_mod
from .prompt import (
    CRYSTAL_DIRECTIVE,
    CRYSTAL_REGEN_SYSTEM,
    ORIENT_PROMPT,
    build_system_prompt,
)
from .providers import llm
from .search import search as nl_search
from .settings import settings
from .tools import IMAGE_RESULT_PREFIX, TOOLS, execute

log = logging.getLogger(__name__)

# ── Request / response models ───────────────────


class Message(BaseModel):
    role: str
    content: str | list | None = None
    tool_calls: list | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[Message]
    session_id: str | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    image_uploaded: bool = False  # Skip user message persist — image upload already wrote it


# ── App ─────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Resolve the first-admin slug up front so the legacy-token migration
    # and any contact-tag backfill have a target. On a fresh install with
    # no admin yet, this returns None and both operations become no-ops
    # until bootstrap runs. Retries because delta-store may still be
    # booting when api starts.
    import asyncio as _asyncio

    from . import chat_listener
    resolved_admin: str | None = None
    for attempt in range(6):
        try:
            resolved_admin = await contacts_mod.first_admin_slug()
            break
        except Exception:
            if attempt == 5:
                log.exception("lifespan: first_admin_slug failed after retries")
            else:
                await _asyncio.sleep(2 ** attempt)
    if resolved_admin:
        migrated = auth.migrate_legacy_tokens(default_slug=resolved_admin)
        if migrated:
            log.info("Bound %d legacy tokens to contact '%s'", migrated, resolved_admin)

    # One-shot backfill of contact:<admin> onto per-user deltas that
    # predate the contact registry. Idempotent — skips deltas that
    # already carry any contact: tag, so re-runs are no-ops. Only fires
    # once an admin exists; on pre-bootstrap installs the lake is empty
    # and there's nothing to backfill anyway.
    async def _backfill_once(admin_slug: str):
        for attempt in range(6):  # ~30s total with backoff
            try:
                result = await delta_client.backfill_contact_tag(
                    contact_slug=admin_slug,
                    filter_tags=[
                        "feed-engagement",
                        "feed-story",
                        "feed-card",
                        "crystal:feed-orient",
                    ],
                )
                if result.get("updated"):
                    log.info(
                        "Backfilled contact:%s on %d legacy feed deltas",
                        admin_slug,
                        result.get("updated"),
                    )
                return
            except Exception:
                if attempt == 5:
                    log.exception("contact backfill failed after retries (non-fatal)")
                    return
                await _asyncio.sleep(2 ** attempt)

    if resolved_admin:
        _asyncio.create_task(_backfill_once(resolved_admin))

    auto_regen.start()
    chat_listener.listener.start()
    try:
        yield
    finally:
        await chat_listener.listener.stop()
        await auto_regen.stop()
        await delta_client.close()


app = FastAPI(title="Fathom Consumer API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(auth.TokenAuthMiddleware)

# ── Routers (one file per resource cluster under api/routes/) ───
from .routes import agents as _agents_routes  # noqa: E402
from .routes import auth as _auth_routes  # noqa: E402
from .routes import contacts as _contacts_routes  # noqa: E402
from .routes import feed as _feed_routes  # noqa: E402
from .routes import lake as _lake_routes  # noqa: E402
from .routes import media as _media_routes  # noqa: E402
from .routes import routines as _routines_routes  # noqa: E402
from .routes import sessions as _sessions_routes  # noqa: E402
from .routes import sources as _sources_routes  # noqa: E402
from .routes import vitals as _vitals_routes  # noqa: E402

app.include_router(_agents_routes.router)
app.include_router(_auth_routes.router)
app.include_router(_contacts_routes.router)
app.include_router(_feed_routes.router)
app.include_router(_lake_routes.router)
app.include_router(_media_routes.router)
app.include_router(_routines_routes.router)
app.include_router(_sessions_routes.router)
app.include_router(_sources_routes.router)
app.include_router(_vitals_routes.router)


# ── Helpers ─────────────────────────────────────

MAX_TOOL_ROUNDS = 10


async def _resolve_tools(
    messages: list[dict],
    model: str,
    tools: list[dict] | None = None,
    on_tool_event: callable | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    session_id: str | None = None,
    **kwargs,
) -> list[dict]:
    """Run the tool-calling loop until the LLM stops calling tools.

    Each round: call LLM → if tool_calls, execute them, append results,
    repeat. When the LLM returns text (no tool_calls), stop and return
    the updated messages list with the final text as the last entry.
    """
    tools = tools or TOOLS
    for _ in range(max_rounds):
        resp = await llm.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            **kwargs,
        )
        choice = resp.choices[0]
        msg = choice.message

        if not msg.tool_calls:
            # LLM produced text, not tool calls — we're done resolving.
            # Append the text as an assistant message so the final streaming
            # call has full context if we need to re-call. But for the
            # non-streaming path, this IS the final answer.
            messages.append({"role": "assistant", "content": msg.content or ""})
            return messages

        # Append the assistant's tool_calls message
        messages.append(msg.model_dump(exclude_none=True))

        # Execute each tool call
        for tc in msg.tool_calls:
            fn = tc.function
            try:
                args = json.loads(fn.arguments) if fn.arguments else {}
            except json.JSONDecodeError:
                args = {}

            if on_tool_event:
                on_tool_event("call", fn.name, args)

            result_str = await execute(fn.name, args, session_id=session_id)

            # Image results become multimodal content blocks
            is_image = result_str.startswith(IMAGE_RESULT_PREFIX)

            if on_tool_event:
                if is_image:
                    on_tool_event("result", fn.name, {"media_hash": args.get("media_hash")})
                else:
                    try:
                        result_data = json.loads(result_str)
                        on_tool_event("result", fn.name, result_data)
                    except Exception:
                        on_tool_event("result", fn.name, {})

            if is_image:
                data_uri = result_str[len(IMAGE_RESULT_PREFIX):]
                media_hash = args.get("media_hash", "?")
                # Gemini doesn't support image_url in tool results.
                # Return text as tool result, then inject the image as a
                # user message so it lands in a supported position.
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": f"Image loaded (media_hash: {media_hash}). See the image in the next message.",
                })
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"[System: here is the image from delta lake, media_hash={media_hash}]"},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                })
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

    # Exceeded max rounds — force a text-only final call so we always get a response
    resp = await llm.chat.completions.create(model=model, messages=messages, **kwargs)
    choice = resp.choices[0]
    messages.append({"role": "assistant", "content": choice.message.content or ""})
    return messages


# ── Core loop ──────────────────────────────────


async def fathom_think(
    user_message: str,
    directive: str = "",
    history: list[dict] | None = None,
    tools: list[dict] | None = None,
    extra_tools: list[dict] | None = None,
    recall: bool = True,
    session_slug: str | None = None,
    model: str | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    on_tool_event: callable | None = None,
    system_override: str | None = None,
    **llm_kwargs,
) -> list[dict]:
    """Unified Fathom reasoning loop.

    Every path through the system — chat, feed, crystal — goes through here.
    This guarantees the same voice (SYSTEM_PREAMBLE), the same relationship
    to memories, and the same tool access regardless of task.

    Args:
        tools: Replace the default tool surface entirely. None = TOOLS.
        extra_tools: Append additional tools to whatever base set is active.
        system_override: Replace the built system prompt entirely. Used by
            crystal regen so the synthesis isn't polluted by SYSTEM_PREAMBLE
            rules, prior-crystal injection, or mood layer — the regen should
            look at itself from outside, not BE itself reading itself.

    Returns the full messages list with the final assistant response as the
    last entry.
    """
    model = model or settings.resolved_model

    # Resolve tool surface: replace, extend, or default
    resolved_tools = tools if tools is not None else TOOLS
    if extra_tools:
        resolved_tools = resolved_tools + extra_tools

    # 1. Build system prompt — default path is the full Fathom voice;
    # callers that need a clean frame (crystal regen) pass system_override.
    if system_override is not None:
        system = system_override
    else:
        crystal_text = await crystal.latest_text()
        current_mood = await mood.maybe_synthesize_on_wake(session_slug=session_slug)
        session_title: str | None = None
        if session_slug:
            sess = await db.get_session(session_slug)
            if sess:
                session_title = sess.get("title")
        from .tools import _agent_alive
        agent_connected, agents_info = await _agent_alive()
        # Known contacts hydrate the "who is Fathom talking to + about"
        # context. Merged with session-addressee so the model can propose
        # new contacts instead of hallucinating slugs. list_all returns
        # a small set (typically <20); the query is 60s-cached elsewhere.
        try:
            known_contacts = await contacts_mod.list_all()
        except Exception:
            known_contacts = []
        current_contact_slug: str | None = None
        if session_slug:
            # The addressee of this chat session — whoever's contact: tag
            # appears on the user deltas in this thread. Read off the
            # most recent user delta via the session history.
            try:
                latest = await delta_client.query(
                    tags_include=[f"chat:{session_slug}", "participant:user"],
                    limit=1,
                )
                if latest:
                    for t in latest[0].get("tags") or []:
                        if isinstance(t, str) and t.startswith("contact:"):
                            current_contact_slug = t.split(":", 1)[1]
                            break
            except Exception:
                pass
        # Resolve the addressee's timezone so "Current time" in the prompt
        # matches the clock rendered in the UI opener stamp. known_contacts
        # is already fetched above, so no extra round-trip.
        user_timezone: str | None = None
        if current_contact_slug and known_contacts:
            for c in known_contacts:
                if c.get("slug") == current_contact_slug:
                    tz_raw = c.get("timezone")
                    if isinstance(tz_raw, str) and tz_raw.strip():
                        user_timezone = tz_raw.strip()
                    break
        system = build_system_prompt(
            crystal_text=crystal_text,
            session_slug=session_slug,
            session_title=session_title,
            mood_carrier_wave=(current_mood or {}).get("carrier_wave"),
            mood_threads=(current_mood or {}).get("threads"),
            agent_connected=agent_connected,
            agent_hosts=[a.get("host", "") for a in agents_info if a.get("host")],
            known_contacts=known_contacts,
            current_contact_slug=current_contact_slug,
            user_timezone=user_timezone,
        )

    # Append task-specific directive
    if directive:
        system += f"\n\n--- Task Directive ---\n{directive}\n--- End Directive ---"

    # 2. Assemble message list
    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 3. Recall — proactively surface memories before the main call
    if recall:
        conv_context = ""
        if history:
            recent = [m for m in history if m.get("role") in ("user", "assistant")][-6:]
            conv_context = "\n".join(
                f'{m["role"]}: {(m.get("content") or "")[:200]}' for m in recent
            )

        recalled = await nl_search(
            text=user_message,
            depth="deep",
            session_slug=session_slug,
            conv_context=conv_context,
        )

        if recalled["as_prompt"]:
            inject_msg = {
                "role": "system",
                "content": (
                    f"You remember these things ({recalled['total_count']} surfaced):\n\n"
                    f"{recalled['as_prompt']}\n\n"
                    "Speak from these naturally — they are your own memories. "
                    "If they don't cover what you need, search deeper."
                ),
            }
            messages.insert(-1, inject_msg)

        if on_tool_event:
            on_tool_event("result", "recall", {"count": recalled["total_count"]})

    # 4. Run the tool loop
    messages = await _resolve_tools(
        messages, model, tools=resolved_tools, on_tool_event=on_tool_event,
        max_rounds=max_rounds, session_id=session_slug, **llm_kwargs,
    )

    return messages


# ── Endpoints ───────────────────────────────────


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    """Write a user message into a chat session and return immediately.

    Fathom's response comes via the chat listener (api/chat_listener.py),
    which polls the lake for new deltas and fires inference per session.
    This endpoint is no longer the inference trigger — it only persists
    the user's delta. The UI picks up Fathom's eventual reply through
    the same poll-the-session cycle that surfaces agent/body messages.

    Why: every participant in a chat (user, Fathom, local bodies, other
    humans in the future) should fire the same way — drop a delta,
    everyone who's listening takes their turn. Previously Fathom only
    responded to HTTP requests; now Fathom responds to deltas. Symmetric.
    """
    # Session-aware: create one if not provided
    session_id = req.session_id
    if session_id:
        session = await db.get_session(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}, 404
    else:
        session_data = await db.create_session()
        session_id = session_data["id"]

    # Persist the user message(s). Image uploads already wrote their own
    # delta via /v1/media so we skip writing a duplicate text delta when
    # image_uploaded is set. The write itself is what triggers the chat
    # listener to take a turn — no inference runs here synchronously.
    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")
    for m in req.messages:
        if m.role == "user" and m.content:
            content = m.content if isinstance(m.content, str) else json.dumps(m.content)
            if not req.image_uploaded:
                await db.add_message(
                    session_id, "user", content, contact_slug=contact_slug
                )

    # Return session_id so the UI can lock onto it for its poll cycle.
    # No streaming response — there's nothing to stream. The chat listener
    # has already (or will shortly) pick up the user delta and write
    # Fathom's reply, which the UI's 3-second poll will surface.
    return {"session_id": session_id}


@app.get("/v1/crystal")
async def get_crystal():
    """Return the current identity crystal (lake-backed)."""
    c = await crystal.latest(force=True)
    if not c:
        raise HTTPException(404, "No crystal generated yet")
    return {
        "text": c["text"],
        "created_at": c["created_at"],
        "id": c["id"],
        "source": c["source"],
    }


CRYSTAL_MIN_CHARS = 800
CRYSTAL_ACCEPT_MIN = 0.05
CRYSTAL_ACCEPT_MAX = 0.5


async def _generate_crystal_candidate(retry_hint: str | None = None) -> str:
    """Run one fathom_think pass for crystal regen. Returns the text."""
    directive = CRYSTAL_DIRECTIVE
    if retry_hint:
        directive += (
            "\n\nYour previous attempt was rejected: "
            f"{retry_hint}. Read more from the lake before writing, and "
            "produce a grounded, multi-section synthesis."
        )
    messages = await fathom_think(
        user_message=ORIENT_PROMPT,
        directive=directive,
        system_override=CRYSTAL_REGEN_SYSTEM,
        recall=False,  # crystal does its own deep searching via tools
        max_rounds=20,
    )
    last = messages[-1] if messages else {}
    return last.get("content", "") or ""


async def _validate_crystal_candidate(text: str) -> str | None:
    """Return a rejection reason or None if the candidate passes gates.

    Gate 1 — length: failure-mode outputs tend to be short paragraphs
    (200-500 chars). Real crystals are multi-section (1500-3000+).

    Gate 2 — semantic band: cosine distance from the lake centroid must
    sit in a reasonable window. Too low (< 0.05) means the text parrots
    the lake without synthesis; too high (> 0.5) means the text doesn't
    describe what's in the lake at all (e.g. the "I can't remember my
    memories" failure mode). Values come from observed good-crystal
    distances clustering around 0.2-0.3.
    """
    if len(text) < CRYSTAL_MIN_CHARS:
        return f"too short ({len(text)} chars, need {CRYSTAL_MIN_CHARS})"
    try:
        d = await delta_client.drift(text)
    except Exception as e:
        return f"drift check failed: {type(e).__name__}: {e}"
    drift_value = float(d.get("drift", 0.0))
    if drift_value < CRYSTAL_ACCEPT_MIN:
        return (
            f"too aligned with lake (drift={drift_value:.3f} < "
            f"{CRYSTAL_ACCEPT_MIN}, looks like a parroted summary)"
        )
    if drift_value > CRYSTAL_ACCEPT_MAX:
        return (
            f"too far from lake (drift={drift_value:.3f} > "
            f"{CRYSTAL_ACCEPT_MAX}, doesn't describe current state)"
        )
    return None


async def _record_rejected_candidate(text: str, reason: str) -> None:
    """Preserve a rejected candidate in the lake for forensics.

    Tagged crystal-reject — invisible to the crystal-regen detection
    rule so it doesn't show up on the identity ECG, but searchable
    later to diagnose what the LLM produced.
    """
    try:
        await delta_client.write(
            content=(text or "(empty)")[:4000] + f"\n\n[rejected: {reason}]",
            tags=["crystal-reject"],
            source="consumer-api",
        )
    except Exception:
        log.exception("failed to record rejected crystal candidate")


@app.post("/v1/crystal/refresh")
async def refresh_crystal():
    """Regenerate the identity crystal via LLM + delta lake tools.

    Gates a candidate through length + drift-band validation before
    persisting. On accept: writes the crystal to the lake, snapshots
    the current lake centroid as the drift anchor (so drift ≡ 0 by
    construction right after regen), and samples drift to seed the
    ECG history. On reject: runs one retry with a corrective hint;
    if that also fails, preserves both candidates as crystal-reject
    deltas for forensics and returns without writing a crystal.
    """
    text = await _generate_crystal_candidate()
    reason = await _validate_crystal_candidate(text)

    if reason:
        log.warning("crystal regen attempt 1 rejected: %s", reason)
        await _record_rejected_candidate(text, reason)
        text = await _generate_crystal_candidate(retry_hint=reason)
        reason = await _validate_crystal_candidate(text)
        if reason:
            log.warning("crystal regen attempt 2 rejected: %s", reason)
            await _record_rejected_candidate(text, reason)
            return {
                "status": "rejected",
                "reason": reason,
                "length": len(text),
            }

    # Accepted — persist crystal first, then snapshot anchor against the
    # post-write lake (one new delta barely perturbs the centroid, so the
    # ECG's first drift tick reads ~0 as intended).
    written = await crystal.write(text, source="consumer-api")
    try:
        c = await delta_client.centroid()
        vec = c.get("centroid")
        if vec:
            await crystal_anchor.save(vec, (written or {}).get("id"))
    except Exception:
        log.exception("failed to snapshot crystal anchor")

    # Seed the drift history with the fresh zero-ish reading.
    try:
        await drift.sample()
    except Exception:
        log.exception("failed to seed post-regen drift sample")

    # Push facets to delta store for activation hooks (best-effort)
    facets = _split_facets(text)
    if facets:
        try:
            c = await delta_client._get()
            await c.post(
                "/hooks/activation/facets",
                json={"facets": facets},
            )
        except Exception:
            pass

    return {"status": "ok", "length": len(text)}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": settings.resolved_model,
            "object": "model",
            "owned_by": settings.provider,
        }],
    }


@app.get("/health")
async def health():
    missing: list[str] = []
    if not settings.api_key:
        missing.append("api_key")
    if not settings.resolved_base_url:
        missing.append("base_url")
    if not settings.resolved_model:
        missing.append("model")
    return {
        "status": "ok",
        "provider": settings.provider,
        "model": settings.resolved_model,
        "llm_configured": not missing,
        "llm_missing": missing,
    }


# ── Crystal facet parsing ───────────────────────

def _split_facets(text: str) -> list[dict]:
    """Split crystal text on ## headers into facets."""
    facets = []
    current_label = None
    current_lines: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current_label and current_lines:
                facets.append({
                    "label": current_label,
                    "text": "\n".join(current_lines).strip(),
                })
            current_label = m.group(1).strip()
            current_lines = []
        elif current_label is not None:
            current_lines.append(line)

    if current_label and current_lines:
        facets.append({
            "label": current_label,
            "text": "\n".join(current_lines).strip(),
        })

    return facets


# ── Static UI (must be last — catches everything unmatched above) ───

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.is_dir():

    @app.get("/")
    async def ui_root():
        return FileResponse(_UI_DIR / "index.html")

    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
