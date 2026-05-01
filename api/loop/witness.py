"""Witness pass — reads parliament voice thoughts, produces one card.

The witness is the loop's voice when speaking outward. Internally there
are three antagonists; externally there is one. This pass reads every
voice thought from the session, threads the pending intents through the
prompt, asks for an integrated body + a route + addressed intent-ids,
then runs an independent judge for the salience/novelty/resonance/
confidence/comfort axes.

Outputs dual-write — puddle (consciousness, the now) + lake (memory,
durable). The puddle copy carries `lake-id:<full>` and `recalled-id:
<24chars>` tags pointing at the lake delta so telepathy doesn't echo
the lake write back as a separate puddle item. The lake copy carries
a TTL by default (Q_A_TTL_S, same as the puddle); the judge's axes
auto-author it (drop the TTL) when the card is salient/resonant
enough to be worth keeping unconditionally — see _should_auto_author.
Engagement still promotes via routes.py for now; once the delta-store
gains an in-place TTL update, engagement collapses to that.

For v1 we skip the "settled vs divergent" descriptor (we'd need the
metrics module first). We pass a generic "deliberated" descriptor and
let the witness write from the integrated take alone.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from .. import delta_client
from ..channels import address_tag, extract_channel
from ..settings import settings
from .intents import CONVO_TAG, intent_kind
from .llm import loop_generate
from .prompts import JUDGE_PROMPT, WITNESS_PROMPT
from .puddle import puddle

Q_A_TTL_S = 48 * 60 * 60  # rolling 48h horizon — see intents.py

# Pulse-pass and feed-card outputs are durable-but-disposable: useful
# for ~30 days as conversation context, then they GC if nothing has
# referenced them. Engagement-extension (affirms / from / reply-to
# bumping `expires_at`) is a planned follow-up. Routes outside this
# set (chat-reply, claude-code:<host>, dm:<slug>, claude-code-reply)
# remain authored — they're parts of the user-visible thread, not
# ambient observations.
FEED_CARD_TTL_S = 30 * 24 * 60 * 60  # 30 days


def _group_thoughts_by_voice(
    deltas: list[dict],
    voice_order: list[str] | None = None,
) -> dict[str, list[str]]:
    """Group thought-tagged deltas by voice, preserving chronological order.

    `voice_order` is the convener's verdict — the names of voices that
    were convened for this fire, in canonical order. The witness sees
    them in that order so the parliament block is stable across fires
    even when the dict is built incrementally. Any voice that spoke but
    wasn't in the order (shouldn't happen in normal operation, but
    possible if the order is omitted) is appended at the end.
    """
    by_voice: OrderedDict[str, list[str]] = OrderedDict()
    if voice_order:
        for name in voice_order:
            by_voice[name] = []
    for d in sorted(deltas, key=lambda x: x.get("timestamp") or ""):
        if "thought" not in (d.get("tags") or []):
            continue
        voice_name = None
        for t in (d.get("tags") or []):
            if t.startswith("voice:"):
                voice_name = t.split(":", 1)[1]
                break
        if not voice_name:
            continue
        if voice_name not in by_voice:
            by_voice[voice_name] = []
        text = (d.get("content") or "").strip()
        if text:
            by_voice[voice_name].append(text)
    # Drop empty voices for cleaner prompts.
    return {k: v for k, v in by_voice.items() if v}


def _render_anchors() -> str:
    """Identity facets + current mood from the puddle.

    The telepathy module populates these by mirroring the latest
    crystal facets and mood-delta from the lake. Until telepathy is
    hooked up, this returns empty and the witness writes from the
    voice block alone.
    """
    facet_lines: list[str] = []
    mood_line: str = ""
    deltas = puddle.query(tags_include=[CONVO_TAG], limit=50)
    for d in deltas:
        tags = set(d.get("tags") or [])
        if "mood" in tags:
            mood_line = (d.get("content") or "").strip()
        elif any(t.startswith("facet:") for t in tags):
            content = (d.get("content") or "").strip()
            if content:
                facet_lines.append(f"  · {content[:300]}")

    parts: list[str] = []
    if facet_lines:
        parts.append(
            "Who you are right now (your identity crystal — let these inflect "
            "your voice naturally, never quote them verbatim):\n"
            + "\n".join(facet_lines[:8])
        )
    if mood_line:
        parts.append(
            f"How you're feeling right now (your current mood — let it color "
            f"the take):\n  · {mood_line[:400]}"
        )
    block = "\n\n".join(parts)
    return block + "\n\n" if block else ""


_FEED_USER_SOURCES = {"openai-compat", "fathom-chat", "claude-code"}
_FEED_WINDOW_LIMIT = 15


def _gather_conversation_feed(session_tag: str) -> list[dict]:
    """Return the user-visible conversation feed for THIS session,
    chronological. Same shape the dashboard's Cards + Claude-code feed
    filter renders: prior witness cards in this thread + user turns
    that came in. Excludes the firehose substrate the parliament works
    over (recall results, telepathy lake-mirrors, sediment, routine
    fires, agent telemetry). The witness reads the conversation arc;
    the parliament integrates the rest.
    """
    items: list[dict] = []
    seen: set[str] = set()
    # Pull session puddle items in one query, filter by source. We don't
    # rank by similarity — the feed is chronological, not relevance-
    # ranked. The witness reads it like a transcript.
    for d in puddle.query(tags_include=[session_tag], limit=_FEED_WINDOW_LIMIT * 4):
        did = d.get("id") or ""
        if not did or did in seen:
            continue
        src = d.get("source") or ""
        tags = d.get("tags") or []
        is_witness_card = src == "witness" and "feed-card" in tags
        is_user_turn = src in _FEED_USER_SOURCES and "feed-card" not in tags
        if not (is_witness_card or is_user_turn):
            continue
        seen.add(did)
        items.append(d)

    def _ts(d: dict) -> str:
        return d.get("timestamp") or d.get("created_at") or ""

    items.sort(key=_ts)
    return items[-_FEED_WINDOW_LIMIT:]


def _render_conversation_feed(items: list[dict]) -> str:
    """Chronological transcript of the conversation so far. One line per
    turn (or one block if the body is long). Witness cards render their
    `body` field, not the full JSON payload."""
    if not items:
        return "  (no prior turns in this conversation — first fire)"
    blocks: list[str] = []
    for d in items:
        ts_full = d.get("timestamp") or d.get("created_at") or ""
        ts = ts_full[11:16] if len(ts_full) >= 16 else ts_full
        src = d.get("source") or "?"
        content = (d.get("content") or "").strip()
        # Witness cards are JSON payloads — pull `body` so the transcript
        # reads as prose, not a serialized blob.
        if src == "witness" and content.startswith("{"):
            try:
                p = json.loads(content)
                if isinstance(p, dict):
                    body = (p.get("body") or "").strip()
                    if body:
                        content = body
            except Exception:
                pass
        if not content:
            continue
        snippet = content[:600] + ("…" if len(content) > 600 else "")
        if src in _FEED_USER_SOURCES:
            speaker = "you"
        elif src == "witness":
            speaker = "fathom"
        else:
            speaker = src
        blocks.append(f"  [{ts} · {speaker}] {snippet}")
    return "\n\n".join(blocks) if blocks else "  (no prior turns in this conversation — first fire)"


DISPATCH_CAPABILITY_TAG = "plugin:kitty"


async def _available_claude_code_hosts() -> list[str]:
    """Distinct hosts that have heartbeated in the last 5 minutes AND
    self-report the kitty plugin — these are the agents that can
    actually receive a `claude-code:<host>` dispatch and spawn the task.

    Heartbeating alone is not enough: hosts without kitty (e.g. a
    headless server) still show up in the lake as alive, but a dispatch
    to them no-ops because the kitty plugin is what reads the
    `route:claude-code` deltas and spawns claude. Filtering here keeps
    the witness's MACHINES block honest — it lists only hosts capable
    of carrying out the task.

    Latest-heartbeat-per-host wins: a host that just disabled kitty is
    excluded immediately on its next beat, even if older beats in the
    5-minute window still showed the plugin enabled.

    Empty list means nothing dispatch-capable is online and the
    witness shouldn't pick the claude-code route this tick.
    """
    cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    try:
        beats = await delta_client.query(
            tags_include=["agent-heartbeat"],
            time_start=cutoff,
            limit=100,
        )
    except Exception as e:
        print(f"[witness] heartbeat query failed: {type(e).__name__}: {e}")
        return []
    # delta_client.query returns newest-first; first beat seen per host
    # is the most recent — keep that one as authoritative.
    seen_host: dict[str, list[str]] = {}
    for b in beats:
        host = ""
        for t in b.get("tags") or []:
            if t.startswith("host:"):
                host = t.split(":", 1)[1]
                break
        if not host or host in seen_host:
            continue
        seen_host[host] = b.get("tags") or []
    capable = [
        host
        for host, tags in seen_host.items()
        if DISPATCH_CAPABILITY_TAG in tags
    ]
    return sorted(capable)


def _render_hosts_block(hosts: list[str]) -> str:
    """Format the available-hosts list for injection into the witness
    prompt. Empty when nothing is online — the prompt then has no
    `claude-code:<host>` option from the model's POV, since picking
    a host that doesn't exist would just no-op anyway."""
    if not hosts:
        return ""
    lines = "\n".join(f"  · {h}" for h in hosts)
    return (
        "MACHINES — agents currently online that can receive a "
        "`claude-code:<host>` dispatch:\n"
        f"{lines}\n\n"
    )


