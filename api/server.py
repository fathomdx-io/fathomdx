"""Fathom Consumer API — OpenAI-compat chat completions with delta lake tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
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

# Strips <recalled>…</recalled> blocks that fathom_think occasionally
# leaves in its draft when it forgot to remove them — used by the /n
# (OpenAI-compat) endpoint when polling for the assistant reply. Kept
# at module scope so chat_listener.py (now retired) doesn't have to be
# resurrected for one regex.
_RECALLED_RE = re.compile(r"<recalled>.*?</recalled>", re.DOTALL | re.IGNORECASE)

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
    # Set to a feed-card delta id when the chat session is being opened
    # from a feed click. On the first turn (fresh session), the card is
    # snapshotted into the lake as a `participant:fathom` chat message —
    # Fathom's opening turn — so reopening the session later shows the
    # card the conversation was about, not just the conversation. The
    # snapshot is written BEFORE the user message persists so timestamps
    # order it first in history. Ignored on subsequent turns.
    seed_card_id: str | None = None


# ── App ─────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Resolve the first-admin slug up front so the legacy-token migration
    # and any contact-tag backfill have a target. On a fresh install with
    # no admin yet, this returns None and both operations become no-ops
    # until bootstrap runs. Retries because delta-store may still be
    # booting when api starts.
    import asyncio as _asyncio

    resolved_admin: str | None = None
    for attempt in range(6):
        try:
            resolved_admin = await contacts_mod.first_admin_slug()
            break
        except Exception:
            if attempt == 5:
                log.exception("lifespan: first_admin_slug failed after retries")
            else:
                await _asyncio.sleep(2**attempt)
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
                await _asyncio.sleep(2**attempt)

    if resolved_admin:
        from ._bgtasks import spawn as _spawn_task

        _spawn_task(_backfill_once(resolved_admin), name="lifespan/contact-backfill")

    from .loop import worker as loop_worker

    auto_regen.start()
    loop_worker.start()
    try:
        yield
    finally:
        await loop_worker.stop()
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
from .routes import agent_instructions as _agent_instructions_routes  # noqa: E402
from .routes import agents as _agents_routes  # noqa: E402
from .routes import alerts as _alerts_routes  # noqa: E402
from .routes import auth as _auth_routes  # noqa: E402
from .routes import contacts as _contacts_routes  # noqa: E402
from .routes import lake as _lake_routes  # noqa: E402
from .routes import media as _media_routes  # noqa: E402
from .routes import messages as _messages_routes  # noqa: E402
from .routes import routines as _routines_routes  # noqa: E402
from .routes import sessions as _sessions_routes  # noqa: E402
from .routes import sources as _sources_routes  # noqa: E402
from .routes import stack as _stack_routes  # noqa: E402
from .routes import vitals as _vitals_routes  # noqa: E402
from .loop import routes as _loop_routes  # noqa: E402

app.include_router(_agent_instructions_routes.router)
app.include_router(_agents_routes.router)
app.include_router(_alerts_routes.router)
app.include_router(_auth_routes.router)
app.include_router(_contacts_routes.router)
app.include_router(_lake_routes.router)
app.include_router(_media_routes.router)
app.include_router(_messages_routes.router)
app.include_router(_routines_routes.router)
app.include_router(_sessions_routes.router)
app.include_router(_sources_routes.router)
app.include_router(_stack_routes.router)
app.include_router(_vitals_routes.router)
app.include_router(_loop_routes.router)


# ── Helpers ─────────────────────────────────────

MAX_TOOL_ROUNDS = 10


async def _none_coro() -> None:
    """Placeholder coroutine for `asyncio.gather` slots that a caller
    decided not to run (e.g. session-scoped reads when session_slug is
    None). Returns None immediately; keeps positional unpacking clean."""
    return None


async def _resolve_tools(
    messages: list[dict],
    model: str,
    tools: list[dict] | None = None,
    on_tool_event: callable | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    session_id: str | None = None,
    client=None,
    **kwargs,
) -> list[dict]:
    """Run the tool-calling loop until the LLM stops calling tools.

    Each round: call LLM → if tool_calls, execute them, append results,
    repeat. When the LLM returns text (no tool_calls), stop and return
    the updated messages list with the final text as the last entry.
    """
    tools = tools or TOOLS
    active_client = client or llm
    for _ in range(max_rounds):
        resp = await active_client.chat.completions.create(
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
                data_uri = result_str[len(IMAGE_RESULT_PREFIX) :]
                media_hash = args.get("media_hash", "?")
                # Gemini doesn't support image_url in tool results.
                # Return text as tool result, then inject the image as a
                # user message so it lands in a supported position.
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Image loaded (media_hash: {media_hash}). See the image in the next message.",
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"[System: here is the image from delta lake, media_hash={media_hash}]",
                            },
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    }
                )
            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    }
                )

    # Exceeded max rounds — force a text-only final call so we always get a response
    resp = await active_client.chat.completions.create(model=model, messages=messages, **kwargs)
    choice = resp.choices[0]
    messages.append({"role": "assistant", "content": choice.message.content or ""})
    return messages


# ── Core loop ──────────────────────────────────


# High-frequency tags that flood structured queries without adding signal.
# These are filtered out of the recent-activity digest so the chat-LLM
# sees what was *worked on*, not what the substrate emitted in the
# background. Keep this list narrow — the digest is the answer to "what's
# been going on", and being too aggressive cuts out real activity (e.g.
# don't drop fathom-feed wholesale, just the per-card scroll-past chatter).
_DIGEST_NOISE_TAGS = [
    "agent-heartbeat",
    "chat-event",
    "feed-engagement",
    "mood-tick",
    "sysinfo",
]


async def _recent_activity_digest(hours: int = 12, max_per_source: int = 4) -> str:
    """Compact digest of what's been landing in the lake recently.

    Pre-turn context for fathom_think so the chat-LLM doesn't have to
    formulate a `remember`/`recall` query to answer recap questions like
    "what have we been working on today". Semantic search alone fails
    here because "today" is a temporal axis the embedding doesn't carry.
    """
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    try:
        deltas = await delta_client.query(
            time_start=since,
            tags_exclude=_DIGEST_NOISE_TAGS,
            limit=200,
        )
    except Exception:
        return ""
    if not deltas:
        return ""
    by_source: dict[str, list[dict]] = {}
    for d in deltas:
        src = d.get("source") or "unknown"
        by_source.setdefault(src, []).append(d)
    lines: list[str] = []
    for src, ds in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        ds.sort(key=lambda d: d.get("timestamp") or "", reverse=True)
        lines.append(f"{src} ({len(ds)}):")
        for d in ds[:max_per_source]:
            ts = (d.get("timestamp") or "")[11:16]
            content = (d.get("content") or "").strip().replace("\n", " ")
            if len(content) > 140:
                content = content[:140] + "…"
            lines.append(f"  {ts} {content}")
    return "\n".join(lines)


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
    # Default to the hard tier — fathom_think is the chat loop + crystal
    # regen path, both of which need tool-use + structured-output
    # reliability. Callers that want a cheaper model for a specific task
    # can pass `model=` explicitly, in which case we pair it with the
    # hard tier's current client (single-provider kwargs for now).
    from . import llm_config  # avoid circular import at module load

    if model is None:
        client, model = await llm_config.resolve_tier("hard")
    else:
        client, _ = await llm_config.resolve_tier("hard")

    # Resolve tool surface: replace, extend, or default
    resolved_tools = tools if tools is not None else TOOLS
    if extra_tools:
        resolved_tools = resolved_tools + extra_tools

    # 1. Build system prompt — default path is the full Fathom voice;
    # callers that need a clean frame (crystal regen) pass system_override.
    if system_override is not None:
        system = system_override
    else:
        # Fan out every lake read the system prompt needs in parallel.
        # Serially these added up to 600-3000 ms per turn; running them
        # concurrently collapses that to ~max(individual latency). All
        # six are independent of one another up until build_system_prompt
        # stitches the results together.
        from .tools import _agent_alive

        session_task = db.get_session(session_slug) if session_slug else None
        addressee_task = (
            delta_client.query(
                tags_include=[f"chat:{session_slug}", "participant:user"],
                limit=1,
            )
            if session_slug
            else None
        )
        (
            crystal_text,
            current_mood,
            agent_info,
            contacts_result,
            session_row,
            addressee_row,
        ) = await asyncio.gather(
            crystal.latest_text(),
            mood.maybe_synthesize_on_wake(session_slug=session_slug),
            _agent_alive(),
            contacts_mod.list_all(),
            session_task if session_task is not None else _none_coro(),
            addressee_task if addressee_task is not None else _none_coro(),
            return_exceptions=True,
        )

        # Unpack with graceful degradation — any gather entry could be an
        # exception or None placeholder.
        if isinstance(crystal_text, BaseException):
            crystal_text = ""
        if isinstance(current_mood, BaseException):
            current_mood = None
        if isinstance(agent_info, BaseException):
            agent_connected, agents_info = False, []
        else:
            agent_connected, agents_info = agent_info
        # Known contacts hydrate the "who is Fathom talking to + about"
        # context. Merged with session-addressee so the model can propose
        # new contacts instead of hallucinating slugs. list_all returns
        # a small set (typically <20); the query is 60s-cached elsewhere.
        known_contacts = [] if isinstance(contacts_result, BaseException) else contacts_result

        session_title: str | None = None
        if session_row and not isinstance(session_row, BaseException):
            session_title = session_row.get("title")

        current_contact_slug: str | None = None
        if addressee_row and not isinstance(addressee_row, BaseException):
            # The addressee of this chat session — whoever's contact: tag
            # appears on the user deltas in this thread. Read off the
            # most recent user delta via the session history.
            for t in addressee_row[0].get("tags") or []:
                if isinstance(t, str) and t.startswith("contact:"):
                    current_contact_slug = t.split(":", 1)[1]
                    break
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

    # 3. Recall — proactively surface memories before the main call.
    # Two passes run concurrently:
    #   - semantic: nl_search on the user message (catches topical recall)
    #   - structured: time-windowed digest of recent lake activity (catches
    #     "what have we been doing" / recap questions where semantic search
    #     misses because "today" isn't an axis in embedding space)
    if recall:
        conv_context = ""
        if history:
            recent = [m for m in history if m.get("role") in ("user", "assistant")][-6:]
            conv_context = "\n".join(
                f"{m['role']}: {(m.get('content') or '')[:200]}" for m in recent
            )

        recalled, digest = await asyncio.gather(
            nl_search(
                text=user_message,
                depth="deep",
                session_slug=session_slug,
                conv_context=conv_context,
            ),
            _recent_activity_digest(hours=12),
            return_exceptions=True,
        )
        if isinstance(recalled, BaseException):
            recalled = {"as_prompt": "", "total_count": 0}
        if isinstance(digest, BaseException):
            digest = ""

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

        if digest:
            messages.insert(-1, {
                "role": "system",
                "content": (
                    "Recent activity in the lake (last 12h, noise filtered, "
                    "grouped by source):\n\n"
                    f"{digest}\n\n"
                    "This is your own footprint — what you've been processing "
                    "across every surface (claude-code work sessions, feed "
                    "synthesis, agent activity, sensors). When asked about "
                    "what's been going on or what you've been doing, speak "
                    "from this directly rather than searching for it."
                ),
            })

        if on_tool_event:
            on_tool_event("result", "recall", {"count": recalled["total_count"]})

    # 4. Run the tool loop
    messages = await _resolve_tools(
        messages,
        model,
        tools=resolved_tools,
        on_tool_event=on_tool_event,
        max_rounds=max_rounds,
        session_id=session_slug,
        client=client,
        **llm_kwargs,
    )

    return messages


# ── OpenAI-compat helpers ───────────────────────

# Max time we'll wait for Fathom's reply delta before returning an empty
# completion. The listener fires on a ~3s poll and a real turn runs
# 5-30s; 120s covers tool-heavy turns without letting clients hang
# indefinitely on a stuck loop.
_OPENAI_REPLY_TIMEOUT_S = 120.0
_OPENAI_REPLY_POLL_S = 0.5


def _client_system_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]


async def _persist_client_system_once(
    session_id: str,
    content: str,
    contact_slug: str | None,
) -> bool:
    """Persist an OpenAI `system` message as a `participant:client-system`
    delta, deduped per-session by content hash.

    OpenAI clients re-send the same system message on every request. The
    first one lands as a real delta in the session — a recorded wish the
    Fathom voice can ground in (or not). Subsequent identical sends are
    no-ops. The chat listener never fires on `participant:client-system`
    deltas; they sit in the lake as session context that the first user
    message arrives into.

    Returns True if a new delta was written, False if deduped.
    """
    h = _client_system_hash(content)
    existing = await delta_client.query(
        tags_include=[f"chat:{session_id}", f"client-system-hash:{h}"],
        limit=1,
    )
    if existing:
        return False
    tags = [
        db.LAKE_CHAT_TAG,
        f"chat:{session_id}",
        "participant:client-system",
        f"client-system-hash:{h}",
    ]
    if contact_slug:
        tags.append(f"contact:{contact_slug}")
    await delta_client.write(
        content=content,
        tags=tags,
        source=db.LAKE_CHAT_SOURCE,
    )
    return True


async def _seed_chat_from_card(
    session_id: str,
    card_id: str,
    contact_slug: str | None,
) -> dict | None:
    """Snapshot a feed-card delta as Fathom's opening turn in a chat session.

    Treats the click on a feed card as Fathom starting the conversation:
    fetches the source feed-card, formats its title/body/link as markdown,
    and writes a normal `participant:fathom` chat-message delta carrying
    the card's image via media_hash. Returns the rendered seed for the
    UI to paint immediately (the alternative is waiting for the next
    poll, which would leave a flash of nothing where the locally-cloned
    card used to be).

    Idempotent per session: if any prior message already exists, the seed
    is skipped — reopening or replaying a session must not double-seed.
    Tagged `seed-card:<id>` for back-pointer; otherwise an ordinary
    assistant message that flows through the standard render path.
    """
    existing = await delta_client.query(
        tags_include=[f"chat:{session_id}"],
        limit=1,
    )
    if existing:
        return None

    try:
        original = await delta_client.get_delta(card_id)
    except Exception:
        return None
    if not original:
        return None

    raw = original.get("content") or ""
    card_payload: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                card_payload = parsed
        except Exception:
            card_payload = {}

    title = (card_payload.get("title") or "").strip()
    body = (card_payload.get("body") or "").strip()
    kicker = (card_payload.get("kicker") or "").strip()
    link = (card_payload.get("link") or "").strip()

    parts: list[str] = []
    if kicker:
        parts.append(f"*{kicker}*")
    if title:
        parts.append(f"**{title}**")
    if body:
        parts.append(body)
    if link.startswith("http://") or link.startswith("https://"):
        parts.append(f"[↗ source]({link})")
    content = "\n\n".join(parts) or title or body or "(card)"

    body_image = card_payload.get("body_image")
    media_hash = original.get("media_hash") or card_payload.get("media_hash")
    if not media_hash and isinstance(body_image, str) and re.fullmatch(r"[0-9a-f]+", body_image):
        media_hash = body_image

    seed_id = await db.add_message(
        session_id=session_id,
        role="assistant",
        content=content,
        contact_slug=contact_slug,
        extra_tags=[f"seed-card:{card_id}"],
        media_hash=media_hash,
    )
    return {
        "id": seed_id,
        "content": content,
        "media_hash": media_hash,
    }


async def _await_fathom_reply(
    session_id: str,
    after_iso: str,
    timeout_s: float = _OPENAI_REPLY_TIMEOUT_S,
) -> tuple[str, str]:
    """Poll the lake for the next Fathom reply delta in this session.

    Returns (finish_reason, content). Reasons:
      "stop"    — a real reply (or silence-ack) landed. content is the
                  reply text, stripped of any <recalled>...</recalled>
                  provenance. Silence-acks surface as empty content.
      "timeout" — no reply within timeout_s. content is empty.

    Ignores tool-event deltas (remember/recall/etc.) — only the durable
    reply or a silence-event counts as a turn completing.
    """

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            fresh = await delta_client.query(
                tags_include=[f"chat:{session_id}", "participant:fathom"],
                time_start=after_iso,
                limit=10,
            )
        except Exception as e:
            log.warning("openai-compat: poll failed: %s", e)
            fresh = []
        for d in sorted(fresh, key=lambda x: x.get("timestamp") or ""):
            ts = d.get("timestamp") or ""
            if ts <= after_iso:
                continue
            tags = d.get("tags") or []
            if "chat-event" in tags:
                if "event:silence" in tags:
                    return "stop", ""
                continue
            text = (d.get("content") or "").strip()
            text = _RECALLED_RE.sub("", text).strip()
            return "stop", text
        await asyncio.sleep(_OPENAI_REPLY_POLL_S)
    return "timeout", ""


def _openai_completion_response(
    text: str,
    session_id: str,
    model: str,
    finish_reason: str,
) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "session_id": session_id,  # Fathom extension — OpenAI clients ignore.
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _openai_stream(
    session_id: str,
    after_iso: str,
    model: str,
):
    """SSE generator for `stream: true`.

    Fathom writes the reply as one complete delta, not token-by-token, so
    we emit a single content chunk once the reply lands, then [DONE]. A
    heartbeat keep-alive frame every 15s prevents client/proxy idle
    timeouts during long turns.
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    def _chunk(delta: dict, finish: str | None = None) -> str:
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "session_id": session_id,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish,
                }
            ],
        }
        return f"data: {json.dumps(payload)}\n\n"

    # Opening role chunk so clients can render the assistant bubble early.
    yield _chunk({"role": "assistant"})


    deadline = time.monotonic() + _OPENAI_REPLY_TIMEOUT_S
    last_heartbeat = time.monotonic()
    finish_reason = "timeout"
    reply_text = ""
    while time.monotonic() < deadline:
        try:
            fresh = await delta_client.query(
                tags_include=[f"chat:{session_id}", "participant:fathom"],
                time_start=after_iso,
                limit=10,
            )
        except Exception as e:
            log.warning("openai-compat stream: poll failed: %s", e)
            fresh = []
        landed = False
        for d in sorted(fresh, key=lambda x: x.get("timestamp") or ""):
            ts = d.get("timestamp") or ""
            if ts <= after_iso:
                continue
            tags = d.get("tags") or []
            if "chat-event" in tags:
                if "event:silence" in tags:
                    finish_reason = "stop"
                    reply_text = ""
                    landed = True
                    break
                continue
            finish_reason = "stop"
            reply_text = _RECALLED_RE.sub("", d.get("content") or "").strip()
            landed = True
            break
        if landed:
            break
        now = time.monotonic()
        if now - last_heartbeat >= 15.0:
            # SSE comment frame — clients ignore, proxies stay open.
            yield ": keep-alive\n\n"
            last_heartbeat = now
        await asyncio.sleep(_OPENAI_REPLY_POLL_S)

    if reply_text:
        yield _chunk({"content": reply_text})
    yield _chunk({}, finish=finish_reason)
    yield "data: [DONE]\n\n"


