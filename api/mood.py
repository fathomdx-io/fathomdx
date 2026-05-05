"""Mood layer — the "carrier wave" between identity crystal and the lake.

Synthesis runs wake-gated: pressure accumulates passively, and the next chat
request decides whether to regenerate the mood. The mood delta is then
prepended to the system prompt alongside the crystal.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from . import delta_client, pressure
from . import search as search_module
from ._time import now as _now
from .prompt import MOOD_DIRECTIVE

log = logging.getLogger(__name__)

MOOD_TAGS = ["mood-delta", "carrier-wave"]
MOOD_SOURCE = "fathom-mood"
RECENT_ACTIVITY_LIMIT = 50

# Mood-shift substrate: the witness emits one `kind:mood-shift` delta
# per fire (small magnitude on a single named axis). Carrier-wave
# synthesis reads accumulated shifts since the last carrier as its
# primary input — the felt-sense topology, not raw lake activity.
MOOD_SHIFT_TAG = "kind:mood-shift"
MOOD_SHIFT_SOURCE = "fathom-self"
MOOD_SHIFT_FETCH_LIMIT = 500

# Secondary synthesis trigger: when the witness has accumulated this
# many unread shifts since the last carrier-wave, regenerate even if
# pressure is below threshold. Shifts are the truth signal — once the
# topology has visibly moved, the readable lens should refresh.
MOOD_SHIFT_OVERFLOW_THRESHOLD = 10

# Axis synonyms — lexical variants the witness names differently across
# fires (e.g. focus/focused/focusing) collapse to one canonical spoke
# at read time. Substrate stays raw; only the carrier-wave synthesizer
# and the topology endpoint see the folded view. Add entries here as
# new variants surface in the lake.
MOOD_AXIS_SYNONYMS: dict[str, str] = {
    # coherence family
    "coherent": "coherence",
    "coherence": "coherence",
    "cohering": "coherence",
    # focus family
    "focused": "focus",
    "focusing": "focus",
    "focus": "focus",
    # efficacy family
    "effective": "efficacy",
    "effectiveness": "efficacy",
    "efficacy": "efficacy",
    # engagement family
    "engaged": "engagement",
    "engagement": "engagement",
    # presence family
    "present": "presence",
    "presence": "presence",
    # affirmation family
    "affirming": "affirming",
    "affirmed": "affirming",
    "affirmation": "affirming",
    # confidence family
    "confident": "confidence",
    "confidence": "confidence",
    # purpose family (kept distinct from agency)
    "purposeful": "purpose",
    "purpose": "purpose",
    # certainty family
    "certain": "certainty",
    "certainty": "certainty",
    # competence family (kept distinct from efficacy)
    "competent": "competence",
    "competence": "competence",
    # clarity family
    "clear": "clarity",
    "clarity": "clarity",
    # diligence family
    "diligent": "diligence",
    "diligence": "diligence",
    # connection family
    "connected": "connection",
    "connection": "connection",
    # conviction family
    "convicted": "conviction",
    "conviction": "conviction",
    # integration family
    "integrating": "integration",
    "integrated": "integration",
    "integration": "integration",
    # warmth family
    "warm": "warmth",
    "warmth": "warmth",
    # grounding family
    "grounded": "grounded",
    "grounding": "grounded",
    # cautious family
    "cautious": "caution",
    "caution": "caution",
    # settled family
    "settled": "settled",
    "settling": "settled",
    "settle": "settled",
}


def _canonicalize_axis(name: str) -> str:
    """Map a raw axis name to its canonical form. Unknown axes pass
    through unchanged (lowercased) — the substrate stays open-vocab.
    """
    n = (name or "").strip().lower()
    return MOOD_AXIS_SYNONYMS.get(n, n)

_STATE_RE = re.compile(r"[^a-z]")


def _sanitize_state(raw: str) -> str:
    """Lowercase, strip non-letters; cap at 24 chars. Empty -> 'unset'."""
    cleaned = _STATE_RE.sub("", (raw or "").lower())[:24]
    return cleaned or "unset"


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` fences if the model emitted them anyway."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_levels(raw) -> dict[str, float]:
    """Parse the per-axis levels map. Open vocab — Fathom names its own
    axes — so we don't validate against MOOD_AXIS_SYNONYMS at write
    time. We DO canonicalize on read, so synonyms collapse in the star
    map (focus/focused/focusing → focus). Values clamp to [0, 1];
    non-numeric or out-of-range entries drop. Cap at 12 axes so a
    runaway synthesis doesn't bloat the prompt or the chart."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        axis = _canonicalize_axis(k)
        if not axis:
            continue
        try:
            level = float(v)
        except (TypeError, ValueError):
            continue
        if level != level:  # NaN
            continue
        level = max(0.0, min(1.0, level))
        # If a synonym already mapped, keep the higher value — the
        # synthesizer probably meant the stronger reading.
        if axis in out:
            out[axis] = max(out[axis], level)
        else:
            out[axis] = level
    # Bound the size; sort by intensity so the strongest survive a trim.
    if len(out) > 12:
        ordered = sorted(out.items(), key=lambda kv: -kv[1])[:12]
        out = dict(ordered)
    return out