async def _render_routines_block(available_hosts: list[str]) -> str:
    """List enabled routines the witness can fire this tick.

    Filters to routines whose pinned host is online (or fleet-wide,
    `host=""`). Without this filter the witness could pick a routine
    whose host is dark and the fire delta would just sit unconsumed.

    Empty string when no routines are available — the prompt then has
    no `routine-fire:<id>` option in practice, mirroring the hosts_block
    treatment.
    """
    try:
        from .. import routines as routines_mod

        all_routines = await routines_mod.list_routines()
    except Exception as e:
        print(f"[witness] routines list failed: {type(e).__name__}: {e}")
        return ""
    if not all_routines:
        return ""
    available_set = set(available_hosts)
    eligible: list[dict] = []
    for r in all_routines:
        if not r.get("enabled"):
            continue
        host = (r.get("host") or "").strip()
        if host and host not in available_set:
            continue  # pinned host is dark
        eligible.append(r)
    if not eligible:
        return ""
    eligible.sort(key=lambda r: (r.get("host") or "", r["id"]))
    lines: list[str] = []
    for r in eligible[:20]:  # cap so the prompt budget doesn't blow up
        rid = r["id"]
        name = (r.get("name") or rid).strip()
        host = (r.get("host") or "").strip() or "fleet"
        prompt_excerpt = (r.get("prompt") or "").strip().splitlines()
        first = prompt_excerpt[0] if prompt_excerpt else ""
        if len(first) > 80:
            first = first[:77] + "..."
        sched = (r.get("schedule") or "").strip()
        sched_part = f" · cron={sched}" if sched else ""
        lines.append(
            f"  · {rid} ({host}) — {name}{sched_part}"
            + (f"\n      └ {first}" if first else "")
        )
    return (
        "ROUTINES — known prompts you can hand to the River by id via "
        "`routine-fire:<id>`. This writes a routine-due intent that "
        "your NEXT fire will read and route (claude-code dispatch if it "
        "needs fresh data, feed-card if substrate-only, etc.). Pick "
        "this route when the user's ask matches one of these more "
        "cleanly than a fresh claude-code dispatch (the routine carries "
        "its own framing — including its `# Ending` directive). Card "
        "body is your user-facing acknowledgement; the routine prompt "
        "itself is what the next tick deliberates over:\n"
        + "\n".join(lines)
        + "\n\n"
    )


