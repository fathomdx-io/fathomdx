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
from collections import OrderedDict

from .. import delta_client
from ..channels import address_tag, extract_channel
from . import resonance
from .intents import CONVO_TAG, intent_kind
from .llm import loop_generate
from .prompts import JUDGE_PROMPT, WITNESS_PROMPT, VOICES
from .puddle import puddle


Q_A_TTL_S = 48 * 60 * 60  # rolling 48h horizon — see intents.py


def _group_thoughts_by_voice(deltas: list[dict]) -> dict[str, list[str]]:
    """Group thought-tagged deltas by voice, preserving chronological order."""
    by_voice: OrderedDict[str, list[str]] = OrderedDict()
    # Initialize in the canonical voice order so the witness always sees the
    # parliament in the same order regardless of which voices spoke first.
    for v in VOICES:
        by_voice[v["name"]] = []
    for d in sorted(deltas, key=lambda x: x.get("timestamp") or ""):
        if "thought" not in (d.get("tags") or []):
            continue
        voice_name = None
        for t in (d.get("tags") or []):
            if t.startswith("voice:"):
                voice_name = t.split(":", 1)[1]
                break
        if voice_name and voice_name in by_voice:
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
        for t in (d.get("tags") or []):
            if t.startswith("from-source:"):
                from_source = t.split(":", 1)[1]
                break
        snippet = content[:600] + ("…" if len(content) > 600 else "")
        blocks.append(f"  [{from_source}] {snippet}")
    return "\n\n".join(blocks) if blocks else "  (no resonant material in the puddle this fire)"


async def _call_witness(
    *,
    intent_block: str,
    voice_blocks: str,
    anchors_block: str,
    resonance_block: str,
) -> dict | None:
    prompt = WITNESS_PROMPT.format(
        intent_block=intent_block,
        voice_blocks=voice_blocks,
        anchors_block=anchors_block,
        resonance_block=resonance_block,
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
    }


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


async def run_witness(
    *,
    session_tag: str,
    pending: list[dict],
) -> list[str]:
    """Run the witness + judge for this session. Returns the list of
    intent-ids the witness claims to have addressed."""
    if not pending:
        return []

    voice_deltas = puddle.query(tags_include=[session_tag], limit=1000)
    by_voice = _group_thoughts_by_voice(voice_deltas)
    if not by_voice:
        print("[witness] no voice thoughts to integrate; skipping")
        return []

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
        meta_parts: list[str] = []
        if contact:
            meta_parts.append(f"from: {contact}")
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

    witness = await _call_witness(
        intent_block=intent_block,
        voice_blocks=voice_blocks,
        anchors_block=_render_anchors(),
        resonance_block=resonance_block,
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
    channel, correlation = "", ""
    addressee = ""
    for it in pending:
        ch, corr = extract_channel(it.get("tags") or [])
        if ch and not channel:
            channel, correlation = ch, corr
        if not addressee:
            for t in (it.get("tags") or []):
                if t.startswith("contact:"):
                    addressee = t.split(":", 1)[1]
                    break
        if channel and addressee:
            break

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

    puddle_tags = [
        CONVO_TAG, session_tag,
        "feed-card", "synthesis", "addressing-output",
        f"route:{route_value}",
    ]
    if channel and correlation:
        puddle_tags.append(address_tag(channel, correlation))
        puddle_tags.append(f"channel:{channel}")
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
