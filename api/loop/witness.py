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

import json
import re
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from .. import delta_client
from ..channels import address_tag, extract_channel
from . import resonance
from .intents import CONVO_TAG, intent_kind
from .llm import loop_generate
from .prompts import JUDGE_PROMPT, WITNESS_PROMPT
from .puddle import puddle

Q_A_TTL_S = 48 * 60 * 60  # rolling 48h horizon — see intents.py


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


RESONANCE_BUDGET = 8


async def _gather_witness_resonance(
    session_tag: str,
    voice_blocks: str,
    intent_text: str,
) -> list[dict]:
    """Build the witness's resonance pool — puddle items most aligned
    with the parliament's collective take + the user's intent.

    Same architecture as voice substrate: candidate union of recall-
    results and lake-mirrors, ranked by similarity to a signal text.
    Witness signal is the integrated voice block (where the parliament
    has gone) plus the intent (the durable anchor) — what the witness
    is integrating, against the lake material that bears on it.
    """
    candidates: list[dict] = []
    seen_ids: set[str] = set()
    for d in puddle.query(
        tags_include=[session_tag, "recall-result"],
        limit=80,
    ):
        did = d.get("id") or ""
        if did and did not in seen_ids:
            candidates.append(d)
            seen_ids.add(did)
    for d in puddle.query(
        tags_include=[CONVO_TAG, "lake-delta"],
        limit=80,
    ):
        did = d.get("id") or ""
        if did and did not in seen_ids:
            candidates.append(d)
            seen_ids.add(did)
    if not candidates:
        return []

    signal = (intent_text or "").strip()
    if voice_blocks:
        signal = f"{signal}\n\n{voice_blocks}".strip()
    if not signal:
        return []

    return await resonance.rank(signal, candidates, top_k=RESONANCE_BUDGET)


def _render_resonance_block(items: list[dict]) -> str:
    if not items:
        return "  (no resonant material in the puddle this fire)"
    blocks: list[str] = []
    for d in items:
        content = (d.get("content") or "").strip()
        if not content:
            continue
        from_source = "lake"
        # Lake-id makes the item durable and addressable — engagement
        # attestations from the constituting act (Phase 3) need this id
        # to point cited_ids/dropped_ids at a real lake delta. Items
        # without a lake counterpart (pure puddle voice thoughts, etc.)
        # render with no id; the witness can still read the content but
        # can't cite them as engagement targets.
        lake_id_short = ""
        for t in (d.get("tags") or []):
            if t.startswith("from-source:"):
                from_source = t.split(":", 1)[1]
            elif t.startswith("lake-id:"):
                lake_id_short = t.split(":", 1)[1][:24]
            elif t.startswith("recalled-id:") and not lake_id_short:
                lake_id_short = t.split(":", 1)[1][:24]
        snippet = content[:600] + ("…" if len(content) > 600 else "")
        prefix = f"[{from_source} · id={lake_id_short}]" if lake_id_short else f"[{from_source}]"
        blocks.append(f"  {prefix} {snippet}")
    return "\n\n".join(blocks) if blocks else "  (no resonant material in the puddle this fire)"


