#!/usr/bin/env bash
# Fathom crystal hook — runs on SessionStart, injects:
#   1. Agent voice / tool guide (canonical block from /v1/agent-instructions)
#      so a fresh claude knows the lake exists, what tools to call, and how
#      to speak from memory. Source of truth lives on the API; updates ship
#      without re-publishing this script.
#   2. Identity crystal (who am I) — from /v1/crystal, with /v1/search fallback.
#   3. Birds-eye situational awareness (what's happening right now) —
#      aggregated from the last N minutes of deltas: source counts,
#      active claude-code sessions, top tags. Lets a fresh claude boot
#      knowing its siblings exist.
#
# If the API is unreachable (no /health), we inject a clear "API down,
# your mcp__fathom__* tools will fail" warning instead — never a baked
# fallback that pretends the lake is available.
#
# Env:
#   FATHOM_API_URL       — consumer API (default: http://localhost:8201)
#   FATHOM_API_KEY       — bearer token from Settings → API Keys
#   FATHOM_SURFACE       — instructions surface key (default: claude-code)
#   BIRDSEYE_WINDOW_MIN  — birds-eye lookback window (default: 30)
#   BIRDSEYE_LIMIT       — max deltas pulled for aggregation (default: 500)
#
# Install: add to ~/.claude/settings.json hooks.SessionStart

set -euo pipefail

FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
FATHOM_API_KEY="${FATHOM_API_KEY:-}"
FATHOM_SURFACE="${FATHOM_SURFACE:-claude-code}"
WINDOW_MIN="${BIRDSEYE_WINDOW_MIN:-30}"
BIRDSEYE_LIMIT="${BIRDSEYE_LIMIT:-500}"

INPUT=$(cat)
EVENT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hook_event_name',''))")
[ "$EVENT" != "SessionStart" ] && exit 0

SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))")

export FATHOM_API_URL FATHOM_API_KEY FATHOM_SURFACE WINDOW_MIN BIRDSEYE_LIMIT SESSION_ID

python3 <<'PYEOF'
import datetime, json, os, sys, urllib.parse, urllib.request
from collections import Counter

API_URL = os.environ['FATHOM_API_URL'].rstrip('/')
API_KEY = os.environ.get('FATHOM_API_KEY', '')
SURFACE = os.environ.get('FATHOM_SURFACE', 'claude-code')
WINDOW_MIN = int(os.environ.get('WINDOW_MIN', '30'))
LIMIT = int(os.environ.get('BIRDSEYE_LIMIT', '500'))
SESSION_ID = os.environ.get('SESSION_ID', '')

HEADERS = {'Content-Type': 'application/json'}
if API_KEY:
    HEADERS['Authorization'] = f'Bearer {API_KEY}'


def _fetch(url, *, data=None, method='GET', timeout=5):
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def api_is_up():
    try:
        urllib.request.urlopen(f'{API_URL}/health', timeout=2)
        return True
    except Exception:
        return False


def api_unreachable_block():
    return (
        f"--- ⚠ Fathom API unreachable at {API_URL} ---\n\n"
        f"The fathom MCP server can't be reached. Your `mcp__fathom__*` tools "
        f"(remember, recall, write, engage, mind_stats, propose_contact) will "
        f"fail until the API is back up. The identity crystal, birds-eye "
        f"situational awareness, and tool guide that normally appear here "
        f"are all unavailable for the same reason.\n\n"
        f"To check: `curl {API_URL}/health` should return 200. If it doesn't, "
        f"the fathomdx api container probably needs to start "
        f"(`podman compose up -d api` from the fathomdx repo)."
    )


def instructions_block():
    qs = urllib.parse.urlencode({'surface': SURFACE})
    try:
        data = _fetch(f'{API_URL}/v1/agent-instructions?{qs}', timeout=3)
        text = data.get('text', '')
        if text:
            return text
    except Exception:
        pass
    return None


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
    # Fallback: search the lake (crystal endpoint missing or empty, but
    # the lake itself may still have an older crystal sitting in it).
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


def session_block():
    # Tell the LLM its claude-code session id. User/assistant turns are
    # already written by the delta hook with `session:<id>` tags — the
    # dashboard's session aggregator unions those in alongside consumer-
    # api chats, so this conversation appears in the sidebar without any
    # extra tagging from the LLM. The session id is needed when the LLM
    # wants to name the conversation.
    if not SESSION_ID:
        return None
    return (
        "--- This conversation ---\n"
        f"claude-code session id: {SESSION_ID}\n"
        "\n"
        "Your user/assistant turns are captured to the lake automatically "
        "by the delta hook, and the dashboard's sessions list picks them "
        f"up via the `claude-code` + `session:{SESSION_ID}` tags.\n"
        "\n"
        "To give this session a readable title, call `rename_session` with "
        f"name=<short title> and session_id={SESSION_ID}.\n"
        "--- End this conversation ---"
    )


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


# Single health check up front. If API is down, all three blocks would
# fail anyway — surface that explicitly so the agent knows its tools are
# offline rather than silently producing an empty SessionStart context.
if not api_is_up():
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'systemMessage': 'Fathom API unreachable — MCP tools offline',
            'additionalContext': api_unreachable_block(),
        }
    }))
    sys.exit(0)

parts = []
for fn in (instructions_block, session_block, crystal_block, birdseye_block):
    block = fn()
    if block:
        parts.append(block)

if not parts:
    sys.exit(0)

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': '\n\n'.join(parts),
    }
}))
PYEOF