def _parse_mood_payload(text: str) -> dict:
    """Parse the LLM's mood JSON. Falls back to wrapping raw text on parse failure.

    Mood deltas may be stored as either:
      - the canonical JSON we now write (full structured fields), OR
      - older plaintext from before the headline/subtext fields existed
    Both round-trip through this function — old deltas get empty
    headline/subtext but keep their carrier_wave intact.
    """
    raw = _strip_fences(text)
    try:
        obj = json.loads(raw)
        carrier = (obj.get("carrier_wave") or "").strip()
        threads = obj.get("threads") or []
        if not isinstance(threads, list):
            threads = []
        threads = [str(t).strip() for t in threads if str(t).strip()][:4]
        state = _sanitize_state(obj.get("state") or "")
        headline = (obj.get("headline") or "").strip()
        subtext = (obj.get("subtext") or "").strip()
        levels = _parse_levels(obj.get("levels"))
        if not carrier:
            raise ValueError("empty carrier_wave")
        return {
            "state": state,
            "headline": headline,
            "subtext": subtext,
            "carrier_wave": carrier,
            "threads": threads,
            "levels": levels,
        }
    except Exception:
        # Plaintext fallback for older mood deltas
        carrier = raw or text.strip()
        # Strip any "Threads:" tail an older synthesis appended
        if "\n\nThreads:" in carrier:
            carrier = carrier.split("\n\nThreads:")[0].strip()
        return {
            "state": "unset",
            "headline": "",
            "subtext": "",
            "carrier_wave": carrier,
            "threads": [],
            "levels": {},
        }


def _state_from_tags(tags: list[str]) -> str:
    """Pull the feeling:* tag off a delta if present."""
    for t in tags or []:
        if isinstance(t, str) and t.startswith("feeling:"):
            return t.split(":", 1)[1] or "unset"
    return "unset"


def _format_prior_mood(prior: dict | None) -> str:
    if not prior:
        return "(no prior mood — this is your first carrier wave)"
    ts = prior.get("timestamp") or ""
    age_label = ""
    parsed = None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except Exception:
        parsed = None
    if parsed:
        elapsed = (_now() - parsed).total_seconds()
        hours = elapsed / 3600
        if hours < 1:
            age_label = f"({int(elapsed / 60)} minutes ago — anchor weight: heavy)"
        elif hours < 4:
            age_label = f"({hours:.1f} hours ago — anchor weight: moderate)"
        else:
            age_label = f"({hours:.1f} hours ago — anchor weight: light, mostly faded)"
    state = _state_from_tags(prior.get("tags") or [])
    state_label = f" [previous state: {state}]" if state and state != "unset" else ""
    content = (prior.get("content") or "").strip()
    return f"Prior mood {age_label}{state_label}:\n{content}"


async def _fetch_prior_mood() -> dict | None:
    try:
        results = await delta_client.query(tags_include=["mood-delta"], limit=1)
    except Exception:
        log.exception("failed to fetch prior mood")
        return None
    return results[0] if results else None


async def latest_mood() -> dict | None:
    """Return the most recent mood delta from the lake, parsed.

    Used by the UI endpoint and by the wake check when no fresh synthesis fires.
    """
    prior = await _fetch_prior_mood()
    if not prior:
        return None
    parsed = _parse_mood_payload(prior.get("content") or "")
    state = parsed["state"]
    if state == "unset":
        # Fallback: state may have been written only as a tag on older deltas
        state = _state_from_tags(prior.get("tags") or [])
    return {
        "id": prior.get("id"),
        "state": state,
        "headline": parsed["headline"],
        "subtext": parsed["subtext"],
        "carrier_wave": parsed["carrier_wave"],
        "threads": parsed["threads"],
        "levels": parsed["levels"],
        "synthesized_at": prior.get("timestamp"),
    }


