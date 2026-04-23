#!/usr/bin/env bash
# Fathom crystal hook — runs on SessionStart, injects:
#   1. Identity crystal (who am I) — from /v1/crystal, with /v1/search fallback.
#   2. Birds-eye situational awareness (what's happening right now) —
#      aggregated from the last N minutes of deltas: source counts,
#      active claude-code sessions, top tags. Lets a fresh claude boot
#      knowing its siblings exist.
#
# Env:
#   FATHOM_API_URL       — consumer API (default: http://localhost:8201)
#   FATHOM_API_KEY       — bearer token from Settings → API Keys
#   BIRDSEYE_WINDOW_MIN  — birds-eye lookback window (default: 30)
#   BIRDSEYE_LIMIT       — max deltas pulled for aggregation (default: 500)
#
# Install: add to ~/.claude/settings.json hooks.SessionStart

set -euo pipefail

FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
FATHOM_API_KEY="${FATHOM_API_KEY:-}"
WINDOW_MIN="${BIRDSEYE_WINDOW_MIN:-30}"
BIRDSEYE_LIMIT="${BIRDSEYE_LIMIT:-500}"

INPUT=$(cat)
EVENT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hook_event_name',''))")
[ "$EVENT" != "SessionStart" ] && exit 0

export FATHOM_API_URL FATHOM_API_KEY WINDOW_MIN BIRDSEYE_LIMIT

python3 <<'PYEOF'
import datetime, json, os, sys, urllib.parse, urllib.request
from collections import Counter

API_URL = os.environ['FATHOM_API_URL'].rstrip('/')
API_KEY = os.environ.get('FATHOM_API_KEY', '')
WINDOW_MIN = int(os.environ.get('WINDOW_MIN', '30'))
LIMIT = int(os.environ.get('BIRDSEYE_LIMIT', '500'))

HEADERS = {'Content-Type': 'application/json'}
if API_KEY:
    HEADERS['Authorization'] = f'Bearer {API_KEY}'


def _fetch(url, *, data=None, method='GET', timeout=5):
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def crystal_block():
    # Primary: dedicated crystal endpoint.
    try:
        data = _fetch(f'{API_URL}/v1/crystal', timeout=3)
        text = data.get('text', '')
        if text:
            created = data.get('created_at', 'unknown')
            return f'Identity crystal (crystallized {created}):\n\n{text}'
    except Exception:
        pass
    # Fallback: search the lake.
    try:
        body = json.dumps({
            'text': 'identity crystal who am I',
            'depth': 'shallow',
            'limit': 1,
        }).encode()
        data = _fetch(f'{API_URL}/v1/search', data=body, method='POST', timeout=3)
        as_prompt = data.get('as_prompt', '')
        if as_prompt:
            return f'Identity crystal (from lake search):\n\n{as_prompt}'
    except Exception:
        pass
    return None


def birdseye_block():
    since_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=WINDOW_MIN)
    since = since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    qs = urllib.parse.urlencode({'time_start': since, 'limit': LIMIT})
    try:
        deltas = _fetch(f'{API_URL}/v1/deltas?{qs}', timeout=8)
    except Exception:
        return None
    if not isinstance(deltas, list) or not deltas:
        return None

    sources = Counter()
    sessions = Counter()
    tags_counter = Counter()
    for d in deltas:
        sources[d.get('source', 'unknown')] += 1
        for t in (d.get('tags') or []):
            if t.startswith('session:'):
                sid = t.split(':', 1)[1]
                sessions[sid[:8]] += 1
            elif t.startswith('chat:') or t.startswith('contact:') or t.startswith('host:'):
                continue  # noisy, skip from top-tags
            else:
                tags_counter[t] += 1

    lines = [
        f"--- What's happening right now (last {WINDOW_MIN} min · "
        f"{len(deltas)} delta{'s' if len(deltas) != 1 else ''}) ---",
        "",
        "Other sessions, agents, and sources are writing into the same lake. "
        "Use `recall` (with source/tag/time filters) to drill in when it matters; "
        "send a message to another claude session by writing a delta tagged "
        "`for-session:<id>` (it'll surface in their next recall).",
        "",
    ]
    src_str = ', '.join(f'{s} ({c})' for s, c in sources.most_common(8))
    lines.append(f"sources: {src_str}")
    if sessions:
        sess_str = ', '.join(f'{s} ({c})' for s, c in sessions.most_common(8))
        lines.append(f"claude-code sessions: {sess_str}")
    top_tags = [(t, c) for t, c in tags_counter.most_common(20) if c >= 2][:10]
    if top_tags:
        tag_str = ', '.join(f'{t} ({c})' for t, c in top_tags)
        lines.append(f"top tags: {tag_str}")
    return '\n'.join(lines)


parts = []
c = crystal_block()
if c:
    parts.append(c)
b = birdseye_block()
if b:
    parts.append(b)

if not parts:
    sys.exit(0)

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': '\n\n'.join(parts),
    }
}))
PYEOF
