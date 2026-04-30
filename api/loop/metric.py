"""Cross-voice convergence metric — the settle signal.

After each voice tick, compute one similarity sample: how close THIS
voice's text is to the most recent thoughts of the OTHER voices in
this session. Append to a rolling 5-sample window; when the window's
spread (max-min) tightens below SETTLE_SPREAD_MAX, the parliament has
settled — voices have persuaded each other and the witness can fire
from consensus.

Lifted in shape from experiments/loop-experiment/worker/controller.py:
  * measure_cross_voice_convergence (line 398)
  * SETTLE_SPREAD_MAX = 0.12, SETTLE_WINDOW = 5

Two adaptations:
  * No HTTP — operates over the in-process puddle directly.
  * Distance is token-Jaccard, not semantic embedding distance. The
    experiment had embeddings on every puddle delta because its
    puddle was a separate Postgres+pgvector instance; ours is in-
    memory. Jaccard catches the same shape — voices saying overlapping
    things produce smaller distances, voices saying disjoint things
    produce bigger ones — at zero cost. When the embedding-based
    metric matters more (deadlock detection, cross-session resonance),
    the upgrade is local: swap _jaccard_distance for an embedding
    call. The settle threshold stays the same.

Each sample is also emitted as a `metric` puddle delta tagged with the
voice name. The convergence dots in the dashboard's status strip read
these to drive their x-positions in real time — without metrics they
fall back to a pure sine oscillator (the spike state).
"""

from __future__ import annotations

import json
import re

from .intents import CONVO_TAG
from .prompts import VOICES
from .puddle import puddle

# Settle thresholds — same values the experiment shipped.
SETTLE_SPREAD_MAX = 0.12
SETTLE_WINDOW = 5

# Metric TTL — short enough that stale metrics from a prior fire don't
# pollute the next session's settle window. The witness fires within
# the window so 5min is plenty.
METRIC_TTL_S = 5 * 60


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    """Lowercased word-tokens. Stopwords kept in — short voice takes
    benefit from the signal. Strip punctuation and collapse case."""
    return set(_TOKEN_RE.findall((text or "").lower()))