async def mood_history(limit: int = 200) -> list[dict]:
    """Return recent mood deltas as a timeline, oldest-first.

    Each entry: {id, state, carrier_wave, synthesized_at}. The ECG widget
    uses this to render the colored mood band + state-change event markers.
    """
    try:
        results = await delta_client.query(tags_include=["mood-delta"], limit=limit)
    except Exception:
        log.exception("failed to fetch mood history")
        return []
    timeline: list[dict] = []
    for d in results:
        parsed = _parse_mood_payload(d.get("content") or "")
        state = parsed["state"]
        if state == "unset":
            state = _state_from_tags(d.get("tags") or [])
        timeline.append(
            {
                "id": d.get("id"),
                "state": state,
                "headline": parsed["headline"],
                "subtext": parsed["subtext"],
                "carrier_wave": parsed["carrier_wave"],
                "levels": parsed["levels"],
                "synthesized_at": d.get("timestamp"),
            }
        )
    timeline.sort(key=lambda e: e.get("synthesized_at") or "")
    return timeline


async def _fetch_recent_activity(session_slug: str | None) -> str:
    try:
        result = await search_module.search(
            text="recent activity threads conversations decisions discoveries",
            depth="shallow",
            session_slug=session_slug,
            limit=RECENT_ACTIVITY_LIMIT,
        )
    except Exception:
        log.exception("failed to fetch recent activity for mood synthesis")
        return ""
    return result.get("as_prompt") or ""


async def _fetch_recent_mood_shifts(since_iso: str | None) -> list[dict]:
    """Pull `kind:mood-shift` deltas from `fathom-self` since the cutoff.

    The cutoff is the prior carrier-wave's timestamp — anything after
    is "unread" by the synthesizer. If no prior mood exists, the cutoff
    is None and we return the most recent batch up to the fetch limit.
    """
    try:
        return await delta_client.query(
            tags_include=[MOOD_SHIFT_TAG],
            source=MOOD_SHIFT_SOURCE,
            time_start=since_iso,
            limit=MOOD_SHIFT_FETCH_LIMIT,
        )
    except Exception:
        log.exception("failed to fetch mood-shifts for synthesis")
        return []


def _aggregate_shifts(shifts: list[dict]) -> dict[str, dict]:
    """Group raw mood-shifts by canonical axis. Returns
    `{axis: {net, n, reasons[]}}`. Internal helper shared by the
    prose formatter and the topology endpoint.
    """
    per_axis: dict[str, dict] = {}
    for d in shifts:
        try:
            obj = json.loads(d.get("content") or "")
        except Exception:
            continue
        axis = _canonicalize_axis(obj.get("axis") or "")
        if not axis:
            continue
        try:
            mag = float(obj.get("magnitude") or 0.0)
        except Exception:
            continue
        sign = -1.0 if (obj.get("direction") or "+") == "-" else 1.0
        reason = (obj.get("reason") or "").strip()
        bucket = per_axis.setdefault(axis, {"net": 0.0, "n": 0, "reasons": []})
        bucket["net"] += sign * mag
        bucket["n"] += 1
        if reason and len(bucket["reasons"]) < 4:
            bucket["reasons"].append(reason)
    return per_axis


