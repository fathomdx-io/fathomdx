#!/usr/bin/env bash
# Fathom recall hook — searches the lake and injects memories as context.
#
# Triggered on UserPromptSubmit. Two retrievals per prompt:
#   1. Semantic recall (POST /v1/search depth=shallow) — what the lake
#      thinks is relevant to this prompt.
#   2. Mailbox (GET /v1/deltas?tags_include=for-session:<id>) — deltas
#      explicitly addressed to this session by another writer (a sibling
#      claude, a routine, etc.). Watermarked per-session so each ping
#      surfaces exactly once.
#
# To send a ping to session X, write a delta tagged `for-session:X`.
# No new addressing protocol — just the tag.
#
# Env:
#   FATHOM_API_URL        — consumer API (default: http://localhost:8201)
#   FATHOM_API_KEY        — bearer token
#   RECALL_DEPTH          — "shallow" (default) or "deep"
#   RECALL_LIMIT          — results per step (default: 30)
#   RECALL_THRESHOLD      — shallow-mode distance cutoff (default: 0.35)
#   RECALL_MIN_PROMPT_LEN — skip prompts shorter than this (default: 10)
#   MAILBOX_WINDOW_HOURS  — first-run mailbox lookback (default: 24)
#   FATHOM_STATE_DIR      — watermark dir (default: ~/.fathom/state)
#
# Install: add to ~/.claude/settings.json hooks.UserPromptSubmit
#          with "timeout": 8000

set -euo pipefail

FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
FATHOM_API_KEY="${FATHOM_API_KEY:-}"
DEPTH="${RECALL_DEPTH:-shallow}"
LIMIT="${RECALL_LIMIT:-30}"
THRESHOLD="${RECALL_THRESHOLD:-0.35}"
MIN_LEN="${RECALL_MIN_PROMPT_LEN:-10}"
WINDOW_HOURS="${MAILBOX_WINDOW_HOURS:-24}"
STATE_DIR="${FATHOM_STATE_DIR:-$HOME/.fathom/state}"

INPUT=$(cat)

eval "$(echo "$INPUT" | python3 -c "
import sys, json, shlex
d = json.load(sys.stdin)
print(f'EVENT={shlex.quote(d.get(\"hook_event_name\", \"\"))}')
print(f'PROMPT={shlex.quote(d.get(\"prompt\", \"\"))}')
print(f'ASSISTANT={shlex.quote(d.get(\"last_assistant_message\", \"\"))}')
print(f'SESSION_ID={shlex.quote(d.get(\"session_id\", \"\"))}')
")"

[ "$EVENT" != "UserPromptSubmit" ] && exit 0
[ -z "$PROMPT" ] && exit 0
if [ "${#PROMPT}" -lt "$MIN_LEN" ]; then
    exit 0
fi

# Build query: recent assistant context + current prompt (richer embeddings)
QUERY=""
if [ -n "$ASSISTANT" ]; then
    QUERY="$(echo "$ASSISTANT" | head -c 500)

"
fi
QUERY="${QUERY}$(echo "$PROMPT" | head -c 1000)"

export DEPTH LIMIT THRESHOLD
SEARCH_BODY=$(python3 -c "
import json, os, sys
print(json.dumps({
    'text': sys.stdin.read().strip(),
    'depth': os.environ['DEPTH'],
    'limit': int(os.environ['LIMIT']),
    'threshold': float(os.environ['THRESHOLD']),
}))
" <<< "$QUERY")

CURL_AUTH=()
[ -n "$FATHOM_API_KEY" ] && CURL_AUTH=(-H "Authorization: Bearer ${FATHOM_API_KEY}")

RESULT=$(curl -sf -X POST "${FATHOM_API_URL}/v1/search" \
    -H "Content-Type: application/json" \
    "${CURL_AUTH[@]}" \
    -d "${SEARCH_BODY}" 2>/dev/null) || RESULT=""

# Mailbox: pings explicitly addressed to this session.
PINGS=""
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [ -n "$SESSION_ID" ]; then
    mkdir -p "$STATE_DIR"
    WATERMARK_FILE="$STATE_DIR/mailbox-seen-${SESSION_ID}"
    if [ -f "$WATERMARK_FILE" ]; then
        SINCE=$(cat "$WATERMARK_FILE")
    else
        SINCE=$(date -u -d "${WINDOW_HOURS} hours ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
              || date -u -v-${WINDOW_HOURS}H +%Y-%m-%dT%H:%M:%SZ)
    fi
    PINGS=$(curl -sf -G "${FATHOM_API_URL}/v1/deltas" \
        --data-urlencode "tags_include=for-session:${SESSION_ID}" \
        --data-urlencode "time_start=${SINCE}" \
        --data-urlencode "limit=20" \
        "${CURL_AUTH[@]}" 2>/dev/null) || PINGS=""
    # Advance watermark so each ping surfaces exactly once.
    echo -n "$NOW" > "$WATERMARK_FILE"
fi

# Bail if neither retrieval produced anything.
[ -z "$RESULT" ] && [ -z "$PINGS" ] && exit 0

python3 -c "
import json, os, sys

raw = sys.stdin.read().split('\\x1e', 1)
search_raw = raw[0] if raw else ''
pings_raw = raw[1] if len(raw) > 1 else ''

# Parse semantic recall.
as_prompt = ''
total = 0
if search_raw.strip():
    try:
        r = json.loads(search_raw)
        as_prompt = r.get('as_prompt') or ''
        total = int(r.get('total_count') or 0)
    except Exception:
        pass

# Parse mailbox pings (API returns a bare JSON array).
ping_deltas = []
if pings_raw.strip():
    try:
        p = json.loads(pings_raw)
        if isinstance(p, list):
            ping_deltas = p
        elif isinstance(p, dict):
            ping_deltas = p.get('deltas') or p.get('results') or []
    except Exception:
        pass

if not as_prompt and not ping_deltas:
    sys.exit(0)

parts = []
if ping_deltas:
    n = len(ping_deltas)
    parts.append(f'--- {n} ping{\"s\" if n != 1 else \"\"} for you ---')
    parts.append('Other writers tagged these deltas for this session. They surface once each.')
    parts.append('')
    for d in ping_deltas:
        src = d.get('source', 'unknown')
        ts = (d.get('created_at') or d.get('timestamp') or '')[:16]
        tags = ', '.join((d.get('tags') or [])[:5])
        content = (d.get('content') or '').strip()
        parts.append(f'[{src} · {ts} · {tags}]')
        parts.append(content)
        parts.append('')

if as_prompt:
    parts.append(f'--- You remember {total} things ---')
    parts.append('')
    parts.append(as_prompt)

context = '\\n'.join(parts).rstrip() + '\\n'

system_bits = []
if ping_deltas:
    system_bits.append(f'{len(ping_deltas)} ping{\"s\" if len(ping_deltas) != 1 else \"\"} for you')
if total:
    system_bits.append(f'You remember {total} things')

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'UserPromptSubmit',
        'systemMessage': ' · '.join(system_bits) or 'recall',
        'additionalContext': context,
    }
}))
" <<< "${RESULT}"$'\x1e'"${PINGS}"
