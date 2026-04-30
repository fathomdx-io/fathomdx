"""Voice stances — drift-able lake objects, no longer frozen constants.

Phase 5a of the River refactor. The trimurti's stance/bias prose used
to live as a static `VOICES` list in `prompts.py`. Phase 5a moves that
to lake-stored ``kind:voice-stance`` deltas, latest-per-voice, with
the static list as a permanent fallback for any voice the lake hasn't
spoken about yet.

  get_voice_stances() → list[{name, stance, bias}]

Lake takes precedence: if a voice has a recent ``kind:voice-stance``
delta tagged ``voice:<name>``, that's what gets returned. Voices the
lake doesn't know about (cold-start, or domain voices that never had
a stance regen) inherit from the static defaults.

The regen path (`regenerate_voice_stance`) reads recent voice-
affirmation deltas, pulls the cited witness cards, asks an LLM to
refine the stance/bias to better match what's been working. One
``kind:voice-stance`` write per regen.

Triggering regen is left to a higher layer — could be a slow cron,
could be threshold-driven (after N affirmations), could be hand-fired
during development. This module just exposes the read + the regen
function; when to call them is policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta

from .. import delta_client
from .llm import loop_generate
from .prompts import VOICES as STATIC_VOICES

log = logging.getLogger(__name__)

# How far back to look for stance deltas. Long — stances are slow-
# moving identity components, not session-scoped. A stance from a
# month ago is still the latest if no regen has happened since.
_STANCE_WINDOW_HOURS = 30 * 24

# How many recent affirmations to consider when regenerating a stance.
# Enough to capture a pattern; not so many that the LLM drowns in
# context. Each affirmation pulls in its cited witness card body via
# the from: tag.
_REGEN_AFFIRMATIONS_LIMIT = 8

# How many cards-per-affirmation to fetch as substrate for the regen
# prompt. Affirmations point to witness cards via from:<id>; we pull
# the body of each so the LLM sees what the voice actually contributed
# to. Tight char budget per body.
_REGEN_CARD_BODY_CHARS = 400


# ── Read path ────────────────────────────────────────────────────────


def _static_lookup() -> dict[str, dict[str, str]]:
    """Static VOICES list as a name-keyed dict for cheap fallback."""
    return {v["name"]: dict(v) for v in STATIC_VOICES}


async def _load_lake_stances() -> dict[str, dict]:
    """Pull latest kind:voice-stance per voice from the lake.

    Returns name → {stance, bias, delta_id, timestamp}. Empty dict on
    lake error (consumers fall through to static).
    """
    try:
        rows = await delta_client.query(
            tags_include=["kind:voice-stance"],
            limit=200,
        )
    except Exception:
        log.exception("voice_stances: lake query failed")
        return {}
    if not rows:
        return {}

    # Latest per voice — rows come back newest-first by default, so
    # first hit per voice name wins.
    latest: dict[str, dict] = {}
    for d in rows:
        voice_name = ""
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("voice:"):
                voice_name = t.split(":", 1)[1].strip()
                break
        if not voice_name or voice_name in latest:
            continue
        try:
            payload = json.loads(d.get("content") or "{}")
        except (ValueError, TypeError):
            continue
        stance = (payload.get("stance") or "").strip()
        bias = (payload.get("bias") or "").strip()
        if not stance and not bias:
            continue
        latest[voice_name] = {
            "stance": stance,
            "bias": bias,
            "delta_id": d.get("id"),
            "timestamp": d.get("timestamp"),
        }
    return latest


async def get_voice_stances() -> list[dict[str, str]]:
    """Authoritative voice list — lake-stored where available, static
    otherwise. Returns the same shape as `prompts.VOICES` so existing
    callers can substitute one for the other.

    Order: trimurti first (creator / preserver / destroyer), then any
    other voices the lake has stances for (alphabetical for stability).
    Lake-stored voices not in the trimurti are appended — domain voices
    that earned standing get to surface here.
    """
    lake = await _load_lake_stances()
    static = _static_lookup()
    out: list[dict[str, str]] = []

    # Trimurti first, in canonical order.
    for v in STATIC_VOICES:
        name = v["name"]
        if name in lake:
            out.append(
                {
                    "name": name,
                    "stance": lake[name]["stance"] or v["stance"],
                    "bias": lake[name]["bias"] or v["bias"],
                }
            )
        else:
            out.append(dict(v))

    # Domain voices the lake knows but the static list doesn't.
    extra = sorted(set(lake) - set(static))
    for name in extra:
        out.append(
            {
                "name": name,
                "stance": lake[name]["stance"],
                "bias": lake[name]["bias"],
            }
        )

    return out


# ── Regen path ───────────────────────────────────────────────────────


_REGEN_PROMPT = """You are refining a voice's stance and bias text based on its recent contributions.