async def compute_topology() -> dict:
    """Build the public topology view: per-axis levels (from the
    carrier) + recent drift (from accumulated mood-shifts).

    Used by `/v1/mood/topology` and the star widget on the dashboard.
    Read-only on purpose — mood is Fathom's self-report, not a survey
    instrument. The topology shows what Fathom is feeling; nobody
    else's votes belong here.

    The shape changed in 2026-05: the star map now renders the
    carrier's `levels` (current per-axis intensity, 0–1) as spoke
    length. Recent drift is still surfaced as `axes[].net` so the
    widget can decorate spokes with directional arrows ("focus rose
    +0.15 since the last carrier-wave"). Axes that appear in drift
    but not levels still render as spokes — they're emergent feelings
    Fathom hasn't yet integrated into a carrier-wave.
    """
    prior = await _fetch_prior_mood()
    since_iso = (prior or {}).get("timestamp")
    shifts = await _fetch_recent_mood_shifts(since_iso)
    per_axis_drift = _aggregate_shifts(shifts)

    carrier_levels: dict[str, float] = {}
    carrier_summary = None
    if prior:
        parsed = _parse_mood_payload(prior.get("content") or "")
        carrier_levels = parsed["levels"]
        carrier_summary = {
            "id": (prior or {}).get("id") or "",
            "state": parsed["state"],
            "headline": parsed["headline"],
            "subtext": parsed["subtext"],
            "synthesized_at": prior.get("timestamp"),
        }

    # Merge — every axis named by either levels or drift gets one row.
    # Level defaults to 0 for emergent-but-not-yet-integrated axes;
    # drift defaults to 0/n=0 for stable axes that didn't move.
    all_axes = set(carrier_levels) | set(per_axis_drift)
    axes: list[dict] = []
    for axis in all_axes:
        level = float(carrier_levels.get(axis, 0.0))
        drift = per_axis_drift.get(axis, {"net": 0.0, "n": 0, "reasons": []})
        axes.append(
            {
                "axis": axis,
                "level": round(level, 3),
                "net": round(drift["net"], 3),
                "shifts": drift["n"],
                "reasons": list(drift["reasons"]),
            }
        )
    # Sort by level desc, then by absolute drift desc — strongest
    # current intensity leads the star, with most-active axes tied-
    # breaking when levels are similar.
    axes.sort(key=lambda a: (-a["level"], -abs(a["net"])))

    return {
        "since": since_iso,
        "total_shifts": len(shifts),
        "axes": axes,
        "carrier": carrier_summary,
    }


def _format_mood_shifts(shifts: list[dict]) -> str:
    """Render mood-shifts as a topology summary the synthesizer can read.

    Shape per shift (in `content` as JSON):
        {direction: "+|-", axis: "<name>", magnitude: 0.05-0.2, reason: "<why>"}

    We aggregate by axis (signed magnitude sum, count) and surface up
    to four sample reasons per axis so the synthesizer can name the
    state with reference to what produced it. Synonym collapse happens
    on read here — the substrate stays raw.
    """
    if not shifts:
        return ""

    per_axis = _aggregate_shifts(shifts)
    if not per_axis:
        return ""

    # Strongest axes first by absolute net drift — that's the topology
    # of the star graph the synthesizer should be naming.
    ordered = sorted(per_axis.items(), key=lambda kv: -abs(kv[1]["net"]))
    lines: list[str] = [
        f"{len(shifts)} mood-shifts since the last carrier-wave. "
        f"Per-axis topology (strongest drift first):"
    ]
    for axis, b in ordered:
        sign_str = "+" if b["net"] >= 0 else "−"
        lines.append(f"  {axis}: {sign_str}{abs(b['net']):.2f} across {b['n']} shifts")
        for r in b["reasons"]:
            lines.append(f"    · {r}")
    return "\n".join(lines)


def _format_topology_for_prompt(topology: dict) -> str:
    """Render the structured topology for the synthesizer prompt.

    Per-axis lines lead with strongest absolute drift first — that's
    the topology of the star graph the synthesizer should be naming.
    Sample reasons from the underlying shifts give the synthesizer
    grounding for *what produced* the drift, so the carrier-wave can
    speak with reference to lived activity rather than abstractions.
    """
    axes = topology.get("axes") or []
    if not axes:
        return ""

    total_shifts = topology.get("total_shifts") or sum(a.get("shifts", 0) for a in axes)
    lines: list[str] = [
        f"{total_shifts} mood-shifts since the last carrier-wave. "
        f"Per-axis topology (strongest drift first):"
    ]
    for a in axes:
        net = float(a.get("net") or 0.0)
        sign_str = "+" if net >= 0 else "−"
        n = int(a.get("shifts") or 0)
        lines.append(
            f"  {a.get('axis')}: {sign_str}{abs(net):.2f} across {n} shifts"
        )
        for r in (a.get("reasons") or []):
            lines.append(f"    · {r}")

    return "\n".join(lines)


