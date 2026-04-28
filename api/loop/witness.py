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
from datetime import datetime, timedelta, UTC

from .. import delta_client
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


async def _call_witness(
    *,
    intent_block: str,
    voice_blocks: str,
    anchors_block: str,
) -> dict | None:
    prompt = WITNESS_PROMPT.format(
        intent_block=intent_block,
        voice_blocks=voice_blocks,
        anchors_block=anchors_block,
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


def _should_auto_author(axes: dict) -> bool:
    """Worth-keeping signal — does this card become durable from birth?

    Two paths to durability without engagement:
      1. The card lands well *and* the witness is sure (resonance and
         confidence both above their thresholds).
      2. Salience alone is high enough that the topic matters even if
         the witness isn't sure of the take.

    Engagement remains the manual override. Tunable; starter values
    chosen to make auto-authoring the minority path until we see real
    distributions in production.
    """
    resonance = float(axes.get("resonance") or 0.0)
    confidence = float(axes.get("confidence") or 0.0)
    salience = float(axes.get("salience") or 0.0)
    return (resonance >= 0.7 and confidence >= 0.6) or salience >= 0.85


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
        intent_lines.append(f"  [intent-id: {iid_short} · kind: {kind}] {text}")
    intent_block = "\n".join(intent_lines)
    primary_intent = (pending[0].get("content") or "").strip()

    witness = await _call_witness(
        intent_block=intent_block,
        voice_blocks=voice_blocks,
        anchors_block=_render_anchors(),
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
    # lake first (so we have its id to back-reference from the puddle
    # copy), then puddle (the conscious-now view). The lake delta
    # carries a TTL by default — Q_A_TTL_S, same as the puddle copy —
    # unless the judge axes pass _should_auto_author, in which case
    # the lake write is durable from birth and the card persists past
    # the puddle's TTL even without engagement.
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
    auto_authored = _should_auto_author(axes)

    # Include `addressing-output` so pending_intents() sees this card
    # as having addressed its intents even after a cold-start restore
    # (telepathy preserves these tags when mirroring the lake delta
    # back into a fresh puddle). Without it, the loop would re-fire
    # on questions it already answered.
    lake_tags = [
        "feed-card", "synthesis", "addressing-output",
        f"route:{route_value}",
    ]
    for intent_id in full_addressed:
        lake_tags.append(f"addresses:{intent_id}")
    if auto_authored:
        lake_tags.append("auto-authored")
    expires_at_iso: str | None = None
    if not auto_authored:
        expires_at_iso = (
            datetime.now(UTC) + timedelta(seconds=Q_A_TTL_S)
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
        # Lake write is non-fatal — a transient lake hiccup must not
        # take the loop offline. The puddle copy still lands; the
        # card is just ephemeral for this fire.
        print(f"[witness] lake write failed (puddle still writing): {type(e).__name__}: {e}")

    puddle_tags = [
        CONVO_TAG, session_tag,
        "feed-card", "synthesis", "addressing-output",
        f"route:{route_value}",
    ]
    for intent_id in full_addressed:
        puddle_tags.append(f"addresses:{intent_id}")
    if auto_authored:
        puddle_tags.append("auto-authored")
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
        f"body[{len(payload['body'])}c] auto={auto_authored} "
        f"lake-id={lake_id[:24] or 'none'}"
    )
    return full_addressed
