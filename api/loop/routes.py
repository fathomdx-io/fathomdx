"""HTTP surface for the Grand Loop — composer writes, dashboard reads.

Endpoint groups:

  Composer side (write):
    POST /v1/puddle/seed                — drop a seed (dual-write
                                          puddle + lake; user-authored
                                          is durable from the moment
                                          of typing)
    POST /v1/puddle/cards/{id}/engage   — author an engaged card into
                                          the lake (more / less / chat)

  Dashboard side (read):
    GET  /v1/puddle/cards    — feed-card witness outputs currently alive
    GET  /v1/puddle/intents  — pending intent queue (key UI element)
    GET  /v1/puddle/feed     — chronological unified stream
    GET  /v1/puddle/stream   — SSE: every puddle write fans out here
    GET  /v1/puddle/stats    — quick health snapshot
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import delta_client
from .intents import (
    CONVO_TAG,
    INTENT_TTL_BY_KIND,
    Q_A_TTL_S,
    intent_kind,
    pending_intents,
    write_intent,
)
from .puddle import puddle


router = APIRouter()


class SeedRequest(BaseModel):
    content: str
    kind: str = "question"
    extra_tags: list[str] | None = None
    media_hash: str | None = None


@router.post("/v1/puddle/seed")
async def post_seed(req: SeedRequest, request: Request) -> dict:
    """Drop a seed delta into the puddle AND the lake.

    Typing a seed is authoring — the lake gets it durable from the
    moment of writing. The puddle gets the immediate echo so the
    loop's next round and the dashboard's feed see it without waiting
    for telepathy to reflect it back. The puddle copy carries
    `lake-id:<full>` and `recalled-id:<24chars>` so telepathy
    correctly dedupes (composer is also in MIRROR_NOISE_SOURCES, so
    the filter and the tag are belt-and-suspenders here).

    Lake write is best-effort: a transient lake hiccup must not block
    the user from sending. The puddle write always lands.

    Contact tagging: the auth middleware resolves the bearer token to
    a contact and stamps it on `request.state.contact`. We propagate
    `contact:<slug>` server-side so Fathom knows who's typing — the
    browser composer never sets this in `extra_tags` and the loop's
    prompts now read it via the seed block + witness intent line.
    """
    kind = req.kind.strip().lower() or "question"
    if kind not in INTENT_TTL_BY_KIND:
        kind = "question"
    body = req.content.strip()

    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")
    contact_tag = f"contact:{contact_slug}" if contact_slug else None

    lake_tags = ["user-seed", f"kind:{kind}"]
    if req.extra_tags:
        lake_tags.extend(req.extra_tags)
    if contact_tag and contact_tag not in lake_tags:
        lake_tags.append(contact_tag)
    media_hash = (req.media_hash or "").strip() or None
    lake_id = ""
    try:
        lake_delta = await delta_client.write(
            content=body,
            tags=lake_tags,
            source="composer",
            media_hash=media_hash,
        )
        if isinstance(lake_delta, dict):
            lake_id = lake_delta.get("id") or ""
    except Exception as e:
        print(f"[seed] lake write failed (puddle still writing): {type(e).__name__}: {e}")

    puddle_extra: list[str] = list(req.extra_tags or [])
    if contact_tag and contact_tag not in puddle_extra:
        puddle_extra.append(contact_tag)
    if lake_id:
        puddle_extra.append(f"lake-id:{lake_id}")
        puddle_extra.append(f"recalled-id:{lake_id[:24]}")
    delta = await write_intent(
        kind=kind,
        content=body,
        extra_tags=puddle_extra,
        source="composer",
    )

    # Phase 1 shadow write: also append to the global thread.
    # Soft-fails — the puddle write above is still authoritative
    # while Phase 2 brings the harness onto the thread.
    try:
        from .. import thread as thread_mod
        await thread_mod.append(
            role="user",
            msg_kind=f"composer-{kind}" if kind != "question" else "composer",
            content=body,
            channel="composer",
            contact=contact_slug or "",
            source="composer",
            media_hash=media_hash or "",
        )
    except Exception as e:
        print(f"[seed] thread shadow write failed: {type(e).__name__}: {e}")

    return {"ok": True, "intent": delta, "lake_id": lake_id}


@router.get("/v1/puddle/feed")
async def get_feed(
    until: str | None = None,
    hours: float = 1.0,
    limit: int = 500,
) -> dict:
    """Unified feed: user seeds + fathom replies + witness cards in one
    chronological list, newest first.

    The dashboard renders three component shapes from this one stream:
      * kind=user-message   → renderTurn({role:'user'})    .msg-user
      * kind=fathom-message → renderTurn({role:'fathom'})  .msg-fathom
      * kind=card           → renderStoryCard()            .card

    A user's seed (kind:question intent) appears as user-message regardless
    of whether the witness has addressed it yet — leaving the seed visible
    after its reply lands keeps the conversation legible. The witness
    output's `addresses` list points back to the seed it answered.

    Cards are decoupled from the puddle TTL: every fire's feed-card is
    durable in the lake (the threaded harness writes there first, then
    mirrors into the puddle). For each window we read both, then drop
    any lake card already represented in the puddle via its
    `recalled-id:<lake_id[:24]>` / `lake-id:<full>` mirror tags. Live
    edge stays puddle-fast; older windows hydrate from the lake even
    after the mirror has aged out.
    """
    # Time-windowed pagination: each page is the last `hours` of the
    # puddle counted backward from `until` (defaults to now). The dashboard
    # walks back one hour at a time on Show-more. Lots of different item
    # kinds can land in any window — voice thoughts, recalls, intents,
    # cards — so paging by time gives a stable rhythm; paging by item
    # count would mean "show more" pulls inconsistent slices when the
    # puddle is dense.
    from datetime import datetime, timedelta, UTC

    until_dt = (
        datetime.fromisoformat(until.replace("Z", "+00:00"))
        if until else datetime.now(UTC)
    )
    since_dt = until_dt - timedelta(hours=hours)
    until_iso = until_dt.isoformat()
    since_iso = since_dt.isoformat()

    items: list[dict] = []
    raw = puddle.query(
        tags_include=[CONVO_TAG],
        time_start=since_iso,
        time_end=until_iso,
        limit=limit,
    )

    # Lake feed-cards in the same window — durable counterparts to the
    # puddle mirror. For the live edge these mostly duplicate `raw` and
    # get deduped below; for older windows they're the only source.
    lake_cards: list[dict] = []
    try:
        lake_cards = await delta_client.query(
            tags_include=["feed-card"],
            time_start=since_iso,
            time_end=until_iso,
            limit=limit,
        ) or []
    except Exception as e:
        print(f"[feed] lake feed-card fetch failed: {type(e).__name__}: {e}")
        lake_cards = []

    # Card anchor — on the live edge (until is None), the active hour
    # is often sparse but the user still wants to see Fathom's recent
    # cards. The puddle's chat anchor only pulls user-seeds + chat-
    # replies (route:chat-reply), so editorial cards (route:feed-card,
    # route:helper) never appeared at all when the live edge was quiet.
    # Pull the most recent N feed-cards from before the window straight
    # from the lake; dedupe handles any overlap with the in-window
    # query and the puddle mirror tags.
    if until is None:
        try:
            anchor_cards = await delta_client.query(
                tags_include=["feed-card"],
                time_end=since_iso,
                limit=50,
            ) or []
            lake_cards = lake_cards + anchor_cards
        except Exception as e:
            print(f"[feed] lake card-anchor fetch failed: {type(e).__name__}: {e}")

    # Routine-due dedupe: a single cron tick produces TWO puddle items —
    # the witness's own write_intent (kind:routine-due) and telepathy's
    # later mirror of the durable routine-tick lake delta. Both
    # classify as kind:"routine-due" with the same routine_id and
    # near-identical timestamps. Track (routine_id, minute) pairs as
    # we walk the deduped list and skip the second arrival, preferring
    # whichever lands first (puddle-write usually beats mirror, but
    # either is a fine breadcrumb for the user).
    _routine_due_seen: set[tuple[str, str]] = set()

    # Chat anchor — on the live edge, also pull chat-shape deltas
    # (user seeds + witness chat-replies) from BEFORE the primary
    # window so the conversation always grounds the view, even when
    # the active hour is otherwise quiet. Bounded by the puddle's TTL;
    # anything evicted is gone from this query and the dashboard would
    # need to page back via Show-more (or the lake) to see it.
    chat_anchor_raw: list[dict] = []
    if until is None:
        anchor_pool = puddle.query(
            tags_include=[CONVO_TAG],
            time_end=since_iso,
            limit=200,
        )
        for d in anchor_pool:
            d_tags = set(d.get("tags") or [])
            is_user_seed = "kind:question" in d_tags and (
                "intent" in d_tags or "user-seed" in d_tags
            )
            is_chat_reply = "feed-card" in d_tags and "route:chat-reply" in d_tags
            if is_user_seed or is_chat_reply:
                chat_anchor_raw.append(d)

    # Dedupe by id when concatenating; the primary window query and
    # the chat anchor's `time_end=since_iso` are non-overlapping by
    # construction, but defensive against any boundary clock skew.
    #
    # Also cross-reference dedupe: a composer seed dual-writes a puddle
    # intent (carrying `lake-id:<X>` + `recalled-id:<X[:24]>`) AND a
    # durable lake delta `<X>`. Telepathy then mirrors the lake delta
    # back into the puddle as a SECOND entry tagged `recalled-id:<X[:24]>`.
    # Both items share the originating lake id but their puddle-row ids
    # differ, so the by-id pass above can't catch it. Without this
    # second pass the user sees their seed (and any attached image)
    # rendered twice in the feed.
    seen_ids: set[str] = set()
    seen_lake_keys: set[str] = set()
    deduped: list[dict] = []
    for d in raw + chat_anchor_raw:
        d_id = d.get("id")
        if not d_id or d_id in seen_ids:
            continue
        lake_key = ""
        for t in d.get("tags") or []:
            if isinstance(t, str):
                if t.startswith("lake-id:"):
                    lake_key = t.split(":", 1)[1]
                    break
                if t.startswith("recalled-id:") and not lake_key:
                    lake_key = t.split(":", 1)[1]
        if lake_key:
            short_key = lake_key[:24]
            if short_key in seen_lake_keys:
                continue
            seen_lake_keys.add(short_key)
        seen_ids.add(d_id)
        deduped.append(d)

    # Lake-card dedupe: walk the puddle items first to gather every lake
    # id they reference, then drop matching lake cards. Puddle items
    # carry `recalled-id:<lake_id[:24]>` (telepathy + bridge convention)
    # and sometimes `lake-id:<full>` (witness dual-write). Either tag
    # identifies the lake counterpart.
    puddle_lake_short: set[str] = set()
    puddle_lake_full: set[str] = set()
    for d in deduped:
        for t in d.get("tags") or []:
            if isinstance(t, str):
                if t.startswith("recalled-id:"):
                    puddle_lake_short.add(t.split(":", 1)[1])
                elif t.startswith("lake-id:"):
                    puddle_lake_full.add(t.split(":", 1)[1])
    for c in lake_cards:
        c_id = c.get("id") or ""
        if not c_id or c_id in seen_ids:
            continue
        if c_id in puddle_lake_full or c_id[:24] in puddle_lake_short:
            continue
        seen_ids.add(c_id)
        deduped.append(c)

    for d in deduped:
        tags = set(d.get("tags") or [])
        ts = d.get("timestamp")
        # Drop agent heartbeats — they're connection signals, not
        # feed content. Telepathy already filters them at mirror time,
        # so this is defensive against any other path that might land
        # one in the puddle.
        if "agent-heartbeat" in tags:
            continue
        # Drop engagement events — alerts, read-receipts, scroll-past
        # markers, crystal-reject deltas. These feed the engagement
        # crystal and pressure metrics, but as feed rows they're noise:
        # the user doesn't want to see "you scrolled past card X" or
        # "viewed-at <timestamp>" rendered alongside chat and cards.
        # Source matches the post-rename string ("fathom-engagement");
        # old `consumer-api`-tagged deltas in the lake will TTL out.
        d_source = d.get("source") or ""
        if d_source in ("fathom-engagement", "consumer-api"):
            continue
        # Pull the session tag out so the frontend can cluster items by
        # (kind, session) — that way all voices from one deliberation
        # pile into a single accordion even when recall results
        # interleave them chronologically.
        session_tag = next(
            (t for t in tags if t.startswith("session:")),
            None,
        )
        common = {
            "id": d.get("id"),
            "timestamp": ts,
            "expires_at": d.get("expires_at"),
            "source": d.get("source"),
            "tags": list(d.get("tags") or []),
            "session": session_tag,
            "media_hash": d.get("media_hash") or None,
        }

        # ── Q (always visible by default) ─────────────────
        # Match both vocabularies: `intent`+`kind:question` is the
        # puddle-native shape (write_intent), `user-seed`+`kind:question`
        # is the lake-side shape (post_seed's durable write). Telepathy
        # may surface the lake shape directly when restoring on cold-
        # start, and the renderer should treat both as the same kind.
        if "kind:question" in tags and ("intent" in tags or "user-seed" in tags):
            items.append({
                "kind": "user-message",
                "content": d.get("content") or "",
                **common,
            })
            continue

        # ── A — witness output, split by route ────────────
        if "feed-card" in tags:
            try:
                payload = json.loads(d.get("content") or "{}")
            except Exception:
                payload = {"body": d.get("content") or ""}
            route = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("route:")),
                "chat-reply",
            )
            addresses = [t.split(":", 1)[1] for t in tags if t.startswith("addresses:")]
            base = {
                **common,
                "addresses": addresses,
                "route": route,
                **payload,
            }
            if route == "helper":
                # Outbound dispatch: Fathom asking a helper agent on a
                # specific machine to do work. Body is the literal prompt
                # the agent reads. Surface the (role, host) pair so the
                # renderer can show full addressing in the kicker.
                host = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("host:")),
                    "",
                )
                role = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("helper-role:")),
                    "",
                )
                items.append({"kind": "helper-dispatch", "host": host, "role": role, **base})
            elif route == "chat-reply":
                sit_round_v = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("sit-round:")),
                    None,
                )
                sit_max_v = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("sit-max-rounds:")),
                    None,
                )
                sit_session_v = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("sit-session:")),
                    "",
                )
                sit_extra = {}
                if sit_round_v is not None:
                    try:
                        sit_extra["sit_round"] = int(sit_round_v)
                        sit_extra["sit_max_rounds"] = int(sit_max_v or "4")
                        sit_extra["sit_session"] = sit_session_v
                    except (ValueError, TypeError):
                        pass
                items.append({"kind": "fathom-message", **base, **sit_extra})
            elif "kind:proposal" in tags:
                # Tool-call proposal — operator-actionable card with
                # Edit / Deny / Approve buttons. The decision delta
                # lives separately tagged `proposal-decision decides:<id>`.
                tool = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("tool:")),
                    "",
                )
                # L1/L2 provenance proposals auto-approve at draft time.
                # The harness-review row in the thinking accordion already
                # surfaces "provenance made" — duplicating as a feed card
                # is noise. Skip.
                if tool == "provenance":
                    level: int | None = None
                    for t in tags:
                        if isinstance(t, str) and t.startswith("provenance-level:"):
                            try:
                                level = int(t.split(":", 1)[1])
                            except (TypeError, ValueError):
                                level = None
                            break
                    if level is not None and level <= 2:
                        continue
                # `lake-id:<full>` is the witness's own dual-write cross-
                # pointer; `recalled-id:<short>` is what telepathy stamps
                # when it mirrors a durable lake delta back into the puddle
                # (e.g. after an api restart wipes the puddle and telepathy
                # re-hydrates from the lake). Either tag identifies the
                # same lake delta, so check both.
                lake_id = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("lake-id:")),
                    "",
                ) or next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("recalled-id:")),
                    "",
                )
                items.append({
                    "kind": "proposal",
                    "tool": tool,
                    "lake_id": lake_id,
                    **base,
                })
            else:
                items.append({"kind": "card", **base})
            continue

        # ── Auxiliary types (filter-toggleable) ───────────
        # Harness turn trace — one entry per tool call. Body is a
        # JSON envelope (turn, tool, thinking, args_json, result, error)
        # so the dashboard can render the full per-turn shape — same
        # info harness-test.html shows live via SSE.
        #
        # respond turns are intentionally suppressed — they're redundant
        # with the actual card that lands in the feed. The thinking
        # accordion shouldn't repeat what the user already sees as a
        # first-class card row.
        if "kind:harness-turn" in tags:
            tool = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("tool:")),
                "",
            )
            if tool == "respond":
                continue
            turn_n = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("turn:")),
                "",
            )
            plan_step = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("plan-step:")),
                "",
            )
            session = next(
                (t for t in tags if isinstance(t, str) and t.startswith("session:")),
                "",
            )
            raw = d.get("content") or ""
            envelope = {}
            try:
                parsed_env = json.loads(raw) if raw else {}
                if isinstance(parsed_env, dict):
                    envelope = parsed_env
            except Exception:
                envelope = {"thinking": raw[:200]}
            items.append({
                "kind": "harness-turn",
                "tool": tool,
                "turn": turn_n,
                "plan_step": plan_step,
                "session": session,
                "thinking": envelope.get("thinking") or "",
                "args_json": envelope.get("args_json") or "",
                "result": envelope.get("result") or "",
                "error": envelope.get("error") or "",
                "content": raw,  # legacy for any client expecting it
                **common,
            })
            continue
        # Harness review pass — what the post-response review decided.
        # Same per-fire grouping as turns; rendered inside the same
        # accordion block.
        if "kind:harness-review" in tags:
            review_kind = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("review-kind:")),
                "",
            )
            session = next(
                (t for t in tags if isinstance(t, str) and t.startswith("session:")),
                "",
            )
            raw = d.get("content") or ""
            envelope = {}
            try:
                parsed_env = json.loads(raw) if raw else {}
                if isinstance(parsed_env, dict):
                    envelope = parsed_env
            except Exception:
                envelope = {"detail": raw[:200]}
            items.append({
                "kind": "harness-review",
                "review_kind": review_kind,
                "session": session,
                "detail": envelope.get("detail") or "",
                "args_json": envelope.get("args_json") or "",
                "content": raw,
                **common,
            })
            continue
        # Voice thoughts — legacy parliament shape, retained for old
        # puddle entries still in TTL window.
        if "thought" in tags:
            voice = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("voice:")),
                None,
            )
            items.append({
                "kind": "voice-thought",
                "voice": voice,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Pulse intents (reflection / drift / bridging / alert) — these
        # are pressure-watcher pass intents that haven't been addressed
        # by witness yet. Surface them so the queue is visible.
        if "intent" in tags:
            kind = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("kind:")),
                "unknown",
            )
            if kind in ("reflection", "drift", "bridging", "alert", "drop-in", "feed"):
                items.append({
                    "kind": "pass-intent",
                    "pass_kind": kind,
                    "content": d.get("content") or "",
                    **common,
                })
            elif kind == "routine-due":
                # Routine cron tick — surfaces as a "routine fired" marker
                # in the feed so the user sees the trigger that caused
                # whatever the witness emitted next. Carries routine-id so
                # the renderer can link to the routine detail page.
                routine_id = next(
                    (t.split(":", 1)[1] for t in tags if t.startswith("routine-id:")),
                    "",
                )
                _ts_minute = (ts or "")[:16]  # YYYY-MM-DDTHH:MM
                _key = (routine_id, _ts_minute)
                if _key not in _routine_due_seen:
                    _routine_due_seen.add(_key)
                    items.append({
                        "kind": "routine-due",
                        "routine_id": routine_id,
                        "content": d.get("content") or "",
                        **common,
                    })
            continue
        # Crystal facets — identity layer.
        if "crystal" in tags:
            facet = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("facet:")),
                None,
            )
            items.append({
                "kind": "crystal",
                "facet": facet,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Mood / felt-sense.
        if "mood" in tags:
            feeling = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("feeling:")),
                None,
            )
            items.append({
                "kind": "mood",
                "feeling": feeling,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Routine activity — three shapes:
        #   · routine-tick — cron tick handed the routine to the River
        #     (surfaces as kind:"routine-due" with variant "fired", the
        #     durable lake-side counterpart of the puddle's routine-due
        #     intent so the marker survives api restarts).
        #   · routine-fire — legacy direct-to-kitty path (variant "fire").
        #   · routine-summary — claude-code's writeup back from a fire
        #     (variant "summary").
        # All carry routine-id so the dashboard renders the routine name.
        if (
            "routine-fire" in tags
            or "routine-summary" in tags
            or "routine-tick" in tags
        ):
            routine_id = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("routine-id:")),
                None,
            )
            if "routine-tick" in tags:
                _ts_minute = (ts or "")[:16]
                _key = (routine_id or "", _ts_minute)
                if _key not in _routine_due_seen:
                    _routine_due_seen.add(_key)
                    items.append({
                        "kind": "routine-due",
                        "routine_id": routine_id,
                        "content": d.get("content") or "",
                        **common,
                    })
            else:
                items.append({
                    "kind": "routine",
                    "routine_id": routine_id,
                    "summary": "routine-summary" in tags,
                    "content": d.get("content") or "",
                    **common,
                })
            continue
        # Claude-code task channel — closure deltas from a tasked
        # claude-code session, plus any other claude-code:task source
        # output. Routed under its own kind so the Claude Code filter
        # category (on by default) surfaces them, instead of getting
        # buried under `thinking` with the rest of the lake-delta noise.
        # `task-abandoned` (kitty noticed the window died with no
        # completion) is a closure too — render it the same shape so
        # an early-killed session shows the wrap rather than orphaning
        # the dispatch card forever.
        if (
            "task-complete" in tags
            or "task-abandoned" in tags
            or d.get("source") == "claude-code:task"
        ):
            host = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("host:")),
                "",
            )
            role = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("helper-role:")),
                "",
            )
            items.append({
                "kind": "helper-reply",
                "host": host,
                "role": role,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Lake delta — telepathy mirror of recent durable lake activity
        # (RSS arrivals, claude-code session deltas, anything new in the
        # lake that isn't loop-output noise). Surfaced under its own kind
        # so the filter can show "raw lake" separately from model-driven
        # recall results.
        if "lake-delta" in tags:
            from_source = next(
                (t.split(":", 1)[1] for t in tags if t.startswith("from-source:")),
                None,
            )
            items.append({
                "kind": "lake-delta",
                "from_source": from_source,
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Compositional recall results — present and future model-driven
        # recall pulls (separate from the continuous mirror above).
        if "recall-result" in tags or "mirror" in tags:
            items.append({
                "kind": "recall",
                "content": d.get("content") or "",
                **common,
            })
            continue
        # Process events (spawn/die/metric) are no longer written by
        # process.py — they were interleaving between voice thoughts in
        # the feed and breaking the cluster. Defensive: silently drop any
        # legacy process-event still alive in the puddle from before the
        # change. They TTL out on their own.
        if "process-event" in tags or "metric" in tags:
            continue
        # Permissive fallthrough — anything else with content surfaces
        # as a generic delta carrying its tags. The puddle is a
        # passive substrate; the frontend dispatches on tags. Critical
        # for items like feed-engagement markers that get dual-written
        # directly to the puddle (no telepathy "lake-delta" stamp) and
        # need to round-trip through the feed so _projectEngagement
        # can read them. The frontend renderer falls back to the
        # lake-delta gray row for unknown kinds, but _projectEngagement
        # filters feed-engagement markers out before rendering.
        from_source = next(
            (t.split(":", 1)[1] for t in tags if t.startswith("from-source:")),
            None,
        )
        items.append({
            "kind": "lake-delta",
            "from_source": from_source or d.get("source") or "unknown",
            "content": d.get("content") or "",
            **common,
        })

    items.sort(key=lambda it: it.get("timestamp") or "", reverse=True)
    # has_more is true if either the puddle OR the lake holds anything
    # older than the window's `since` boundary. The puddle peek catches
    # ephemera (intents, voice thoughts) still alive within TTL; the
    # lake peek catches feed-cards that have aged out of the puddle but
    # are still walkable via Show-more.
    older = puddle.query(
        tags_include=[CONVO_TAG],
        time_end=since_iso,
        limit=1,
    )
    if not older:
        try:
            older_lake = await delta_client.query(
                tags_include=["feed-card"],
                time_end=since_iso,
                limit=1,
            ) or []
        except Exception as e:
            print(f"[feed] lake has_more peek failed: {type(e).__name__}: {e}")
            older_lake = []
    else:
        older_lake = []
    return {
        "items": items,
        "since": since_iso,
        "until": until_iso,
        "hours": hours,
        "has_more": bool(older or older_lake),
    }


@router.get("/v1/puddle/cards")
def get_cards(limit: int = 50) -> dict:
    """Witness outputs (feed-card synthesis deltas) alive in the puddle.

    Each card's `content` is JSON with kicker/title/body/tail/route/axes.
    The dashboard parses and renders. Newest-first.
    """
    raw = puddle.query(
        tags_include=[CONVO_TAG, "feed-card"],
        limit=limit,
    )
    cards = []
    for d in raw:
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            payload = {"body": d.get("content") or ""}
        addressed = [
            t.split(":", 1)[1]
            for t in (d.get("tags") or [])
            if t.startswith("addresses:")
        ]
        cards.append({
            "id": d.get("id"),
            "timestamp": d.get("timestamp"),
            "expires_at": d.get("expires_at"),
            "addresses": addressed,
            **payload,
        })
    return {"cards": cards}


@router.get("/v1/puddle/intents")
async def get_intents() -> dict:
    """Pending intent queue. The 'what's next' indicator the dashboard
    polls for the throbber.

    With FATHOM_THREADED_HARNESS=1, the queue source is the thread —
    project thread.unaddressed into the same shape so the dashboard
    keeps working without changes. With the flag off, read the puddle
    as before.
    """
    import os
    if os.environ.get("FATHOM_THREADED_HARNESS", "0") == "1":
        from .. import thread as thread_mod
        window = await thread_mod.build_window()
        out = []
        for d in window["unaddressed"]:
            msg_kind = "question"
            for t in d.get("tags") or []:
                if isinstance(t, str) and t.startswith("msg-kind:"):
                    msg_kind = t.split(":", 1)[1]
                    break
            out.append({
                "id": d.get("id"),
                "kind": msg_kind,
                "content": d.get("content") or "",
                "timestamp": d.get("timestamp"),
                "expires_at": None,
            })
        return {"intents": out}

    pending = pending_intents()
    out = []
    for d in pending:
        out.append({
            "id": d.get("id"),
            "kind": intent_kind(d),
            "content": d.get("content") or "",
            "timestamp": d.get("timestamp"),
            "expires_at": d.get("expires_at"),
        })
    return {"intents": out}


# ── Thread endpoints ──────────────────────────────────────────────
#
# Phase 4 surface for the dashboard / external clients to read what's
# in Fathom's working-memory thread. Counterpart to the legacy
# /v1/puddle/feed which renders the puddle. Both can coexist during
# the migration; Phase 5 deletes the puddle ones.


def _project_thread_msg(d: dict) -> dict:
    """Project a kind:thread-msg lake delta into a UI-friendly dict.

    Pulls the role / msg-kind / channel / correlation / contact /
    addresses tags out as first-class fields so the dashboard renders
    on structured shape rather than scanning tag strings client-side.
    """
    tags = d.get("tags") or []

    def tag_value(prefix: str) -> str:
        for t in tags:
            if isinstance(t, str) and t.startswith(prefix):
                return t.split(":", 1)[1]
        return ""

    addresses: list[str] = []
    for t in tags:
        if isinstance(t, str) and t.startswith("addresses:"):
            v = t.split(":", 1)[1]
            if v and v not in addresses:
                addresses.append(v)

    return {
        "id": d.get("id"),
        "timestamp": d.get("timestamp"),
        "role": tag_value("role:"),
        "msg_kind": tag_value("msg-kind:"),
        "channel": tag_value("channel:"),
        "correlation": tag_value("correlation:"),
        "contact": tag_value("contact:"),
        "addresses": addresses,
        "content": d.get("content") or "",
        "source": d.get("source") or "",
    }


@router.get("/v1/thread/window")
async def get_thread_window(
    limit: int = 15,
    max_tokens: int | None = 12_000,
) -> dict:
    """Return Fathom's rolling-window thread tail.

    Same view the harness sees on each fire — last `limit` messages,
    further trimmed if the estimated token total exceeds `max_tokens`.
    Oldest-first (chronological), so a renderer can append top-down.

    Each message carries role + msg_kind + channel + correlation +
    contact + addresses as structured fields. The dashboard's river
    view renders bubbles by role; surfaces (composer, openai client,
    routine page) can filter by channel/correlation to show their own
    slice.
    """
    from .. import thread as thread_mod

    rows = await thread_mod.tail(limit=limit, max_tokens=max_tokens)
    messages = [_project_thread_msg(d) for d in rows]
    return {
        "messages": messages,
        "count": len(messages),
        "window_limit": limit,
        "max_tokens": max_tokens,
    }


@router.get("/v1/thread/unaddressed")
async def get_thread_unaddressed(
    limit: int = 15,
    max_tokens: int | None = 12_000,
) -> dict:
    """Return user-role messages in the window with no covering tally
    mark — Fathom's active "what's pending" queue.

    The dashboard's tally overlay renders this when count >= 1.
    Each entry includes a 200-char preview alongside the full body
    so a small panel can show meaningful text without re-fetching.
    """
    from .. import thread as thread_mod

    window = await thread_mod.build_window(
        max_messages=limit,
        max_tokens=max_tokens,
    )
    pending = window["unaddressed"]
    out = []
    for d in pending:
        proj = _project_thread_msg(d)
        body = proj["content"] or ""
        proj["preview"] = body[:200]
        out.append(proj)
    return {"unaddressed": out, "count": len(out)}


@router.get("/v1/loop/relief-tiers")
async def relief_tiers() -> dict:
    """Tier table + thresholds for the dashboard pressure chart.

    Returns each tier's name, engine, pressure-floor ratio, consume
    weight, and cooldown — plus the absolute pressure threshold so
    the UI can render reference lines on the pressure chart.
    """
    from . import relief
    from ..settings import settings

    threshold = settings.feed_pressure_threshold
    return {
        "threshold": threshold,
        "tiers": [
            {
                "name": t["name"],
                "engine": t["engine"],
                "min_pressure_ratio": t["min_pressure_ratio"],
                "min_pressure_value": t["min_pressure_ratio"] * threshold,
                "consume_weight": t["consume_weight"],
                "cooldown_seconds": t["cooldown_seconds"],
            }
            for t in relief.RELIEF_TIERS
        ],
    }


@router.get("/v1/loop/relief-cooldowns")
async def relief_cooldowns() -> dict:
    """Current cooldown state for every fire button in the Weather card.

    Returns each tier's last_fired_at, cooldown_seconds, and
    remaining_seconds so the UI can disable locked buttons on load
    rather than waiting for the first failed click to discover the lock.
    """
    from datetime import UTC, datetime
    from . import relief
    from ..settings import settings

    now = datetime.now(UTC)
    state = relief._load_cooldowns()

    def _remaining(tier_name: str, cooldown_s: int) -> dict:
        last_iso = state.get(tier_name)
        if not last_iso:
            return {"last_fired_at": None, "cooldown_seconds": cooldown_s, "remaining_seconds": 0}
        try:
            last = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            elapsed = (now - last).total_seconds()
            remaining = max(0.0, cooldown_s - elapsed)
        except Exception:
            remaining = 0.0
            last_iso = None
        return {
            "last_fired_at": last_iso,
            "cooldown_seconds": cooldown_s,
            "remaining_seconds": int(remaining),
        }

    tiers = {t["name"]: _remaining(t["name"], t["cooldown_seconds"]) for t in relief.RELIEF_TIERS}

    return {"tiers": tiers}


@router.post("/v1/puddle/pulse")
async def fire_pulse(reason: str = "manual", tier: str | None = None) -> dict:
    """Trigger one tier of pressure relief.

    Manual entry — the pressure-watcher's automatic path has its own
    floor gate (only fires when pressure crosses thresholds). This
    endpoint bypasses the floor so a button click always fires
    SOMETHING when nothing's on cooldown. Per-tier cooldowns still
    apply, so spamming the button can't loop-pin one tier.

    `tier=<name>` (optional) — fire a specific tier (`alert`,
    `bridging`, `reflection`, `drift`) instead of letting the picker
    walk cheapest-first. Powers the per-tier buttons in the Weather
    card header. Cooldown still gates; on-cooldown returns
    `fired: None, reason: on_cooldown`.

    Returns the relief outcome — which tier fired, on what engine,
    or `fired: None` if every tier is on cooldown (or the requested
    tier is on cooldown when `tier=` is set).
    """
    from . import relief
    result = await relief.fire_relief(
        reason, bypass_floor=True, force_tier=tier
    )
    return {"ok": True, **result}


@router.get("/v1/puddle/stats")
def get_stats() -> dict:
    return puddle.stats()


@router.get("/v1/puddle/metrics")
def get_metrics(per_voice: int = 8) -> dict:
    """Recent cross-voice convergence samples per voice.

    Drives the dashboard's convergence dots — each dot's x-position
    tracks (1 - distance) of the latest sample for that voice. Returns
    a dict keyed by voice name; each value is a list of {timestamp,
    distance} sorted oldest-first so the renderer can take the tail.
    """
    raw = puddle.query(tags_include=[CONVO_TAG, "metric"], limit=200)
    by_voice: dict[str, list[dict]] = {}
    for d in raw:
        voice = next(
            (t.split(":", 1)[1] for t in (d.get("tags") or []) if t.startswith("voice:")),
            None,
        )
        if not voice:
            continue
        try:
            payload = json.loads(d.get("content") or "{}")
        except Exception:
            continue
        dist = payload.get("distance")
        if not isinstance(dist, (int, float)):
            continue
        by_voice.setdefault(voice, []).append({
            "timestamp": d.get("timestamp"),
            "distance": float(dist),
        })
    out: dict[str, list[dict]] = {}
    for voice, samples in by_voice.items():
        samples.sort(key=lambda s: s.get("timestamp") or "")
        out[voice] = samples[-per_voice:]
    return {"voices": out}


@router.get("/v1/puddle/deltas")
def get_deltas(
    tags_include: list[str] | None = None,
    tags_exclude: list[str] | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Delta-store-shaped query over the puddle.

    The Grand Loop viz issues `/deltas?tags_include=convo:grand` (and
    similar tag queries) against the experiment's delta-store. This
    endpoint exposes the same shape over the in-memory puddle so the
    viz can be lifted with near-zero adaptation. Returns the bare list
    of delta dicts the viz expects.
    """
    return puddle.query(
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        time_start=time_start,
        time_end=time_end,
        limit=limit,
    )


