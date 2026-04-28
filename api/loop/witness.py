"""Witness pass — reads parliament voice thoughts, produces one card.

The witness is the loop's voice when speaking outward. Internally there
are three antagonists; externally there is one. This pass reads every
voice thought from the session, threads the pending intents through the
prompt, asks for an integrated body + a route + addressed intent-ids,
then runs an independent judge for the salience/novelty/resonance/
confidence/comfort axes.

Outputs are written to the puddle as `feed-card` deltas with TTLs from
the experiment (Q_A_TTL_S = 30min). Engagement promotes them to the
durable lake — handled in routes.py via the engagement endpoint.

For v1 we skip the "settled vs divergent" descriptor (we'd need the
metrics module first). We pass a generic "deliberated" descriptor and
let the witness write from the integrated take alone.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict

from .intents import CONVO_TAG, intent_kind
from .llm import loop_generate
from .prompts import JUDGE_PROMPT, WITNESS_PROMPT, VOICES
from .puddle import puddle


Q_A_TTL_S = 30 * 60


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

    The vampire-tap module (when wired) populates these by mirroring the
    latest crystal facets and mood-delta from the lake. Until vampire-tap
    is hooked up, this returns empty and the witness writes from the
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

    # Write the routed output as a feed-card delta. Tags include
    # `addresses:<id>` for each claimed intent so pending_intents() can
    # exclude them on the next pass.
    tags = [
        CONVO_TAG, session_tag,
        "feed-card", "synthesis", "addressing-output",
        f"route:{witness.get('route') or 'chat-reply'}",
    ]
    for intent_id in full_addressed:
        tags.append(f"addresses:{intent_id}")
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
    await puddle.write(
        content=json.dumps(payload, ensure_ascii=False),
        tags=tags,
        source="witness",
        ttl_seconds=Q_A_TTL_S,
    )
    print(f"[witness] addressed={len(full_addressed)} route={payload['route']!r} body[{len(payload['body'])}c]")
    return full_addressed