# ── Endpoints ───────────────────────────────────


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    """OpenAI-shaped chat completions over Fathom's lake substrate.

    Fathom is OpenAI-*shaped*, not OpenAI-*semantic*: the conversation
    lives in Fathom's memory, not in the request's `messages` array.
    Only the latest `user` turn and any new `system` directive are
    read off the payload. Older assistant turns the client sends are
    ignored — Fathom re-orients from the lake by session, so doctoring
    past turns in the request is inert.

    Flow per request:
      1. Resolve or mint a session.
      2. Persist any `system` message as a `participant:client-system`
         delta (deduped per-session by content hash). These are recorded
         wishes, not privileged directives; the chat listener never fires
         on them.
      3. Persist the latest `user` message as a `participant:user` delta.
         That write is what the chat listener picks up (within ~3s) and
         responds to via `fathom_think`, writing a `participant:fathom`
         reply delta.
      4. Wait (poll the lake) for that reply delta to land, up to
         `_OPENAI_REPLY_TIMEOUT_S`. Return it in OpenAI completion shape,
         or stream it as SSE when `stream: true`.

    The response carries `session_id` as an extension field so the
    internal dashboard UI (which calls this same endpoint) can lock onto
    the session for its own polling cycle without caring about `choices`.
    """
    session_id = req.session_id
    if session_id:
        session = await db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    else:
        session_data = await db.create_session()
        session_id = session_data["id"]

    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")

    # Seed-card snapshot: when a chat is opened from a feed card, treat
    # the card as Fathom's opening turn. Write it as a normal assistant
    # message FIRST so its timestamp orders before the user message and
    # the conversation reads chronologically: Fathom surfaced this →
    # user replied → Fathom replied. Idempotent per session — only
    # writes when the session is brand-new with no prior messages.
    seed_payload: dict | None = None
    if req.seed_card_id:
        seed_payload = await _seed_chat_from_card(
            session_id=session_id,
            card_id=req.seed_card_id,
            contact_slug=contact_slug,
        )

    # Snapshot the wall clock BEFORE any delta writes so the reply-poller
    # ignores deltas that predate this request (including our own user
    # write). Lake timestamps are server-assigned ISO-8601.
    request_start_iso = datetime.now(UTC).isoformat()

    persisted_user = False
    # Iterate in request order so a system message preceding the first
    # user message lands in the lake first — the user message then
    # arrives into a session that already has the recorded system wish
    # as recent context, not the other way around.
    for m in req.messages:
        if m.role == "system" and isinstance(m.content, str) and m.content.strip():
            await _persist_client_system_once(session_id, m.content.strip(), contact_slug)
        elif m.role == "user" and m.content:
            content = m.content if isinstance(m.content, str) else json.dumps(m.content)
            if not req.image_uploaded:
                await db.add_message(session_id, "user", content, contact_slug=contact_slug)
            persisted_user = True
        # Other roles (assistant, tool, function) are intentionally
        # ignored — Fathom's prior turns live in the lake, not in the
        # client's replay of the conversation.

    model_label = req.model or "fathom"

    # Nothing to respond to — no user turn, no image upload. Return an
    # empty completion rather than hanging on a poll that won't fire.
    if not persisted_user and not req.image_uploaded:
        if req.stream:

            async def _empty_stream():
                chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
                payload = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_label,
                    "session_id": session_id,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": "stop"}
                    ],
                }
                yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(_empty_stream(), media_type="text/event-stream")
        return _openai_completion_response("", session_id, model_label, "stop")

    if req.stream:
        return StreamingResponse(
            _openai_stream(session_id, request_start_iso, model_label),
            media_type="text/event-stream",
        )

    finish_reason, reply_text = await _await_fathom_reply(session_id, request_start_iso)
    response = _openai_completion_response(reply_text, session_id, model_label, finish_reason)
    if seed_payload:
        # Fathom extension carrying the freshly-written feed-seed message
        # so the dashboard can paint it at the top of the new session
        # without a separate fetch. OpenAI clients ignore unknown keys.
        response["seed"] = seed_payload
    return response


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