class EngageRequest(BaseModel):
    kind: str = "more"  # more | less | chat


@router.post("/v1/puddle/cards/{card_id}/engage")
async def engage_card(card_id: str, req: EngageRequest) -> dict:
    """Author an engagement delta pointing at the card.

    Engagement is its own shape — a `feed-engagement` delta with an
    `engages:<lake_id>` pointer to the card it modifies (matching the
    generalized engagement-as-delta vocabulary documented in
    docs/reference/feed-spec.md and reserved-tags-spec.md). The card
    itself is not duplicated. The dashboard renders the engagement
    state by projecting these markers onto their target cards: card
    + engagement = a card with state, never two cards.

    Dual-write to lake + puddle so the dashboard's puddle watcher sees
    the engagement immediately, without waiting for telepathy's next
    5-minute tick. The puddle copy carries lake-id + recalled-id back-
    references so telepathy correctly dedupes when it later mirrors
    the lake delta.
    """
    kind = req.kind.strip().lower()
    if kind not in ("more", "less", "chat"):
        kind = "more"

    card = puddle.get(card_id)
    if card is None:
        # Either expired, never existed, or already TTL'd. Returning 404
        # is design-correct — engaging on a card that's gone shouldn't
        # silently succeed.
        raise HTTPException(status_code=404, detail="card not in puddle (expired or unknown)")

    try:
        payload = json.loads(card.get("content") or "{}")
    except Exception:
        payload = {"body": card.get("content") or ""}

    # The card's lake-id is what we'll point at via `engages:`.
    # Fresh Phase-1 puddle writes carry `lake-id:<id>`; telepathy-
    # restored puddle items only carry `recalled-id:<id>` (which IS
    # the full lake id — store.new_id() emits 12-char ids and the
    # 24-char slice is effectively a no-op). Prefer lake-id when
    # present; fall back to recalled-id so engagement still works
    # post-cold-start.
    card_lake_id = ""
    fallback_lake_id = ""
    for t in card.get("tags") or []:
        if t.startswith("lake-id:"):
            card_lake_id = t.split(":", 1)[1]
            break
        if t.startswith("recalled-id:") and not fallback_lake_id:
            fallback_lake_id = t.split(":", 1)[1]
    card_lake_id = card_lake_id or fallback_lake_id

    # Engagement marker — short content for context, the full pointer
    # carried by the `engages:` tag. via:loop preserves the legacy
    # confidence-scoring/crystal-regen signal shape.
    marker_content = (payload.get("body") or "")[:200]
    marker_tags = [
        "feed-engagement",
        f"engagement:{kind}",
        "via:loop",
    ]
    if card_lake_id:
        marker_tags.append(f"engages:{card_lake_id}")
    marker = await delta_client.write(
        content=marker_content,
        tags=marker_tags,
        source="loop-engagement",
    )
    marker_id = (
        marker.get("id")
        if isinstance(marker, dict)
        else (marker if isinstance(marker, str) else "")
    )
    puddle_marker_tags = [CONVO_TAG, *marker_tags]
    if marker_id:
        puddle_marker_tags.append(f"lake-id:{marker_id}")
        puddle_marker_tags.append(f"recalled-id:{marker_id[:24]}")
    await puddle.write(
        content=marker_content,
        tags=puddle_marker_tags,
        source="loop-engagement",
        ttl_seconds=Q_A_TTL_S,
    )

    # Fire the thumb-threshold check in the background — if this push
    # tips the count to THUMB_REGEN_THRESHOLD since the last crystal,
    # regen_from_signal triggers immediately (no cooldown).
    try:
        import asyncio as _asyncio
        from . import feed_orient as _feed_orient
        _asyncio.create_task(_feed_orient.on_engagement_written())
    except Exception:
        pass

    return {
        "ok": True,
        "marker_id": marker_id,
        "engages": card_lake_id,
        "kind": kind,
    }