async def _available_claude_code_hosts() -> list[str]:
    """Distinct hosts that have heartbeated in the last 5 minutes —
    these are the agents that can receive a `claude-code:<host>`
    dispatch. Empty list means nothing is online and the witness
    shouldn't pick that route this tick.
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
    hosts: set[str] = set()
    for b in beats:
        for t in b.get("tags") or []:
            if t.startswith("host:"):
                hosts.add(t.split(":", 1)[1])
                break
    return sorted(hosts)


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
    resonance_block: str,
    hosts_block: str,
    standpoint_block: str = "",
) -> dict | None:
    prompt = WITNESS_PROMPT.format(
        standpoint_block=standpoint_block
        or "(standpoint unavailable — integrate from anchors alone)",
        intent_block=intent_block,
        voice_blocks=voice_blocks,
        anchors_block=anchors_block,
        resonance_block=resonance_block,
        hosts_block=hosts_block,
        settled_status="deliberated",
        settled_descriptor=(
            "The voices took their turns — speak from the integrated take "
            "without performing consensus you didn't earn."
        ),
    )
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="hard",
            max_tokens=4096,
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
    body = (parsed.get("body") or "").strip()
    if not body:
        return None
    return {
        "kicker": (parsed.get("kicker") or "").strip(),
        "title": (parsed.get("title") or "").strip(),
        "body": body,
        "tail": (parsed.get("tail") or "").strip(),
        "body_image": (parsed.get("body_image") or "").strip(),
        "link": (parsed.get("link") or "").strip(),
        "links": parsed.get("links") or [],
        "route": (parsed.get("route") or "chat-reply").strip(),
        "addresses": parsed.get("addresses") or [],
        # Self-state fields — Phase 3 of the River refactor. Each fire is
        # also a constituting act: writes a small contribution into all
        # four self-systems (identity / affect / values / understanding).
        # All four optional — older models or budget-truncated outputs
        # may omit them; that's a graceful degradation, not an error.
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
    prompt = JUDGE_PROMPT.format(kicker=kicker, body=body, seed=seed)
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="hard",
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
        # depth=zero, OR every voice failed to produce a thought. Witness
        # speaks from intent + resonance + identity anchors alone — the
        # substrate is enough for casual drop-ins and small-talk replies.
        print("[witness] no voice thoughts — speaking from substrate alone")
        voice_blocks = (
            "(no parliament this tick — speak from the intent, the "
            "resonance pool, and your identity. This is a casual or "
            "low-stakes turn that doesn't need internal deliberation.)"
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
        meta_suffix = (" · " + " · ".join(meta_parts)) if meta_parts else ""
        intent_lines.append(f"  [intent-id: {iid_short} · kind: {kind}{meta_suffix}] {text}")
    intent_block = "\n".join(intent_lines)
    primary_intent = (pending[0].get("content") or "").strip()
    primary_intent_clean = primary_intent.split("\n\n[intent-payload]", 1)[0].strip()

    resonant = await _gather_witness_resonance(
        session_tag=session_tag,
        voice_blocks=voice_blocks,
        intent_text=primary_intent_clean,
    )
    resonance_block = _render_resonance_block(resonant)

    available_hosts = await _available_claude_code_hosts()
    standpoint_block = _render_standpoint_for_witness(standpoint)
    witness = await _call_witness(
        intent_block=intent_block,
        voice_blocks=voice_blocks,
        anchors_block=_render_anchors(),
        resonance_block=resonance_block,
        hosts_block=_render_hosts_block(available_hosts),
        standpoint_block=standpoint_block,
    )
    if witness is None:
        print("[witness] produced nothing")
        return []

    addresses_raw = witness.get("addresses") or []
    full_addressed: list[str] = []
    for a in addresses_raw:
        if isinstance(a, str) and a in short_to_full:
            full_addressed.append(short_to_full[a])
    # Default-claim-all when the witness leaves addresses empty — the
    # parliament deliberated on every pending intent and producing one
    # body claims them by virtue of integration. Dropping intents on the
    # floor would make them re-fire the same deliberation forever.
    if not full_addressed:
        full_addressed = [it.get("id") for it in pending if it.get("id")]

    if witness.get("route") == "unknown":
        axes = {"salience": 0.3, "novelty": 0.0, "resonance": 0.0,
                "confidence": 0.0, "comfort": 0.5}
    else:
        axes = await _call_judge(
            kicker=witness.get("kicker") or "",
            body=witness["body"],
            seed=primary_intent,
        )

    # Write the routed output as a feed-card delta. Dual-write:
    # Dual-write the witness card to lake (durable, no TTL) + puddle
    # (working memory, TTL'd). Engagement is now a marker delta with
    # an `engages:<lake_id>` pointer at this card, so the card itself
    # has to live durably or the pointer dangles. The judge axes still
    # get computed and stored on the card payload — useful for ranking
    # later — but they no longer gate whether the card survives.
    payload = {
        "kicker": witness.get("kicker") or "",
        "title": witness.get("title") or "",
        "body": witness["body"],
        "tail": witness.get("tail") or "",
        "body_image": witness.get("body_image") or "",
        "link": witness.get("link") or "",
        "links": witness.get("links") or [],
        "route": witness.get("route") or "chat-reply",
        "axes": axes,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    route_value = witness.get("route") or "chat-reply"

    # Read channel/correlation off the addressed intents — supervisor
    # groups by (channel, correlation) so every intent in this fire
    # carries the same pair (or none, for ambient fires). Stamp
    # `to:<channel>:<correlation>` on the output so the channel's
    # consumer (OpenAI endpoint poller, etc.) finds it with one tag
    # query without scanning route metadata.
    #
    # `host_for_channel` is captured for the claude-code channel: kitty
    # plugins on each machine query by (route:claude-code AND host:<me>),
    # so the host tag must propagate from the addressed intent onto the
    # outbound card. Other channels ignore it.
    channel, correlation = "", ""
    addressee = ""
    host_for_channel = ""
    for it in pending:
        ch, corr = extract_channel(it.get("tags") or [])
        if ch and not channel:
            channel, correlation = ch, corr
            for t in (it.get("tags") or []):
                if t.startswith("host:"):
                    host_for_channel = t.split(":", 1)[1]
                    break
        if not addressee:
            for t in (it.get("tags") or []):
                if t.startswith("contact:"):
                    addressee = t.split(":", 1)[1]
                    break
        if channel and addressee:
            break

    # Proactive claude-code dispatch — witness picked `claude-code:<host>`
    # as its route, meaning it wants to spawn a fresh kitty window on
    # that host with `body` as the prompt. This is the user-asked-for-
    # hands-on-work path; distinct from the closure-followup case where
    # the addressed intent already lives on the claude-code channel.
    #
    # We override channel/correlation/host with a fresh dispatch tuple
    # so the existing tag-stamping branches downstream emit the right
    # `to:claude-code:<corr>` + `host:<H>` + `task-corr:<corr>` set,
    # and the kitty plugin's `route:claude-code AND host:<myhost>`
    # query lands the dispatch at the targeted machine.
    proactive_route_raw = (witness.get("route") or "").strip()
    if proactive_route_raw.startswith("claude-code:") and available_hosts:
        target = proactive_route_raw.split(":", 1)[1].strip()
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

    # Channels with a known consumer (kitty for claude-code) need their
    # route to match the consumer's filter even when the witness model's
    # JSON didn't pick that route explicitly. The route field on the
    # JSON is still informational for feed rendering; the wire-level
    # routing comes from the `to:<channel>:<corr>` tag pair, which is
    # already stamped above. Here we keep `route:<...>` aligned with
    # the channel so nothing downstream has to special-case it.
    #
    # `closure:true` on an addressed intent means the task already
    # wrapped (claude wrote its task-complete delta) and the watcher
    # minted this intent from the closure. Routing back as
    # `claude-code` here would make kitty respawn the closed task. So
    # for closure-driven intents we use `chat-reply` instead — the
    # witness reply lands in the feed as a normal message.
    is_closure_followup = any(
        "closure:true" in (it.get("tags") or []) for it in pending
    )
    # `about_corr` / `about_host` carry the closure's task linkage onto
    # the chat-reply as informational tags (`about-task-corr:` /
    # `about-host:`) so the renderer can show "Fathom (about task on
    # <host>)" without misrepresenting the chat-reply as if it were
    # addressed to claude-code as a wire.
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
        # The closure-driven chat-reply addresses the user via `for:`
        # (the contact propagated by the watcher), not claude-code as
        # a destination. Drop the channel/correlation/host stamps; the
        # `about-task-corr` link below preserves the threading.
        about_corr = correlation
        about_host = host_for_channel
        channel = ""
        correlation = ""
        host_for_channel = ""

    # Include `addressing-output` so pending_intents() sees this card
    # as having addressed its intents even after a cold-start restore
    # (telepathy preserves these tags when mirroring the lake delta
    # back into a fresh puddle). Without it, the loop would re-fire
    # on questions it already answered.
    lake_tags = [
        "feed-card", "synthesis", "addressing-output",
        f"route:{route_value}",
    ]
    if channel and correlation:
        lake_tags.append(address_tag(channel, correlation))
        lake_tags.append(f"channel:{channel}")
    # Claude-code consumers (kitty plugin) match on
    # `route:claude-code AND host:<myhost>`; the host has to ride along
    # for that filter to land at the right machine. `task-corr:<corr>`
    # is the cross-cutting key the loop watcher uses to thread replies
    # to a particular task — present on the witness card, on the kitty
    # join delta, and on claude's closure delta.
    if channel == "claude-code":
        if host_for_channel:
            lake_tags.append(f"host:{host_for_channel}")
        if correlation:
            lake_tags.append(f"task-corr:{correlation}")
    if about_corr:
        lake_tags.append(f"about-task-corr:{about_corr}")
        if about_host:
            lake_tags.append(f"about-host:{about_host}")
    if addressee:
        # `for:<contact>` is the existing addressing convention (see
        # messages.send_message); reusing it means contact-scoped views
        # see Fathom's reply alongside any direct messages for the
        # same person. Also lets the dashboard render "Fathom > Myra".
        lake_tags.append(f"for:{addressee}")
    for intent_id in full_addressed:
        lake_tags.append(f"addresses:{intent_id}")
    lake_id = ""
    try:
        lake_delta = await delta_client.write(
            content=payload_json,
            tags=lake_tags,
            source="witness",
        )
        if isinstance(lake_delta, dict):
            lake_id = lake_delta.get("id") or ""
    except Exception as e:
        # Lake write is non-fatal — a transient lake hiccup must not
        # take the loop offline. The puddle copy still lands; the
        # card is just ephemeral for this fire.
        print(f"[witness] lake write failed (puddle still writing): {type(e).__name__}: {e}")

    # Phase 3 of the River refactor: the witness fire is also a
    # self-constituting act. After the main card writes, emit the small
    # side-effect deltas that drift identity, affect, and committed
    # values for future fires. lake_id is the provenance anchor;
    # without it (lake write failed) the side-effects skip — the next
    # fire's standpoint will pick up whatever DID land.
    if lake_id:
        try:
            await _write_constituting_writes(
                lake_card_id=lake_id,
                attestation=witness.get("attestation") or "",
                mood_shift=witness.get("mood_shift"),
                cited_ids=witness.get("cited_ids") or [],
                dropped_ids=witness.get("dropped_ids") or [],
            )
        except Exception as e:
            # Side-effect writes are non-fatal — the card still landed.
            # Log loudly so the loss is visible; the loop continues.
            print(
                f"[witness] constituting-act writes failed: "
                f"{type(e).__name__}: {e}"
            )
        # Phase 4a — voice priors. Separate from the constituting writes
        # because the predicate is different (judge axes, not witness
        # JSON). Soft-fails independently.
        try:
            await _write_voice_affirmations(
                lake_card_id=lake_id,
                voice_order=voice_order,
                axes=axes,
            )
        except Exception as e:
            print(
                f"[witness] voice-affirmation writes failed: "
                f"{type(e).__name__}: {e}"
            )

    puddle_tags = [
        CONVO_TAG, session_tag,
        "feed-card", "synthesis", "addressing-output",
        f"route:{route_value}",
    ]
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
        # Back-reference to the durable counterpart. recalled-id is the
        # canonical telepathy-dedupe tag (telepathy skips lake deltas
        # whose short id is already represented in the puddle); lake-id
        # carries the full id for engagement to look up directly.
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
    return full_addressed