CRYSTAL_REJECT_TTL_SECONDS = 7 * 24 * 3600


async def _record_rejected_candidate(text: str, reason: str) -> None:
    """Preserve a rejected candidate in the lake for forensics.

    Tagged crystal-reject — invisible to the crystal-regen detection
    rule so it doesn't show up on the identity ECG, but searchable
    later to diagnose what the LLM produced. Short TTL — this is a
    debug breadcrumb, not memory.
    """
    expires_at = (datetime.now(UTC) + timedelta(seconds=CRYSTAL_REJECT_TTL_SECONDS)).isoformat()
    try:
        await delta_client.write(
            content=(text or "(empty)")[:4000] + f"\n\n[rejected: {reason}]",
            tags=["crystal-reject"],
            source="consumer-api",
            expires_at=expires_at,
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
            # Best-effort: the crystal has already been written, hooks
            # missing on this tick just means resonance filters don't
            # update immediately. Log so a persistent failure (bad hook
            # config, delta-store unreachable) is visible rather than
            # silently degrading "how come my resonance doesn't work?"
            log.exception("crystal-refresh: facet hook post failed (non-fatal)")

    return {"status": "ok", "length": len(text)}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": settings.resolved_model,
                "object": "model",
                "owned_by": settings.provider,
            }
        ],
    }