@router.get("/v1/puddle/_debug")
def debug_dump(limit: int = 50) -> dict:
    """Temporary: dump all alive deltas with tags for spike-mode debugging."""
    raw = puddle.query(limit=limit)
    return {
        "deltas": [
            {
                "id": d.get("id"),
                "tags": d.get("tags"),
                "source": d.get("source"),
                "timestamp": d.get("timestamp"),
                "expires_at": d.get("expires_at"),
                "content_preview": (d.get("content") or "")[:120],
            }
            for d in raw
        ],
    }


@router.get("/v1/puddle/stream")
async def stream() -> StreamingResponse:
    """Server-Sent Events — one event per puddle write.

    Used by the live viz and (eventually) the main dashboard view to
    push cards, voice thoughts, and process events without polling.
    """
    async def event_gen():
        async for delta in puddle.subscribe(maxsize=256):
            try:
                yield f"data: {json.dumps(delta)}\n\n"
            except Exception:
                # Defensive: if a delta carries something json.dumps can't
                # encode, skip rather than killing the stream for everyone.
                continue

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/v1/loop/helper-tasks")
async def get_helper_tasks() -> dict:
    """Active helper-channel tasks for the dashboard's status bar.

    Joins three lake reads — `task-spawn` (the kitty handshake delta),
    `task-complete` (claude's closure signal), and `assistant`/`user`
    hook deltas for each active session — into one row per live task.

    Returns:
      {"tasks": [
        {
          "corr": str,
          "title": str,            # first user prompt, truncated
          "host": str,
          "claude_session_id": str,
          "project": str,
          "spawn_iso": str,
          "last_message_at": str,  # most recent assistant delta ts
          "last_message_id": str,  # for click-to-scroll
        },
        ...
      ]}
    """
    spawns, completes, abandoned = await asyncio.gather(
        delta_client.query(tags_include=["task-spawn"], limit=200),
        delta_client.query(tags_include=["task-complete"], limit=200),
        delta_client.query(tags_include=["task-abandoned"], limit=200),
    )

    def _tag_value(tags: list[str], prefix: str) -> str:
        for t in tags:
            if t.startswith(prefix):
                return t[len(prefix):]
        return ""

    # Both `task-complete` (claude wrote a closure delta) and
    # `task-abandoned` (the kitty plugin noticed the window died with no
    # closure) are closure signals — either one clears the strip.
    completed: set[str] = set()
    for c in (*completes, *abandoned):
        corr = _tag_value(c.get("tags") or [], "task-corr:")
        if corr:
            completed.add(corr)

    active: list[dict] = []
    seen: set[str] = set()
    for s in spawns:
        tags = s.get("tags") or []
        corr = _tag_value(tags, "task-corr:")
        sid = _tag_value(tags, "helper-session:")
        if not corr or not sid or corr in completed or corr in seen:
            continue
        seen.add(corr)
        active.append({
            "corr": corr,
            "claude_session_id": sid,
            "host": _tag_value(tags, "host:"),
            "project": _tag_value(tags, "project:"),
            "spawn_iso": s.get("timestamp") or "",
        })

    if not active:
        return {"tasks": []}

    # Per-task lookups, parallelized. The first user-prompt of the
    # session gives the dashboard a human-readable title; the latest
    # assistant delta gives "last message X ago" + click-to-scroll.
    async def _enrich(row: dict) -> dict:
        sid = row["claude_session_id"]
        first_user, last_assistant = await asyncio.gather(
            delta_client.query(
                tags_include=["user", f"session:{sid}"],
                limit=1,
            ),
            delta_client.query(
                tags_include=["assistant", f"session:{sid}"],
                limit=50,
            ),
        )
        title_raw = ""
        if first_user:
            # `query` returns newest-first; the first user prompt of the
            # session is the OLDEST one. Re-sort to grab it.
            first_user.sort(key=lambda d: d.get("timestamp") or "")
            title_raw = (first_user[0].get("content") or "").strip()
        # Strip the kitty prompt header so the user sees the actual ask.
        title_raw = title_raw.replace(f"[fathom-task:{row['corr']}]", "").strip()
        title = title_raw[:80] + ("…" if len(title_raw) > 80 else "")

        last_message_at = ""
        last_message_id = ""
        if last_assistant:
            last_assistant.sort(key=lambda d: d.get("timestamp") or "")
            tail = last_assistant[-1]
            last_message_at = tail.get("timestamp") or ""
            last_message_id = tail.get("id") or ""

        return {
            **row,
            "title": title or "(no prompt yet)",
            "last_message_at": last_message_at,
            "last_message_id": last_message_id,
        }

    enriched = await asyncio.gather(*(_enrich(r) for r in active))
    enriched.sort(key=lambda r: r.get("spawn_iso") or "")
    return {"tasks": enriched}