def _jaccard_distance(a: str, b: str) -> float:
    """1 - Jaccard similarity. 0 = identical; 1 = no shared tokens."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 0.0
    if not ta or not tb:
        return 1.0
    return 1.0 - len(ta & tb) / len(ta | tb)


def measure_cross_voice_convergence(
    *,
    text: str,
    voice_name: str,
    session_tag: str,
    voice_names: list[str] | None = None,
) -> float | None:
    """Mean distance from `text` to the most recent thought of each
    OTHER voice in this session. Returns None if no other voice has
    spoken yet — the settle window doesn't start until the parliament
    has at least one cross-voice take to compare against.

    `voice_names` is the active set the convener picked for this fire.
    Falls back to the canonical trimurti when the caller omits it
    (back-compat for any non-supervisor caller); the supervisor always
    passes the convener's verdict.voices through.
    """
    if voice_names is None:
        voice_names = [v["name"] for v in VOICES]
    other_names = [n for n in voice_names if n != voice_name]
    if not other_names:
        return None
    distances: list[float] = []
    for other in other_names:
        thoughts = puddle.query(
            tags_include=[CONVO_TAG, session_tag, "thought", f"voice:{other}"],
            limit=2,
        )
        for t in thoughts:
            distances.append(_jaccard_distance(text, t.get("content") or ""))
    if not distances:
        return None
    # Top-3 closest matches across all other voices — Riley-style "this
    # take is near a couple of recent things others said," which is
    # what convergence actually looks like.
    distances.sort()
    top = distances[:3]
    return sum(top) / len(top)


async def emit_metric(
    *,
    session_tag: str,
    voice_name: str,
    distance: float,
) -> None:
    """Write a `metric` delta into the puddle so the dashboard's
    convergence dots can read real data instead of the sine fallback.
    """
    await puddle.write(
        content=json.dumps({"distance": distance, "voice": voice_name}),
        tags=[
            CONVO_TAG, session_tag,
            "metric", f"voice:{voice_name}",
        ],
        source="metric",
        ttl_seconds=METRIC_TTL_S,
    )


def settle_window_check(
    samples: list[float],
    *,
    spread_max: float | None = None,
) -> tuple[bool, float | None]:
    """Returns (settled, level). Settled when the last SETTLE_WINDOW
    samples span less than `spread_max` (or SETTLE_SPREAD_MAX when
    not specified). `level` is the mean of that window — recorded as
    the settle level, not used as a gate.

    `spread_max` (Phase 4b): the supervisor passes a session-aware
    threshold computed from recent metric history. Hard-problem
    sessions (where past spreads consistently ran wide) get a looser
    bar; quiet sessions (where convergence has been tight) get a
    tighter one. Falls back to the static default when no value is
    supplied — preserves existing behavior for legacy/test callers.
    """
    threshold = SETTLE_SPREAD_MAX if spread_max is None else max(0.01, spread_max)
    if len(samples) < SETTLE_WINDOW:
        return False, None
    window = samples[-SETTLE_WINDOW:]
    spread = max(window) - min(window)
    if spread < threshold:
        return True, sum(window) / len(window)
    return False, None


# Phase 4b — session-aware settle threshold drift.
#
# How recently to look when computing the drifted threshold. Two hours
# is enough to capture a working-session arc; older spreads aren't
# representative of the current cadence (a hard-problem morning
# shouldn't permanently loosen the threshold for a quiet evening).
_DRIFT_LOOKBACK_HOURS = 2

# Minimum spreads needed before drift kicks in. Below this we use the
# static SETTLE_SPREAD_MAX default — small samples are too noisy to
# trust as a signal about current cadence.
_DRIFT_MIN_SAMPLES = 8

# Drift cap — how far the threshold can move from the static default.
# 50% in either direction (so 0.06–0.18 around 0.12) is a meaningful
# range without letting outliers run the loop into deadlock or
# false-settle.
_DRIFT_MAX_FACTOR = 1.5
_DRIFT_MIN_FACTOR = 0.5


def session_aware_spread_max() -> float:
    """Compute a drifted SETTLE_SPREAD_MAX based on recent puddle
    metric deltas. Returns the static default when not enough recent
    samples exist to drift confidently.

    Hard-problem hours (median spread runs high) loosen the threshold
    so the parliament isn't hammering itself against an unreachable
    convergence. Quiet hours (median spread runs low) tighten it so
    we don't flag small early ripples as settled when there's actually
    more deliberation to do.

    Reads from the puddle (session-process-scoped). Across-session
    drift would require lake-durable metrics; this is intentionally
    in-session because what's "hard right now" is a within-process
    signal, not a longitudinal one.

    Soft-fails to the static default on any error.
    """
    from datetime import UTC, datetime, timedelta

    try:
        since = (
            datetime.now(UTC) - timedelta(hours=_DRIFT_LOOKBACK_HOURS)
        ).isoformat()
        rows = puddle.query(
            tags_include=["metric"],
            time_start=since,
            limit=200,
        )
    except Exception:
        return SETTLE_SPREAD_MAX

    if not rows or len(rows) < _DRIFT_MIN_SAMPLES:
        return SETTLE_SPREAD_MAX

    spreads: list[float] = []
    for d in rows:
        content = d.get("content") or ""
        if not content:
            continue
        # Metric deltas store the per-voice convergence distance as
        # JSON {"distance": float, "voice": str}. Parse forgivingly.
        try:
            payload = json.loads(content)
            dist = float(payload.get("distance", 0.0))
            spreads.append(dist)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

    if len(spreads) < _DRIFT_MIN_SAMPLES:
        return SETTLE_SPREAD_MAX

    spreads.sort()
    median = spreads[len(spreads) // 2]
    # Drift toward median — if past spreads ran 0.20, threshold loosens
    # toward that; if they ran 0.04, it tightens. Clamp by factor so
    # the threshold can't drift wildly.
    drifted = SETTLE_SPREAD_MAX * 0.5 + median * 0.8
    floor = SETTLE_SPREAD_MAX * _DRIFT_MIN_FACTOR
    ceil = SETTLE_SPREAD_MAX * _DRIFT_MAX_FACTOR
    return max(floor, min(ceil, drifted))