@app.get("/v1/settings/models", dependencies=[Depends(auth.require_admin)])
async def settings_models():
    """Tier-aware model config for the Settings → Models UI.

    Returns the configured providers (those with credentials in .env),
    the current pick per tier (resolved via llm_config), and each
    provider's recommended pick per tier so the UI can mark matches
    as "recommended". Cross-provider tier selection is supported —
    each tier carries {provider, model}, not just a model string.
    """
    from . import llm_config
    from .settings import PROVIDER_DEFAULTS

    configured = settings.configured_providers()
    provider_defaults = {
        p: {tier: PROVIDER_DEFAULTS.get(p, {}).get(tier, "") for tier in llm_config.VALID_TIERS}
        for p in configured
    }

    tiers = []
    for tier_id, label, uses in (
        ("hard", "Main chat & heavy work", "Chat loop, identity-crystal regeneration"),
        ("medium", "Standard tasks", "Search planning, mood synthesis, feed crystal"),
    ):
        config = await llm_config.get_tier_config(tier_id)
        tiers.append(
            {
                "id": tier_id,
                "label": label,
                "uses": uses,
                "current": {
                    "provider": config.get("provider", ""),
                    "model": config.get("model", ""),
                },
            }
        )
    return {
        "providers": configured,
        "provider_defaults": provider_defaults,
        "tiers": tiers,
    }


