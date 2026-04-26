"""Feed alert pass — the piercing tier of the synthesis layer.

This pass scans recent activity for things that fall *outside* the
normal pattern of the lake. Not novel things, not interesting things —
deviating things. Sensor spikes, source-ran-silent gaps, integrity
events, unfamiliar identities writing into sensitive workspaces.

Cards from this pass route to ALERT level (the router auto-promotes
kind=alert). Silence is the expected outcome on most fires; false
alerts erode trust faster than missed ones.

Pure read/format logic. The fire entrypoint + write lives in
feed_loop.py alongside the per-line and drift paths.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from . import delta_client
from ._time import now as _now

# Sources that are pure noise from an alert standpoint — sysinfo
# heartbeats are *expected* to be stable, so a stable sysinfo stream
# isn't an alert. (A *missing* heartbeat for a long stretch *is* — that
# falls out of the source-counts comparison the LLM does.)
_NOISE_SOURCES = {"agent-heartbeat", "heartbeat"}

# Tags whose deltas aren't candidates for the alert pass — bookkeeping,
# our own outputs, ephemeral chat events.
_EXCLUDE_TAGS = [
    "chat-event",
    "feed-card",
    "feed-story",
    "feed-engagement",
    "silence",
]

# Recent window — what to scan for anomalies.
_ALERT_WINDOW_HOURS = 6
# Baseline window — what "normal" recently looked like, for comparison.
_BASELINE_WINDOW_DAYS = 7


def _format_source_counts(deltas: list[dict]) -> str:
    """Compact 'source: count (with-image)' summary so the LLM can see
    distribution shifts between recent and baseline windows."""
    counts: Counter[str] = Counter()
    for d in deltas:
        src = (d.get("source") or "?").lower()
        if src in _NOISE_SOURCES:
            continue
        counts[src] += 1
    if not counts:
        return "(empty window)"
    lines: list[str] = []
    for src, n in counts.most_common(20):
        lines.append(f"  {src:24s} {n}")
    return "\n".join(lines)


async def fetch_recent_window(window_hours: int = _ALERT_WINDOW_HOURS) -> list[dict]:
    """Recent activity — what we're scanning for anomalies."""
    cutoff = (_now() - timedelta(hours=window_hours)).isoformat()
    try:
        return await delta_client.query(
            tags_exclude=_EXCLUDE_TAGS,
            time_start=cutoff,
            limit=200,
        )
    except Exception:
        return []


async def fetch_baseline_window(window_days: int = _BASELINE_WINDOW_DAYS) -> list[dict]:
    """Baseline window for the LLM to compare against."""
    cutoff = (_now() - timedelta(days=window_days)).isoformat()
    try:
        return await delta_client.query(
            tags_exclude=_EXCLUDE_TAGS,
            time_start=cutoff,
            limit=500,
        )
    except Exception:
        return []


def format_recent_for_alert(deltas: list[dict], limit: int = 40) -> str:
    """Format recent deltas as candidate alert items. Compact, with
    source + timestamp + content excerpt so the LLM can spot deviations."""
    if not deltas:
        return "(recent window is empty)"
    lines: list[str] = []
    for d in deltas[:limit]:
        ts = (d.get("timestamp") or "")[:19]  # YYYY-MM-DDTHH:MM:SS
        src = (d.get("source") or "?")[:24]
        did = (d.get("id") or "")[:12]
        content = (d.get("content") or "").strip().replace("\n", " ")[:200]
        lines.append(f"  [{ts}] {src:24s} ({did}) {content}")
    return "\n".join(lines)


def build_alert_directive(
    recent_window_text: str,
    recent_summary: str,
    baseline_summary: str,
) -> str:
    return f"""\
You are running the ALERT pass on Fathom. Your job is to notice things \
in recent activity that fall OUTSIDE the normal pattern of the lake. \
Not interesting. Not novel. *Deviating*.

Examples of what an alert looks like:
  • A periodic source has gone silent for an unusual stretch (the \
baseline shows it active; the recent window shows it absent).
  • A sensor or metric shows a value far outside its recent rolling \
band.
  • A burst of error / warning content from a source that's normally \
quiet.
  • An unfamiliar identity wrote into a sensitive workspace.
  • A configuration change that wasn't expected.

=== RECENT ACTIVITY (last {_ALERT_WINDOW_HOURS}h, source counts) ===
{recent_summary}

=== BASELINE (last {_BASELINE_WINDOW_DAYS}d, source counts) ===
{baseline_summary}

=== RECENT DELTAS (the actual content of the recent window) ===
{recent_window_text}

Compare the recent window to the baseline. Look for sources that should \
be present but aren't. Look for content patterns that broke from \
typical. Look for individual deltas that simply don't belong.

If nothing genuinely deviates — and most fires of this pass should \
produce zero alerts — return `{{"cards": [], "reason": "<short>"}}`. \
False alerts cost trust faster than missed ones; when in doubt, skip.

Cap output at 5 cards. If more than 5 things deviate, prefer severity \
over volume.

Card schema (each card in the cards array):
  kicker — "ALERT · <short label>"
  title  — one-sentence summary of the deviation (≤120 chars)
  body   — 2-4 sentences. What changed, what the baseline was, why \
worth noticing.
  tail   — ≤8 words. Source/timestamp/metric pointer.
  body_image — empty (alerts are usually textual; only set if a \
candidate genuinely contains a relevant image)
  link   — empty unless the deviating source has a canonical URL

Respond with ONLY a JSON object {{"cards": [...]}}. No markdown fences."""