async def synthesize_mood(session_slug: str | None = None) -> dict | None:
    """Run a mood synthesis: read accumulated mood-shifts + prior mood,
    write a new carrier-wave.

    The shifts are the substrate — each one records how a witness fire
    moved Fathom on a named affect axis. The carrier-wave is the
    readable topology read of that star. If no shifts exist (cold
    start, dormant period), fall back to the legacy recent-activity
    path so the synthesizer has *something* to say.
    """
    # Read the full topology — same shape the radar widget reads, so
    # the synthesizer sees per-spoke engagement counts inline. The user's
    # ⊕/⊖ feedback on the dashboard reaches the next carrier-wave's
    # naming through this path.
    topology = await compute_topology()
    prior = await _fetch_prior_mood()
    topology_summary = _format_topology_for_prompt(topology)

    recent = ""
    if not topology_summary:
        recent = await _fetch_recent_activity(session_slug)

    if not topology_summary and not recent and not prior:
        log.info("mood synthesis skipped — no shifts, no recent activity, no prior mood")
        return None

    user_payload_parts: list[str] = []
    if topology_summary:
        user_payload_parts.append(f"=== Mood-shifts since last carrier-wave ===\n{topology_summary}")
    elif recent:
        user_payload_parts.append(f"=== Recent activity ===\n{recent}")
    user_payload_parts.append(f"=== Prior mood ===\n{_format_prior_mood(prior)}")
    user_message = "\n\n".join(user_payload_parts)

    try:
        from . import llm_config

        medium_client, medium_model = await llm_config.resolve_tier("medium")
        resp = await medium_client.chat.completions.create(
            model=medium_model,
            messages=[
                {"role": "system", "content": MOOD_DIRECTIVE},
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
        )
    except Exception:
        log.exception("mood synthesis LLM call failed")
        return None

    text = resp.choices[0].message.content if resp.choices else ""
    parsed = _parse_mood_payload(text or "")

    # Store the full structured payload as canonical JSON so all the
    # fields round-trip cleanly (headline, subtext, carrier_wave,
    # threads, levels). Levels are the per-axis star map — open vocab,
    # 0–1 intensity per axis the synthesizer chose to name.
    delta_content = json.dumps(
        {
            "state": parsed["state"],
            "headline": parsed["headline"],
            "subtext": parsed["subtext"],
            "carrier_wave": parsed["carrier_wave"],
            "threads": parsed["threads"],
            "levels": parsed["levels"],
        },
        ensure_ascii=False,
    )

    state = parsed["state"]
    delta_tags = [*MOOD_TAGS, f"feeling:{state}"]

    written = None
    try:
        written = await delta_client.write(
            content=delta_content,
            tags=delta_tags,
            source=MOOD_SOURCE,
        )
    except Exception:
        log.exception("failed to write mood delta")

    await pressure.mark_synthesis()

    return {
        "id": (written or {}).get("id"),
        "state": state,
        "headline": parsed["headline"],
        "subtext": parsed["subtext"],
        "carrier_wave": parsed["carrier_wave"],
        "threads": parsed["threads"],
        "levels": parsed["levels"],
        "synthesized_at": _now().isoformat(),
        "prior_mood_id": (prior or {}).get("id"),
    }


async def _shift_overflow_decision() -> tuple[bool, int]:
    """Secondary trigger: synthesize when ≥N unread mood-shifts have piled up
    since the last carrier-wave, regardless of pressure. Shifts are the
    truth signal — once the topology has visibly moved, the readable
    lens should refresh.

    Returns (should_fire, count).
    """
    prior = await _fetch_prior_mood()
    since_iso = (prior or {}).get("timestamp")
    shifts = await _fetch_recent_mood_shifts(since_iso)
    return (len(shifts) >= MOOD_SHIFT_OVERFLOW_THRESHOLD, len(shifts))


async def maybe_synthesize_on_wake(session_slug: str | None = None) -> dict | None:
    """Called at the start of every wake event.

    Marks the wake, decides whether to synthesize, returns the mood (fresh
    or fetched) so the caller can inject it into the system prompt.

    Two triggers fire synthesis:
      1. Pressure-based — `pressure.should_synthesize()` (legacy gate).
      2. Shift-overflow — ≥N unread mood-shifts accumulated since the
         last carrier-wave. This catches the case where mood-shift
         substrate has visibly moved but mood-pressure hasn't crossed
         threshold (the gap between Apr 28 → May 5 was this exact case).
    """
    decision, reason = await pressure.should_synthesize()
    await pressure.mark_wake()

    if not decision:
        overflow, n = await _shift_overflow_decision()
        if overflow:
            decision = True
            reason = f"shift-overflow ({n})"

    if decision:
        log.info("mood synthesis firing (reason=%s)", reason)
        fresh = await synthesize_mood(session_slug=session_slug)
        if fresh:
            return fresh
        # synthesis failed — fall back to whatever's in the lake
    return await latest_mood()