class _TierPick(BaseModel):
    provider: str
    model: str


@app.put("/v1/settings/models/{tier}", dependencies=[Depends(auth.require_admin)])
async def settings_models_put(tier: str, body: _TierPick):
    """Persist a tier's (provider, model) pick as a lake config delta.

    Takes effect on the next turn — the resolver caches for a few
    seconds but set_tier_config invalidates the cache on write.
    """
    from . import llm_config

    try:
        written = await llm_config.set_tier_config(tier, body.provider, body.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "ok": True,
        "tier": tier,
        "provider": body.provider,
        "model": body.model,
        "id": written.get("id"),
    }


@app.get("/v1/settings/providers/{provider}/models", dependencies=[Depends(auth.require_admin)])
async def settings_provider_models(provider: str):
    """Proxy a provider's /v1/models so the UI can populate dropdowns.

    All three providers Fathom speaks to (gemini, openai, ollama) expose
    an OpenAI-compatible /v1/models list, so the query is uniform. Keys
    never leave the server — the UI just gets the id list back.
    """
    from . import llm_config  # noqa: F401 — used via providers registry
    from .providers import get_client

    if provider not in settings.configured_providers():
        raise HTTPException(status_code=404, detail=f"provider '{provider}' not configured")

    try:
        client = get_client(provider)
        resp = await client.models.list()
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"provider {provider} models list failed: {e}"
        ) from e

    # Normalize to [{"id": ...}, ...] — different providers decorate
    # the response differently, but the id field is the one we want.
    models = []
    for m in getattr(resp, "data", []) or []:
        mid = getattr(m, "id", None)
        if mid:
            models.append({"id": mid})
    return {"provider": provider, "models": models}


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
        "edition": settings.fathom_edition,
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
                facets.append(
                    {
                        "label": current_label,
                        "text": "\n".join(current_lines).strip(),
                    }
                )
            current_label = m.group(1).strip()
            current_lines = []
        elif current_label is not None:
            current_lines.append(line)

    if current_label and current_lines:
        facets.append(
            {
                "label": current_label,
                "text": "\n".join(current_lines).strip(),
            }
        )

    return facets


# ── Static UI (must be last — catches everything unmatched above) ───

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.is_dir():
    # The UI is a single self-contained index.html (CSS+JS inline). It
    # changes every rebuild, so any browser cache means stale dashboards
    # after `docker compose up --build`. Force revalidation on every load.
    _NO_CACHE_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/")
    async def ui_root():
        return FileResponse(_UI_DIR / "index.html", headers=_NO_CACHE_HEADERS)

    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