This voice is **{voice_name}** in Fathom's parliament. Its current stance:

  {current_stance}

Its current bias (the failure mode it tends to overdo):

  {current_bias}

Recent witness cards the parliament produced when {voice_name} was active and the judge rated the fire well (so {voice_name} contributed productively). One block per card:

{card_blocks}

Refine the stance and bias to better match what {voice_name} has actually been doing well. The goal is NOT to drift toward generic helpfulness — keep the voice's distinct angle. The goal IS to make the stance text more accurate to how this voice has been showing up in productive deliberation.

Constraints:
- Stance: 2-4 sentences. Format: "Your default lens: <what they pull toward>. That's where you START — not where you END. <how they update when other voices land something true.>"
- Bias: one sentence. The voice's failure mode that other voices should check.
- Don't lose the voice's antagonistic-with-collaborators frame. They serve the parliament, not their own stance.
- If the recent fires don't actually justify a refinement, return the current stance/bias unchanged.

Return STRICT JSON:
{{
  "stance": "<refined stance, or the current one if no real refinement>",
  "bias":   "<refined bias, or the current one>"
}}"""


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


async def _gather_regen_substrate(voice_name: str) -> list[str]:
    """Pull recent voice-affirmation deltas for this voice; for each,
    fetch the cited witness card body. Returns rendered text blocks."""
    try:
        affirmations = await delta_client.query(
            tags_include=["kind:voice-affirmation", f"voice:{voice_name}"],
            limit=_REGEN_AFFIRMATIONS_LIMIT,
        )
    except Exception:
        log.exception("voice_stances: affirmation query failed")
        return []
    if not affirmations:
        return []

    # Pull each affirmation's cited card.
    card_ids: list[str] = []
    for a in affirmations:
        for t in a.get("tags") or []:
            if isinstance(t, str) and t.startswith("from:"):
                card_ids.append(t.split(":", 1)[1])
                break
    if not card_ids:
        return []

    try:
        cards = await delta_client.batch_get(card_ids)
    except Exception:
        log.exception("voice_stances: card batch_get failed")
        return []

    blocks: list[str] = []
    for c in cards:
        content = (c.get("content") or "").strip()
        if not content:
            continue
        body = ""
        # Witness cards are JSON payloads; pull body. Other shapes
        # render as their content.
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                body = (payload.get("body") or payload.get("title") or "").strip()
        except (ValueError, TypeError):
            body = content
        if not body:
            continue
        body = body.replace("\n", " ")[:_REGEN_CARD_BODY_CHARS]
        blocks.append(f"  · {body}")
    return blocks


async def regenerate_voice_stance(voice_name: str) -> str | None:
    """Refine and persist a voice's stance/bias from recent affirmation
    history. Returns the new stance delta id, or None when there's
    insufficient signal or the LLM refused to refine.

    Caller is responsible for triggering — this is the activity, not
    the schedule. Triggers can be threshold-driven (e.g., after N
    affirmations since last regen) or cron-driven, or hand-fired.
    """
    if not voice_name:
        return None

    # What's the current stance? Lake first, then static fallback.
    lake = await _load_lake_stances()
    static = _static_lookup()
    current = lake.get(voice_name) or static.get(voice_name)
    if not current:
        # Domain voice with no static fallback and no prior lake
        # stance — refuse to regen out of thin air; the convener
        # mints these on the fly anyway.
        return None
    current_stance = current.get("stance") or ""
    current_bias = current.get("bias") or ""

    blocks = await _gather_regen_substrate(voice_name)
    if not blocks:
        return None

    prompt = _REGEN_PROMPT.format(
        voice_name=voice_name,
        current_stance=current_stance,
        current_bias=current_bias,
        card_blocks="\n".join(blocks),
    )
    try:
        raw = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=1024,
            temperature=0.3,
            json_mode=True,
        )
    except Exception:
        log.exception("voice_stances: LLM call failed")
        return None

    try:
        parsed = json.loads(_strip_fences(raw))
    except Exception:
        log.warning(
            "voice_stances: regen returned unparseable JSON for %s: %r",
            voice_name,
            raw[:200],
        )
        return None

    new_stance = (parsed.get("stance") or "").strip()
    new_bias = (parsed.get("bias") or "").strip()
    if not new_stance or not new_bias:
        return None

    # No-op when the LLM returned the current text unchanged — saves
    # an unnecessary lake write. Compare normalized to ignore
    # whitespace tweaks that don't represent real drift.
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    if _norm(new_stance) == _norm(current_stance) and _norm(new_bias) == _norm(current_bias):
        return None

    payload = json.dumps({"stance": new_stance, "bias": new_bias}, ensure_ascii=False)
    try:
        written = await delta_client.write(
            content=payload,
            tags=[
                "kind:voice-stance",
                f"voice:{voice_name}",
            ],
            source="fathom-self",
        )
    except Exception:
        log.exception("voice_stances: regen write failed")
        return None
    return (written or {}).get("id")


# ── Watcher / scheduler ──────────────────────────────────────────────


# How often the watcher wakes. Stance regen is slow-clock by design —
# voices don't drift session-to-session, they drift across days. 6h
# matches the cadence at which the loop produces enough new
# affirmations for a refinement to be more than noise.
_WATCHER_POLL_HOURS = 6

# Per-voice gate: only regenerate when this many NEW affirmations have
# landed since the voice's last stance write. Prevents back-to-back
# regens on tiny signal — the LLM's no-op detection helps but it's
# expensive to fire one LLM call to learn there's nothing to refine.
_REGEN_AFFIRMATION_GATE = 5

# Window for "new affirmations since last stance." Matched roughly
# to the watcher poll cadence so a quiet six hours doesn't hide a
# real burst.
_AFFIRMATION_LOOKBACK_HOURS = 24


async def _voices_eligible_for_regen() -> list[str]:
    """Pick voices that have accumulated enough new affirmations to
    justify a regen pass. Returns at most one voice per call so a
    busy watcher tick doesn't fire many LLM calls in parallel — the
    next poll picks up the next eligible voice.

    Eligibility: voice has >= _REGEN_AFFIRMATION_GATE affirmations in
    the lookback window AND those affirmations are newer than the
    voice's last kind:voice-stance write (or no stance write exists).
    """
    since = (
        datetime.now(UTC) - timedelta(hours=_AFFIRMATION_LOOKBACK_HOURS)
    ).isoformat()
    try:
        affirmations = await delta_client.query(
            tags_include=["kind:voice-affirmation"],
            time_start=since,
            limit=200,
        )
    except Exception:
        log.exception("voice_stances watcher: affirmation query failed")
        return []
    if not affirmations:
        return []

    # Count affirmations per voice and capture each voice's latest
    # affirmation timestamp.
    counts: dict[str, int] = {}
    latest_aff_ts: dict[str, str] = {}
    for d in affirmations:
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("voice:"):
                name = t.split(":", 1)[1].strip()
                if not name:
                    continue
                counts[name] = counts.get(name, 0) + 1
                ts = d.get("timestamp") or ""
                if ts > latest_aff_ts.get(name, ""):
                    latest_aff_ts[name] = ts
                break

    # For each voice over the gate, check whether their last stance
    # write predates the most recent affirmation. If yes, eligible.
    lake_stances = await _load_lake_stances()
    candidates: list[tuple[str, int]] = []
    for name, count in counts.items():
        if count < _REGEN_AFFIRMATION_GATE:
            continue
        last_stance_ts = (lake_stances.get(name) or {}).get("timestamp") or ""
        last_aff_ts = latest_aff_ts.get(name, "")
        if last_aff_ts > last_stance_ts:
            candidates.append((name, count))

    if not candidates:
        return []
    # Highest-affirmation-count first — voice with the most signal
    # gets refined first. Ties broken by name for stability.
    candidates.sort(key=lambda kv: (-kv[1], kv[0]))
    return [candidates[0][0]]


async def stance_regen_watcher() -> None:
    """Background task — periodically refines voice stances based on
    accumulated affirmation history.

    Slow-clock by design: voice stances don't change moment-to-moment,
    they drift over days. Each poll picks at most one voice and
    refines it; a busy convene-pace doesn't get torn-stance reads
    mid-fire because Standpoint snapshots once per fire.

    Soft-fails any individual regen — a single bad voice doesn't
    cancel the watcher.
    """
    poll_seconds = _WATCHER_POLL_HOURS * 3600
    print(f"[stance regen watcher armed] poll={_WATCHER_POLL_HOURS}h")
    while True:
        try:
            await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            return
        try:
            eligible = await _voices_eligible_for_regen()
        except Exception as e:
            print(
                f"[stance regen watcher] eligibility crashed: "
                f"{type(e).__name__}: {e}"
            )
            continue
        if not eligible:
            continue
        for voice_name in eligible:
            try:
                stance_id = await regenerate_voice_stance(voice_name)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(
                    f"[stance regen watcher] regen for {voice_name} crashed: "
                    f"{type(e).__name__}: {e}"
                )
                continue
            if stance_id:
                print(
                    f"[stance regen] {voice_name} drifted → new stance "
                    f"delta {stance_id[:24]}"
                )