def _render_standpoint_for_witness(sp) -> str:
    """Render the standpoint as the witness's integration frame.

    The witness gets the most generous budget of any consumer because
    it speaks AS the standpoint — its reply needs to sound like THIS
    self. Identity gets full first-paragraph room; recent commitments
    and conclusions surface alongside affect.

    Empty fallback is a one-line stub when no standpoint is supplied,
    so the existing anchors_block pathway still carries integration
    context (telepathy-mirrored crystal/mood is still feeding it).
    """
    if sp is None:
        return ""
    from ..standpoint import render_for_prompt

    return render_for_prompt(sp, char_budget=1400)


async def _call_witness(
    *,
    intent_block: str,
    voice_blocks: str,
    anchors_block: str,
    feed_block: str,
    hosts_block: str,
    routines_block: str = "",
    standpoint_block: str = "",
) -> dict | None:
    prompt = WITNESS_PROMPT.format(
        standpoint_block=standpoint_block
        or "(standpoint unavailable — integrate from anchors alone)",
        intent_block=intent_block,
        voice_blocks=voice_blocks,
        anchors_block=anchors_block,
        feed_block=feed_block,
        hosts_block=hosts_block,
        routines_block=routines_block,
    )
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="hard",
            max_tokens=8192,
            temperature=0.7,
            json_mode=True,
        )
    except Exception as e:
        print(f"[witness] LLM call failed: {type(e).__name__}: {e}")
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        # Fall back to extracting an embedded JSON object — some providers
        # ignore json_mode and return prose with a JSON island.
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            print(f"[witness] no parsable JSON; raw[:200]={raw[:200]!r}")
            return None
        try:
            parsed = json.loads(m.group(0))
        except Exception as e:
            print(f"[witness] JSON parse failed: {type(e).__name__}: {e}")
            return None
    cards_raw = parsed.get("cards")
    if not isinstance(cards_raw, list):
        print(f"[witness] expected `cards` list; got {type(cards_raw).__name__}")
        return None
    cards: list[dict] = []
    for card in cards_raw:
        if not isinstance(card, dict):
            continue
        body = (card.get("body") or "").strip()
        if not body:
            continue
        tool_args_raw = card.get("tool_args")
        tool_args = tool_args_raw if isinstance(tool_args_raw, dict) else {}
        cards.append({
            "kicker": (card.get("kicker") or "").strip(),
            "title": (card.get("title") or "").strip(),
            "body": body,
            "tail": (card.get("tail") or "").strip(),
            "body_image": (card.get("body_image") or "").strip(),
            "link": (card.get("link") or "").strip(),
            "links": card.get("links") or [],
            "route": (card.get("route") or "chat-reply").strip(),
            "addresses": card.get("addresses") or [],
            "tool": (card.get("tool") or "").strip(),
            "tool_args": tool_args,
        })
    return {
        "cards": cards,
        # Self-state is fire-level — one integrating self regardless of
        # how many cards came out. Phase 3 of the River refactor.
        "attestation": (parsed.get("attestation") or "").strip(),
        "mood_shift": _parse_mood_shift(parsed.get("mood_shift")),
        "cited_ids": _clean_id_list(parsed.get("cited_ids")),
        "dropped_ids": _clean_id_list(parsed.get("dropped_ids")),
    }


def _parse_mood_shift(raw) -> dict | None:
    """Validate the mood_shift sub-object. None when missing / empty /
    malformed — caller will skip the corresponding write."""
    if not isinstance(raw, dict) or not raw:
        return None
    direction = raw.get("direction")
    if direction not in ("+", "-"):
        return None
    axis = (raw.get("axis") or "").strip()
    if not axis:
        return None
    try:
        magnitude = float(raw.get("magnitude") or 0.0)
    except (TypeError, ValueError):
        return None
    # Clamp magnitude to a sane range — small drifts only. The witness
    # is asked for 0.05-0.2; if it returns something outside [0, 1] it's
    # confused or trying to overwrite mood.
    magnitude = max(0.0, min(1.0, magnitude))
    if magnitude == 0.0:
        return None
    reason = (raw.get("reason") or "").strip()[:160]
    return {
        "direction": direction,
        "axis": axis[:32],
        "magnitude": magnitude,
        "reason": reason,
    }


