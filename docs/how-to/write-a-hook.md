---
title: How to write a hook
description: Add a shell script that fires on a Claude Code lifecycle event and writes deltas into Fathom. Pattern used by the three hooks fathom-connect installs.
audience: developer
quadrant: how-to
last_verified: 2026-04-24
owners: [addons/connect/hooks/, addons/connect/index.js]
---

# How to write a hook

A hook is a shell command Claude Code runs on a lifecycle event. When the event fires, Claude pipes a JSON payload into the command's stdin. The command does whatever it's going to do and exits. If the event is synchronous, Claude waits for the exit and can use the hook's stdout to modify the session. If async, Claude fires and forgets.

Fathom ships with three hooks installed by `fathom-connect`: crystal injection at `SessionStart`, recall surfacing on `UserPromptSubmit`, and delta capture on `UserPromptSubmit` and `Stop`. You can add your own for any other event you want to observe.

## Prerequisites

- Claude Code installed.
- Fathom running and an API token available.
- Familiarity with shell scripts and JSON on stdin.

## The event you hook into

Claude Code emits these lifecycle events today:

| Event | Fires on | Timing |
|---|---|---|
| `SessionStart` | First system context is built | Sync, runs before the session sees anything |
| `UserPromptSubmit` | User sends a message | Sync or async; sync hook's stdout gets appended to context |
| `Stop` | Assistant finishes a turn | Async |
| `PreToolUse` | Before a tool is called | Sync; can block or modify the call |
| `PostToolUse` | After a tool returns | Async |

For observation (write to the lake, don't block the user), use async. For injection (add context the model should see), use sync.

## Skeleton

```bash
#!/usr/bin/env bash
# my-hook — <what it does>

set -euo pipefail

export FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
export FATHOM_API_KEY="${FATHOM_API_KEY:-}"

INPUT=$(cat)

# Parse the incoming JSON. Python is a safe bet; jq works too.
eval "$(echo "$INPUT" | python3 -c "
import sys, json, shlex
d = json.load(sys.stdin)
print(f'export EVENT={shlex.quote(d.get(\"hook_event_name\", \"\"))}')
print(f'export SESSION_ID={shlex.quote(d.get(\"session_id\", \"unknown\"))}')
print(f'export CWD={shlex.quote(d.get(\"cwd\", \"\"))}')
")"

# Do whatever this hook does.
# ...
```

Every hook follows this pattern: read stdin, parse the JSON, act on the fields. The exact fields depend on the event.

## Example: log Bash tool uses

Say you want a delta every time the assistant runs a shell command. Use `PostToolUse`:

```bash
#!/usr/bin/env bash
# fathom-bash-log-hook — one delta per Bash tool call.

set -euo pipefail

export FATHOM_API_URL="${FATHOM_API_URL:-http://localhost:8201}"
export FATHOM_API_KEY="${FATHOM_API_KEY:-}"

INPUT=$(cat)

eval "$(echo "$INPUT" | python3 -c "
import sys, json, shlex
d = json.load(sys.stdin)
tool = d.get('tool_name', '')
if tool != 'Bash':
    exit(0)  # not a Bash call; nothing to do
args = d.get('tool_input', {})
print(f'export CMD={shlex.quote(args.get(\"command\", \"\"))}')
print(f'export SESSION={shlex.quote(d.get(\"session_id\", \"unknown\"))}')
")"

[ -z "${CMD:-}" ] && exit 0

curl -s -X POST "${FATHOM_API_URL}/v1/deltas" \
  -H "Authorization: Bearer ${FATHOM_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json, os
print(json.dumps({
    'content': os.environ['CMD'],
    'tags': ['claude-code', 'tool-use', 'tool:bash', f\"session:{os.environ['SESSION']}\"],
    'source': 'claude-code',
}))
")" > /dev/null
```

Drop that at `~/.fathom/hooks/fathom-bash-log-hook.sh`, `chmod +x`. Every Bash command the assistant runs now lands as a delta with the full command line, tagged for easy filtering.

## Register the hook

Hooks are registered in `~/.claude/settings.json` under the `hooks` key:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "FATHOM_API_URL='http://localhost:8201' FATHOM_API_KEY='sk-...' /home/you/.fathom/hooks/fathom-bash-log-hook.sh",
            "async": true
          }
        ]
      }
    ]
  }
}
```

Env vars go inline in the command string so Claude picks them up at exec time (not from Claude's own environment). Restart Claude Code to pick up new hook registrations.

For reference, the three Fathom hooks fathom-connect installs all live in `~/.claude/settings.json` the same way. Take a look at what's already there to see the shape.

## Sync vs async

| Mode | Use when | Cost |
|---|---|---|
| **async** (`"async": true`) | Observation: write a delta, measure something, log something | No blocking, but failures are silent |
| **sync** (default, or with `"timeout": N`) | Injection: add a recall result, add context | Blocks the user's interaction for up to `timeout` ms |

The recall hook is sync with an 8-second timeout. The delta hook is async. The crystal hook is sync with a 5-second timeout because the session genuinely can't start without it.

If your hook writes but doesn't modify context, make it async. Silent failure on write is fine; you don't want a temporarily-unreachable Fathom to break Claude.

## What your hook can return

On a **sync** hook, stdout is appended to Claude's context for that turn. This is how the recall hook surfaces memories: it writes the recalled deltas to stdout, and Claude reads them as if they were part of its instructions.

On an **async** hook, stdout is discarded. Log to stderr if you want to see output during development (Claude captures stderr to its own logs).

## Timing out gracefully

Claude Code enforces hook timeouts strictly. If your hook hasn't exited by `timeout` ms, Claude kills it. Your hook should therefore:

- Keep the critical path fast. One HTTP round-trip to Fathom is fine; a long pipeline isn't.
- Short-circuit early when the event doesn't match. The Bash-log example above exits 0 immediately if `tool != 'Bash'`.
- Put slow work (large computation, media downloads) behind an async hook, not a sync one.

## Test it by hand

Run the hook manually with a sample payload before registering it:

```bash
echo '{"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls /tmp"}, "session_id": "test-session"}' \
  | FATHOM_API_URL=http://localhost:8201 FATHOM_API_KEY=sk-... \
    ./fathom-bash-log-hook.sh
```

If the hook exits 0 and a delta appears in `/v1/deltas?tags_include=tool:bash`, it's working. Then register in `settings.json` and restart Claude Code.

## Things to know

- **Hooks run with your shell's environment, not Claude's.** Put env vars inline in the `command` string.
- **Async hooks fail silently.** During development, add `2>/tmp/hook.log` to the command and tail that file to see errors.
- **One hook can handle multiple events.** Check `EVENT` inside the script and branch on it. The Fathom delta hook does this to handle both `UserPromptSubmit` and `Stop`.
- **Don't write sensitive data in hook stdout on sync hooks.** Whatever you print gets appended to context the model sees. If that's a secret, you've just leaked it.
- **Hooks can compose.** Multiple hooks can be registered for the same event; Claude runs them all. They don't see each other; each gets the raw event payload.
- **Hooks can't write back to the lake with anyone's identity but the one in the bearer token.** If you want writes tagged as a specific contact, mint a scoped token for that contact and put it in the hook's env.