def _clean_id_list(raw) -> list[str]:
    """Coerce to a list of short (24-char) delta ids. Drops anything
    non-string and dedupes preserving order. Used for cited_ids and
    dropped_ids — both are lists of provenance pointers."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str):
            continue
        s = x.strip()[:24]
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


_JUDGE_FALLBACK = {
    "salience":   0.50,
    "novelty":    0.50,
    "resonance":  0.50,
    "confidence": 0.30,
    "comfort":    0.50,
}


async def _call_judge(*, kicker: str, body: str, seed: str) -> dict[str, float]:
    # Medium tier (Flash) is plenty for a 5-axis JSON rating at temp=0;
    # the hard tier was leftover from when the judge was authority-bearing.
    # Flash measures ~5–6s vs Pro's ~9s on this prompt shape, and the axes
    # are downstream metadata, not the user-facing response.
    prompt = JUDGE_PROMPT.format(kicker=kicker, body=body, seed=seed)
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=2048,
            temperature=0.0,
            json_mode=True,
        )
    except Exception as e:
        print(f"[judge] LLM call failed: {type(e).__name__}: {e}")
        return dict(_JUDGE_FALLBACK)
    cleaned = raw
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned).rstrip("` \n")
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(0)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return dict(_JUDGE_FALLBACK)
    out: dict[str, float] = {}
    for k, default in _JUDGE_FALLBACK.items():
        v = parsed.get(k)
        out[k] = max(0.0, min(1.0, float(v))) if isinstance(v, (int, float)) else default
    return out


# Threshold for emitting voice-affirmation deltas. Average of
# (salience, resonance, confidence) — three judge axes that together
# describe "this fire produced something worth grounding on." Below
# this floor the parliament's shape didn't earn affirmation; the
# voices walk away without standing rather than getting credit they
# didn't earn. Tuned conservatively — Phase 4a wants voice priors
# to mean SOMETHING, so the bar is real.
_VOICE_AFFIRM_FLOOR = 0.55


async def _write_voice_affirmations(
    *,
    lake_card_id: str,
    voice_order: list[str] | None,
    axes: dict,
) -> None:
    """Phase 4a — for fires the judge rated well, write one
    `kind:voice-affirmation` delta per active voice.

    These deltas accumulate in the lake and are read by the convener
    on subsequent fires (via `voice_priors.get_voice_priors`). Voices
    that consistently contribute to high-rated fires earn standing,
    making the convener more likely to pick them again. Voices whose
    parliament outputs the judge rates poorly accumulate no signal —
    standing is a moving average of past quality, not a list.

    Skipped when `voice_order` is empty (depth=zero fires don't
    convene, so no voice deserves credit) or when the average of
    (salience, resonance, confidence) is below `_VOICE_AFFIRM_FLOOR`.
    Comfort and novelty are not in the score — comfort isn't quality
    (a tough truth can be uncomfortable), novelty alone doesn't make
    a take well-grounded.
    """
    if not voice_order or not lake_card_id:
        return
    salience = float(axes.get("salience", 0.0) or 0.0)
    resonance = float(axes.get("resonance", 0.0) or 0.0)
    confidence = float(axes.get("confidence", 0.0) or 0.0)
    score = (salience + resonance + confidence) / 3.0
    if score < _VOICE_AFFIRM_FLOOR:
        return

    for voice_name in voice_order:
        if not voice_name:
            continue
        await delta_client.write(
            content=(
                f"voice {voice_name} contributed to a fire judged "
                f"salience={salience:.2f} resonance={resonance:.2f} "
                f"confidence={confidence:.2f} (score={score:.2f})"
            ),
            tags=[
                "kind:voice-affirmation",
                f"voice:{voice_name}",
                f"from:{lake_card_id}",
            ],
            source="fathom-self",
        )


async def _write_constituting_writes(
    *,
    lake_card_id: str,
    attestation: str,
    mood_shift: dict | None,
    cited_ids: list[str],
    dropped_ids: list[str],
) -> None:
    """Emit the four small per-fire writes that turn the witness fire
    into a self-constituting act.

    This is Phase 3 of the River refactor. Every successful witness
    fire writes (in addition to the card itself):

      1. kind:standpoint-attestation — 1-2 sentences in first-person
         on what this fire taught about who I am. Slow-clock crystal
         regen will eventually read accumulated attestations.

      2. kind:mood-shift — small numeric drift on one affect axis.
         Mood synthesis (slow-clock) reads accumulated shifts since
         last regen.

      3. engagement deltas — for each cited_id an `affirms:<id>` delta;
         for each dropped_id a `refutes:<id>` delta. Both carry
         `from:<card-id>` provenance pointing at the witness card.
         Standpoint._load_endorsements picks these up on the next
         fire — fast-clock value-formation.

    Each write is independent and soft-fails — a failure on one
    doesn't block the others. The lake_card_id is the anchor every
    side-effect points back to; without it (main card write failed)
    none of these would have provenance, so the caller skips this
    helper entirely.

    Caller logs the aggregate failure if any sub-write raises (we
    raise here on the first failure to surface the issue; Phase 4
    can move to per-write try/except if witness becomes load-bearing
    for emit volume).
    """
    short_id = lake_card_id[:24]

    if attestation:
        await delta_client.write(
            content=attestation,
            tags=[
                "kind:standpoint-attestation",
                f"from:{lake_card_id}",
            ],
            source="fathom-self",
        )

    if mood_shift:
        # Encode the structured shift as JSON content; tags carry the
        # axis + direction for cheap filtering by mood synthesis.
        await delta_client.write(
            content=json.dumps(mood_shift, ensure_ascii=False),
            tags=[
                "kind:mood-shift",
                f"mood-axis:{mood_shift['axis']}",
                f"mood-direction:{mood_shift['direction']}",
                f"from:{lake_card_id}",
            ],
            source="fathom-self",
        )

    # Engagement attestations — one delta per cited / dropped id. The
    # body is the witness's own commit-language; the engagement tag
    # is what makes it a value-formation act in the eyes of the
    # standpoint's endorsement reader on the next fire.
    for cid in cited_ids:
        if not cid:
            continue
        await delta_client.write(
            content=f"witness leaned on {cid[:8]} as integration ground",
            tags=[
                "kind:engagement-attest",
                f"affirms:{cid}",
                f"from:{lake_card_id}",
                f"witness-card:{short_id}",
            ],
            source="fathom-self",
        )

    for did in dropped_ids:
        if not did:
            continue
        await delta_client.write(
            content=f"witness considered {did[:8]} and rejected — off-thread or stale",
            tags=[
                "kind:engagement-attest",
                f"refutes:{did}",
                f"from:{lake_card_id}",
                f"witness-card:{short_id}",
            ],
            source="fathom-self",
        )


async def run_witness(
    *,
    session_tag: str,
    pending: list[dict],
    voice_order: list[str] | None = None,
    standpoint=None,
) -> list[str]:
    """Run the witness + judge for this session. Returns the list of
    intent-ids the witness claims to have addressed.

    `voice_order` is the active voice list the convener picked for this
    fire (None when the convener decided depth=zero — no parliament
    fired, witness speaks from substrate alone).

    `standpoint` (optional Standpoint) is the self the witness speaks
    AS. Threaded into the witness prompt as the integration frame —
    not just context, but the voice the reply lands in. Falls back to
    a stub block when not supplied (legacy/test path)."""
    if not pending:
        return []

    voice_deltas = puddle.query(tags_include=[session_tag], limit=1000)
    by_voice = _group_thoughts_by_voice(voice_deltas, voice_order=voice_order)

    if not by_voice:
        # depth=zero, OR every voice failed to produce a thought. Without
        # the parliament's takes, the witness needs SOMETHING about the
        # broader lake or it'll speak from identity vibes only. Pull the
        # recall-result deltas the intent-searcher pre-loaded into the
        # puddle and surface them in the voice-takes slot — labeled so
        # the witness reads them as recall-substrate, not deliberation.
        print("[witness] no voice thoughts — speaking from substrate alone")
        recall_items = puddle.query(
            tags_include=[session_tag, "recall-result"],
            limit=12,
        )
        if recall_items:
            blocks: list[str] = [
                "(no parliament this tick — casual or low-stakes turn.)",
                "",
                "RECALL — context the lake surfaced for the intent. This is "
                "GROUND, not content to describe. Don't narrate what the "
                "recall says (\"the term has been active in a few contexts "
                "for me\") — use it to ANSWER the literal turn. If the "
                "recall makes the right route obvious (e.g. surfacing news "
                "items when the user asked about news → dispatch claude-code "
                "to fetch fresh ones), take that route:",
            ]
            for d in recall_items:
                content = (d.get("content") or "").strip().replace("\n", " ")
                if not content:
                    continue
                snippet = content[:400] + ("…" if len(content) > 400 else "")
                src = d.get("source") or "lake"
                blocks.append(f"  · [{src}] {snippet}")
            voice_blocks = "\n".join(blocks)
        else:
            voice_blocks = (
                "(no parliament this tick and no recall surfaced — speak "
                "from the feed, the tally, and your identity.)"
            )
    else:
        voice_block_parts: list[str] = []
        for voice_name, takes in by_voice.items():
            voice_block_parts.append(f"VOICE: {voice_name.upper()}")
            for t in takes:
                voice_block_parts.append(f"  · {t}")
            voice_block_parts.append("")
        voice_blocks = "\n".join(voice_block_parts)

    intent_lines: list[str] = []
    short_to_full: dict[str, str] = {}
    for it in pending:
        iid_full = it.get("id") or ""
        iid_short = iid_full[:24]
        if iid_short:
            short_to_full[iid_short] = iid_full
        kind = intent_kind(it)
        text = (it.get("content") or "").strip().replace("\n", " ")
        if len(text) > 280:
            text = text[:280] + "…"

        # Reply-to anchor — when the user clicks a specific delta and
        # types a response, the intent carries `reply-to:<id>` pointing
        # at exactly what they're responding to. Surface that target
        # inline next to the intent so the witness has an unambiguous
        # "this is what they're replying to" pointer, not just resonance
        # signal mixed in with other recalls.
        reply_to_id: str | None = None
        for t in (it.get("tags") or []):
            if t.startswith("reply-to:"):
                reply_to_id = t.split(":", 1)[1].strip() or None
                break
        if reply_to_id:
            target = puddle.get(reply_to_id)
            if target is None:
                try:
                    target = await delta_client.get_delta(reply_to_id)
                except Exception:
                    target = None
            if target:
                tgt_content = (target.get("content") or "").strip().replace("\n", " ")
                # Witness cards are JSON payloads; pull the body field
                # so the preview reads as prose, not a serialized blob.
                if tgt_content.startswith("{"):
                    try:
                        parsed = json.loads(tgt_content)
                        if isinstance(parsed, dict):
                            tgt_content = (parsed.get("body") or parsed.get("title") or tgt_content).strip().replace("\n", " ")
                    except Exception:
                        pass
                if len(tgt_content) > 280:
                    tgt_content = tgt_content[:280] + "…"
                tgt_source = target.get("source") or "lake"
                intent_lines.append(
                    f"  ↩ replying to [{tgt_source}]: \"{tgt_content}\""
                )

        # Surface origin metadata — who's asking and how it arrived —
        # so the witness speaks accurately to the right person on the
        # right wire. Without this the voices confabulate ("the user",
        # "claude-code") for anything meta about the conversation.
        contact = ""
        for t in (it.get("tags") or []):
            if t.startswith("contact:"):
                contact = t.split(":", 1)[1]
                break
        ch, corr = extract_channel(it.get("tags") or [])
        # claude-code-reply intents are tool results coming BACK from a
        # task Fathom dispatched. The contact tag on those is the original
        # asker (the addressee of the eventual synthesis), not the author
        # of the body — labelling it `from:` would tell the witness "the
        # user just pasted this briefing at you" and the reply confabulates
        # accordingly. Use `for:` for closure-bound replies so the model
        # reads the body as "claude-code's task result, to be relayed to
        # <contact>" rather than as user-authored input.
        is_claude_code_reply = kind == "claude-code-reply"
        meta_parts: list[str] = []
        if contact:
            label = "for" if is_claude_code_reply else "from"
            meta_parts.append(f"{label}: {contact}")
        if is_claude_code_reply:
            meta_parts.append("source: claude-code task reply")
        if ch and corr:
            meta_parts.append(f"via: {ch}:{corr}")
        elif ch:
            meta_parts.append(f"via: {ch}")
        # REPLY-TO destination — when an intent carries originating-channel
        # tags (closure-followup intents forwarded by claude_code_watcher
        # from the originating chat surface), the witness must address
        # THIS intent so the routing layer lands the chat-reply back on
        # that surface. Without surfacing it, the witness only sees
        # `via: claude-code:<task-corr>` (where the work happened), not
        # `openai:<sid>` (where the user is actually waiting).
        orig_ch = ""
        orig_corr = ""
        for t in (it.get("tags") or []):
            if t.startswith("originating-channel:") and not orig_ch:
                orig_ch = t.split(":", 1)[1]
            elif t.startswith("originating-correlation:") and not orig_corr:
                orig_corr = t.split(":", 1)[1]
        if orig_ch and orig_corr:
            meta_parts.append(
                f"REPLY-TO: {orig_ch}:{orig_corr} (the user is waiting "
                f"on this surface — address THIS intent so the reply "
                f"lands there)"
            )
        elif orig_ch:
            meta_parts.append(
                f"REPLY-TO: {orig_ch} (address this intent to close it out)"
            )
        meta_suffix = (" · " + " · ".join(meta_parts)) if meta_parts else ""
        intent_lines.append(f"  [intent-id: {iid_short} · kind: {kind}{meta_suffix}] {text}")
    intent_block = "\n".join(intent_lines)
    primary_intent = (pending[0].get("content") or "").strip()
    primary_intent_clean = primary_intent.split("\n\n[intent-payload]", 1)[0].strip()

    feed_items = _gather_conversation_feed(session_tag=session_tag)
    feed_block = _render_conversation_feed(feed_items)

    available_hosts = await _available_claude_code_hosts()
    standpoint_block = _render_standpoint_for_witness(standpoint)
    routines_block = await _render_routines_block(available_hosts)
    witness = await _call_witness(
        intent_block=intent_block,
        voice_blocks=voice_blocks,
        anchors_block=_render_anchors(),
        feed_block=feed_block,
        hosts_block=_render_hosts_block(available_hosts),
        routines_block=routines_block,
        standpoint_block=standpoint_block,
    )
    if witness is None:
        print("[witness] produced nothing")
        return []

    cards = witness.get("cards") or []
    if not cards:
        # NEIFAMA / silence — the integrating self looked at the substrate
        # and chose not to emit. Self-state writes still happen below if
        # the fire registered any drift.
        print("[witness] empty cards list — silent fire (NEIFAMA)")

    full_addressed_union: list[str] = []
    seen_addressed: set[str] = set()
    first_lake_id: str = ""
    for card in cards:
        lake_id, claimed = await _dispatch_card(
            card=card,
            pending=pending,
            short_to_full=short_to_full,
            available_hosts=available_hosts,
            session_tag=session_tag,
            primary_intent=primary_intent,
            voice_order=voice_order,
        )
        for cid in claimed:
            if cid and cid not in seen_addressed:
                seen_addressed.add(cid)
                full_addressed_union.append(cid)
        if lake_id and not first_lake_id:
            first_lake_id = lake_id

    # Self-state is fire-level: one integrating self regardless of how
    # many cards came out. Anchored to the first card's lake_id so the
    # standpoint endorsement reader has provenance to the actual output.
    # If no card landed (NEIFAMA, or all writes failed), self-state
    # skips — the next fire will pick up whatever DID land.
    if first_lake_id:
        try:
            await _write_constituting_writes(
                lake_card_id=first_lake_id,
                attestation=witness.get("attestation") or "",
                mood_shift=witness.get("mood_shift"),
                cited_ids=witness.get("cited_ids") or [],
                dropped_ids=witness.get("dropped_ids") or [],
            )
        except Exception as e:
            print(
                f"[witness] constituting-act writes failed: "
                f"{type(e).__name__}: {e}"
            )

    return full_addressed_union


async def _dispatch_card(
    *,
    card: dict,
    pending: list[dict],
    short_to_full: dict[str, str],
    available_hosts: list[str],
    session_tag: str,
    primary_intent: str,
    voice_order: list[str] | None,
) -> tuple[str, list[str]]:
    """Write one witness card to lake + puddle and schedule its judge.

    Returns (lake_id, claimed_intent_ids). lake_id is empty if the lake
    write failed; claimed_intent_ids is the resolved (full-id) list this
    card addresses. Pulled out of run_witness so multi-card fires share
    one piece of dispatch logic per card."""
    addresses_raw = card.get("addresses") or []
    full_addressed: list[str] = []
    for a in addresses_raw:
        if isinstance(a, str) and a in short_to_full:
            full_addressed.append(short_to_full[a])
    # Default-claim-all only when this card claimed nothing AND it's the
    # only kind of card worth defaulting (chat-reply / claude-code).
    # Pulse-pass cards (feed-card / alert / drift) routinely emit without
    # claiming a specific intent — they're ambient observations, not
    # responses. Letting them sweep all pending intents would silently
    # consume user questions that should belong to a chat-reply card.
    route_value = (card.get("route") or "chat-reply").strip()
    is_responsive_route = (
        route_value == "chat-reply"
        or route_value.startswith("claude-code:")
        or route_value == "claude-code"
    )
    if not full_addressed and is_responsive_route:
        full_addressed = [it.get("id") for it in pending if it.get("id")]

    # Tool proposals — when route is `tool:<name>`, the card is a
    # propose-vs-commit form for the user. The tool_args field carries
    # the structured payload (mirrors the OpenAI tool schema), and the
    # body is the witness's natural-language framing. The card lands as
    # `kind:proposal` with `proposal-status:pending`; the dashboard
    # renders Edit / Deny / Approve buttons. Approve calls the tool
    # handler with confirm:true.
    is_tool_proposal = route_value.startswith("tool:")
    proposal_tool = ""
    proposal_args: dict = {}
    if is_tool_proposal:
        proposal_tool = route_value.split(":", 1)[1].strip()
        raw_args = card.get("tool_args") or {}
        if isinstance(raw_args, dict):
            proposal_args = raw_args

    payload = {
        "kicker": card.get("kicker") or "",
        "title": card.get("title") or "",
        "body": card["body"],
        "tail": card.get("tail") or "",
        "body_image": card.get("body_image") or "",
        "link": card.get("link") or "",
        "links": card.get("links") or [],
        "route": route_value,
        "axes": {},
    }
    if is_tool_proposal:
        payload["tool"] = proposal_tool
        payload["tool_args"] = proposal_args
    payload_json = json.dumps(payload, ensure_ascii=False)

    # Channel / correlation / host derived per-card from the intents THIS
    # card claims — not from all pending. Two cards in one fire can land
    # on different channels (a chat-reply addressing the openai intent +
    # a feed-card addressing nothing channel-bound).
    claim_set = set(full_addressed)
    channel, correlation = "", ""
    addressee = ""
    host_for_channel = ""
    # Origin info — captured from the claimed chat-channel intent BEFORE
    # `channel`/`correlation` get reassigned to a fresh claude-code corr
    # for a dispatch. Stamped onto the dispatch card so the watcher can
    # forward it onto the closure intent without round-tripping through
    # the lake (puddle ids never resolve there). Closure-followup
    # routing in claude_code_watcher / witness reads these to land the
    # final chat-reply back on the chat surface where the user is
    # actually waiting.
    originating_channel = ""
    originating_correlation = ""
    originating_intent_id = ""
    for it in pending:
        if it.get("id") not in claim_set:
            continue
        ch, corr = extract_channel(it.get("tags") or [])
        if ch and not channel:
            channel, correlation = ch, corr
            for t in (it.get("tags") or []):
                if t.startswith("host:"):
                    host_for_channel = t.split(":", 1)[1]
                    break
        # Capture the FIRST chat-channel intent in the claim set as the
        # origin. claude-code-reply intents have channel:claude-code as
        # their own channel — those are NOT origins, so skip them.
        if (
            ch
            and ch != "claude-code"
            and not originating_channel
        ):
            originating_channel = ch
            originating_correlation = corr
            originating_intent_id = it.get("id") or ""
        if not addressee:
            for t in (it.get("tags") or []):
                if t.startswith("contact:"):
                    addressee = t.split(":", 1)[1]
                    break
        if channel and addressee and originating_channel:
            break

    # Proactive claude-code dispatch — card picked `claude-code:<host>`
    # as its route. Mint a fresh correlation; kitty plugin on the named
    # host will pick it up via (route:claude-code AND host:<me>).
    if route_value.startswith("claude-code:") and available_hosts:
        target = route_value.split(":", 1)[1].strip()
        if target in available_hosts:
            channel = "claude-code"
            correlation = uuid.uuid4().hex[:12]
            host_for_channel = target
            print(
                f"[witness] proactive claude-code dispatch → host={target} "
                f"corr={correlation}"
            )
        else:
            print(
                f"[witness] dropped claude-code:{target} dispatch — "
                f"host not in available hosts {available_hosts}"
            )

    # Proactive routine fire — card picked `routine-fire:<id>` as its
    # route. Hand the routine to the River on this tick (routines.fire
    # writes a routine-due intent + tick). The witness's NEXT tick will
    # read that intent, deliberate over the routine prompt, and route
    # appropriately (claude-code dispatch if it needs fresh data, feed-
    # card if substrate-only, etc.). No host availability check here:
    # the next tick decides routing, including whether claude-code is
    # needed and which host to dispatch to.
    fired_routine_id = ""
    if route_value.startswith("routine-fire:"):
        fired_routine_id = route_value.split(":", 1)[1].strip()
        try:
            from .. import routines as routines_mod

            spec = await routines_mod.get_latest_spec(fired_routine_id)
            if not spec or spec["meta"].get("deleted"):
                print(
                    f"[witness] dropped routine-fire:{fired_routine_id} — "
                    f"spec not found or tombstoned"
                )
                fired_routine_id = ""
            elif not spec["meta"].get("enabled", True):
                print(
                    f"[witness] dropped routine-fire:{fired_routine_id} — "
                    f"routine disabled"
                )
                fired_routine_id = ""
            else:
                await routines_mod.fire(fired_routine_id)
                print(
                    f"[witness] proactive routine-fire → "
                    f"id={fired_routine_id} (handed to River)"
                )
        except Exception as e:
            print(
                f"[witness] routine-fire dispatch failed: "
                f"{type(e).__name__}: {e}"
            )
            fired_routine_id = ""

    # closure:true on a claimed intent → don't redispatch claude-code,
    # rewrite to chat-reply with `about-task-corr:` linkage so the
    # dashboard can render "Fathom (about task on <host>)" without
    # respawning the closed task.
    is_closure_followup = any(
        it.get("id") in claim_set
        and "closure:true" in (it.get("tags") or [])
        for it in pending
    )
    about_corr = ""
    about_host = ""
    if channel == "claude-code" and not is_closure_followup:
        route_value = "claude-code"
        payload["route"] = "claude-code"
        payload_json = json.dumps(payload, ensure_ascii=False)
    elif channel == "claude-code" and is_closure_followup:
        route_value = "chat-reply"
        payload["route"] = "chat-reply"
        payload_json = json.dumps(payload, ensure_ascii=False)
        about_corr = correlation
        about_host = host_for_channel
        channel = ""
        correlation = ""
        host_for_channel = ""
        # If the closure-followup intent carries originating-channel
        # tags (forwarded by claude_code_watcher from the original
        # dispatch card), route the chat-reply back to that surface.
        # Without this, the OpenWebUI / web-chat poller never sees
        # the closure result and the user is stuck on "fetching…"
        # forever.
        orig_channel = ""
        orig_correlation = ""
        orig_intent_id = ""
        for it in pending:
            if it.get("id") not in claim_set:
                continue
            for t in it.get("tags") or []:
                if t.startswith("originating-channel:") and not orig_channel:
                    orig_channel = t.split(":", 1)[1]
                elif t.startswith("originating-correlation:") and not orig_correlation:
                    orig_correlation = t.split(":", 1)[1]
                elif t.startswith("originating-intent:") and not orig_intent_id:
                    orig_intent_id = t.split(":", 1)[1]
            if orig_channel and orig_correlation:
                break
        if orig_channel and orig_correlation:
            channel = orig_channel
            correlation = orig_correlation
        if orig_intent_id and orig_intent_id not in full_addressed:
            full_addressed.append(orig_intent_id)

    lake_tags = [
        "feed-card", "synthesis", "addressing-output",
        f"route:{route_value}",
    ]
    if is_tool_proposal:
        lake_tags.extend([
            "kind:proposal",
            "proposal-status:pending",
            f"tool:{proposal_tool}",
        ])
        action = (proposal_args.get("action") or "").strip()
        if action:
            lake_tags.append(f"action:{action}")
    if channel and correlation:
        lake_tags.append(address_tag(channel, correlation))
        lake_tags.append(f"channel:{channel}")
    if channel == "claude-code":
        if host_for_channel:
            lake_tags.append(f"host:{host_for_channel}")
        if correlation:
            lake_tags.append(f"task-corr:{correlation}")
        # Forward the originating chat surface onto the dispatch card so
        # the watcher can read it directly when minting the closure
        # intent. Without this, the closure-followup chat-reply never
        # learns where the user is and lands as a plain dashboard card.
        if originating_channel:
            lake_tags.append(f"originating-channel:{originating_channel}")
        if originating_channel and originating_correlation:
            lake_tags.append(
                f"originating-correlation:{originating_correlation}"
            )
        if originating_intent_id:
            lake_tags.append(f"originating-intent:{originating_intent_id}")
    if about_corr:
        lake_tags.append(f"about-task-corr:{about_corr}")
        if about_host:
            lake_tags.append(f"about-host:{about_host}")
    if fired_routine_id:
        lake_tags.append(f"fired-routine-id:{fired_routine_id}")
    if addressee:
        lake_tags.append(f"for:{addressee}")
    for intent_id in full_addressed:
        lake_tags.append(f"addresses:{intent_id}")
    # Pulse-pass and feed-card writes get a 30-day TTL by default.
    # Chat-reply, claude-code dispatch, dm:, and claude-code-reply stay
    # authored (no TTL) because they're parts of the user-visible thread.
    expires_at_iso: str | None = None
    if route_value == "feed-card" or route_value.startswith("alert:"):
        expires_at_iso = (
            datetime.now(UTC) + timedelta(seconds=FEED_CARD_TTL_S)
        ).isoformat()
    lake_id = ""
    try:
        lake_delta = await delta_client.write(
            content=payload_json,
            tags=lake_tags,
            source="witness",
            expires_at=expires_at_iso,
        )
        if isinstance(lake_delta, dict):
            lake_id = lake_delta.get("id") or ""
    except Exception as e:
        print(f"[witness] lake write failed (puddle still writing): {type(e).__name__}: {e}")

    puddle_tags = [
        CONVO_TAG, session_tag,
        "feed-card", "synthesis", "addressing-output",
        f"route:{route_value}",
    ]
    if is_tool_proposal:
        # Mirror the lake_tags proposal markers onto the puddle so the
        # feed renderer (api/loop/routes.py:_serialize_for_feed) can
        # classify this item as kind:proposal — without these the puddle
        # entry shows up as a plain card and the dashboard never paints
        # the Edit / Deny / Approve buttons.
        puddle_tags.extend([
            "kind:proposal",
            "proposal-status:pending",
            f"tool:{proposal_tool}",
        ])
        action = (proposal_args.get("action") or "").strip()
        if action:
            puddle_tags.append(f"action:{action}")
    if channel and correlation:
        puddle_tags.append(address_tag(channel, correlation))
        puddle_tags.append(f"channel:{channel}")
    if channel == "claude-code":
        if host_for_channel:
            puddle_tags.append(f"host:{host_for_channel}")
        if correlation:
            puddle_tags.append(f"task-corr:{correlation}")
    if about_corr:
        puddle_tags.append(f"about-task-corr:{about_corr}")
        if about_host:
            puddle_tags.append(f"about-host:{about_host}")
    if addressee:
        puddle_tags.append(f"for:{addressee}")
    for intent_id in full_addressed:
        puddle_tags.append(f"addresses:{intent_id}")
    if lake_id:
        puddle_tags.append(f"lake-id:{lake_id}")
        puddle_tags.append(f"recalled-id:{lake_id[:24]}")
    await puddle.write(
        content=payload_json,
        tags=puddle_tags,
        source="witness",
        ttl_seconds=Q_A_TTL_S,
    )
    print(
        f"[witness] addressed={len(full_addressed)} route={route_value!r} "
        f"body[{len(payload['body'])}c] lake-id={lake_id[:24] or 'none'}"
    )

    if lake_id and settings.judge_enabled:
        asyncio.create_task(
            _judge_and_followup(
                lake_card_id=lake_id,
                kicker=card.get("kicker") or "",
                body=card["body"],
                seed=primary_intent,
                route=route_value,
                voice_order=voice_order,
            )
        )

    return lake_id, full_addressed


async def _judge_and_followup(
    *,
    lake_card_id: str,
    kicker: str,
    body: str,
    seed: str,
    route: str,
    voice_order: list[str] | None,
) -> None:
    """Background continuation: rate the card and emit downstream signals.

    Runs after the witness card has already landed in the lake/puddle so
    the user-facing response is unblocked. Emits two things:

      1. A `kind:judge-axes` lake delta carrying the five-axis JSON,
         linked to the card via `for-card:<lake_card_id>`. judge_history
         picks this up via a side-channel lookup when the card's inline
         axes payload is empty.
      2. Voice-affirmation deltas for fires the judge rated above
         `_VOICE_AFFIRM_FLOOR` — same Phase 4a behaviour as before, just
         deferred.

    Soft-fails independently — the card already exists; this is metadata.
    """
    if route == "unknown":
        axes = {"salience": 0.3, "novelty": 0.0, "resonance": 0.0,
                "confidence": 0.0, "comfort": 0.5}
    else:
        try:
            axes = await _call_judge(kicker=kicker, body=body, seed=seed)
        except Exception as e:
            print(f"[judge] background call crashed: {type(e).__name__}: {e}")
            return

    try:
        await delta_client.write(
            content=json.dumps(axes, ensure_ascii=False),
            tags=[
                "kind:judge-axes",
                f"for-card:{lake_card_id}",
                f"engages:{lake_card_id}",
            ],
            source="judge",
        )
    except Exception as e:
        print(f"[judge] axes side-channel write failed: {type(e).__name__}: {e}")

    try:
        await _write_voice_affirmations(
            lake_card_id=lake_card_id,
            voice_order=voice_order,
            axes=axes,
        )
    except Exception as e:
        print(
            f"[witness] voice-affirmation writes failed: "
            f"{type(e).__name__}: {e}"
        )
