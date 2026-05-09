---
title: How to wire up helpers (claude-code, ACP adapters)
description: Mint a helper-scoped token, configure the agent's plugins, and let Fathom dispatch tasks to claude-code via kitty or to other agents over ACP.
audience: developer
quadrant: how-to
last_verified: 2026-05-08
owners: [api/routes/helpers.py, addons/agent/plugins/]
---

# How to wire up helpers

A *helper* (in this doc — not to be confused with the LLM-callable
"helper tools" registered in `LAKE_TOOLS`) is a host-side capability
Fathom can dispatch work to. The agent advertises one or more
`(host, role)` pairs via heartbeat. Fathom's harness picks one when it
calls the `dispatch_helper` tool, and the matching plugin on that host
picks up the dispatch and runs it.

The two helper plugins shipped today:

- **kitty** — spawns a real claude-code session in a kitty terminal
  window. Advertises `helper-role:claude-code`. The user can watch the
  work and intervene.
- **acp** — a generic dispatcher for adapters that speak the [Agent
  Client Protocol](https://agentclientprotocol.com) over stdio. Each
  configured target advertises its own role
  (`openclaw`, `codex`, etc.).

Both go through the same `/v1/helpers/<host>/inbox` endpoint with a
host-bound, scope-narrowed *helper token* — a different credential
from the agent's lake-write `api_key`.

## Prerequisites

- A paired agent on the host (see [the QUICKSTART](../../QUICKSTART.md)).
- Admin access to a token with `tokens:manage` scope (any dashboard
  session token has this).
- For kitty: kitty + Claude Code installed and on PATH.
- For ACP targets: whatever package the adapter ships in (e.g.
  `@openclaw/acp-bridge`, `codex-acp`).

## 1. Mint a helper token

Helper tokens are scoped to one host. They grant `helper` scope only —
no lake-read, no lake-write — and the API enforces that the token's
`helper_host` matches the path host on every request.

```bash
ADMIN=fth_<your-admin-token>
HOST=myras-fedora-laptop  # whatever you named the host at pair time

curl -s -X POST \
  -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"agent@$HOST\"}" \
  http://localhost:8201/v1/admin/helpers/$HOST/tokens
```

Response:

```json
{
  "token": "fth_8njLaUNUgd4rPWzS8eWZVtt4afqCqj3e9ZjEmXEZ",
  "id": "f45adaffa40e",
  "name": "agent@myras-fedora-laptop",
  "scopes": ["helper"],
  "helper_host": "myras-fedora-laptop",
  ...
}
```

Save the `token` value — it's only shown once.

## 2. Add `helper_token` to agent.json

Edit `~/.fathom/agent.json` on the host machine. Add `helper_token` at
the top level alongside `api_key`:

```json
{
  "api_url": "http://localhost:8201",
  "api_key": "fth_<lake-scope-token>",
  "helper_token": "fth_<the-helper-token-you-just-minted>",
  "host": "myras-fedora-laptop",
  "plugins": {
    "heartbeat": { "enabled": true },
    "kitty": { "enabled": true }
  }
}
```

Restart the agent (`fathom-agent run` or `fathom-agent install`).
The kitty plugin will start polling
`/v1/helpers/<host>/inbox?role=claude-code` immediately.

## 3. Verify kitty/claude-code

Ask Fathom in chat to dispatch a small task to claude-code. The
harness should call `dispatch_helper(host=<host>, role="claude-code", task=...)`,
draft a proposal, and you'll see it in the dashboard's bell. Approve.

A kitty window should open with claude. The lake will accumulate
`helper-reply` deltas tagged
`task-corr:<corr>` + `kind:helper-update` (and finally
`kind:helper-complete` + `task-complete`) as the work progresses.

## 4. (Optional) Add ACP targets

A fresh agent install ships an `acp` block already in your
`agent.json` with the plugin disabled and a couple of example
`targets` (also disabled). Flip the flags to opt in. Each target
advertises its own `helper-role`:

```json
"plugins": {
  "kitty": { "enabled": true },
  "acp": {
    "enabled": true,
    "poll_interval_ms": 3000,
    "targets": [
      {
        "enabled": true,
        "role": "openclaw",
        "command": "podman",
        "args": ["exec", "-i", "openclaw", "openclaw", "acp", "--session", "agent:main:main"],
        "description": "OpenClaw — multi-channel chat-routing agent (ACP bridge over its gateway)"
      },
      {
        "enabled": false,
        "role": "codex",
        "command": "npx",
        "args": ["-y", "codex-acp"],
        "description": "OpenAI Codex coding agent"
      }
    ]
  }
}
```

Per-target `enabled: false` means the target is config-visible but
not advertised — handy for shipping examples or temporarily
silencing a role without deleting its config.

Restart the agent. Enabled roles show up in the next heartbeat as
`helper-role:<role>`. The harness's HELPERS block lists all available
`(host, role)` pairs alphabetically:

```
HELPERS — agents currently online that can receive a `dispatch_helper` task:
  · claude-code @ myras-fedora-laptop — shell, file edits, git, web via headed browser
  · openclaw    @ myras-fedora-laptop — OpenClaw — multi-channel chat-routing agent
```

### OpenClaw setup notes

The `openclaw acp` bridge connects to the OpenClaw Gateway over
WebSocket. Two pieces of config inside the OpenClaw container need
to be set so the bridge can authenticate:

```bash
podman exec openclaw openclaw config set gateway.remote.url 'ws://127.0.0.1:18789/ws'
podman exec openclaw openclaw config set gateway.remote.password '<your-gateway-password>'
```

Without `--session agent:main:main` the bridge mints a fresh
ACP-only session per dispatch and OpenClaw's main agent never sees
it (no model invocation). Pin the bridge to the main session so
both Fathom's ACP traffic and OpenClaw's chat UI share a single
conversation thread.

> **Don't add `claude-code-acp` as an ACP target.** Claude Code lives
> under the kitty plugin (or analogous OS-specific terminal-spawner
> plugins) so the user gets a visible window they can intervene in.
> ACP is reserved for adapters whose value is the structured
> protocol — agents without a chat window worth watching, or remote-only
> agents.

### What ACP currently does and doesn't do

Phase 3.0 is **headless**: the ACP plugin refuses client-method
requests (`fs/read_text_file`, `terminal/create`,
`session/request_permission`) with method-not-found. Adapters that
need filesystem or shell tools to do useful work will get errors back.
The wire works; tool execution is a follow-up slice.

### Closure-followup chain

When you ask Fathom in chat to "dispatch task X to openclaw and
bring me the reply," the full chain is:

1. **Harness** drafts a `dispatch_helper` proposal and stamps it
   with `originating-channel` / `correlation` / `intent` so the
   chain knows which chat surface to come back to.
2. **Operator approves** the proposal in the dashboard's bell. The
   approve flow writes a `route:helper:<role>` dispatch delta with
   the `originating-*` tags forwarded.
3. **ACP plugin** picks up the dispatch from its inbox, spawns the
   adapter subprocess, runs the JSON-RPC dance
   (`initialize` → `session/new` → `session/prompt`), accumulates
   `agent_message_chunk` text, and emits a `kind:helper-complete`
   with `task-complete` carrying the model's reply.
4. **claude_code_watcher** pairs the corr to the helper session
   (via the `task-spawn` handshake the ACP plugin emits after
   `session/new`), sees the closure, and writes a thread row tagged
   `closure:true` + the inherited `originating-*`.
5. **Threaded harness** fires on the closure thread row, produces
   a chat-reply ("OpenClaw replies: …"), routes it back to the
   originating chat surface.

Same chain for kitty/CC closures.

## How dispatches are addressed

The address is always a `(host, role)` pair:

| Where it shows up | Form |
|---|---|
| Heartbeat tags | `helper-role:<role>` + `host:<host>` |
| Dispatch tags | `route:helper:<role>` + `host:<host>` + `helper-role:<role>` |
| HELPERS prompt block | `<role> @ <host> — <description>` |
| `dispatch_helper` tool | `dispatch_helper(host=..., role=..., task=...)` |
| Inbox endpoint | `GET /v1/helpers/<host>/inbox?role=<role>` |

Same role on different hosts is fine (kitty on Linux + a future
warp.js on Windows can both advertise `claude-code`). Same role on
the **same** host across two plugins is bad — they'd race for the
same dispatches. The agent logs a loud warning at startup if it
detects this.

## Troubleshooting

**Helper token returns 403 "Token is not authorized for this helper host".**
The token's `helper_host` doesn't match the path host. Mint a new
token bound to the correct host.

**Inbox returns the same dispatch on every poll.** A completion
marker (`task-complete`, `task-abandoned`, `kind:helper-complete`,
`kind:helper-error`) should be in the lake for that corr. If the
adapter or kitty plugin crashed without writing one, the inbox
keeps the dispatch live until the 24-hour lookback window expires.

**ACP adapter exits with `method not supported`.** Phase 3.0 is
headless — the adapter is asking for `fs/*` or `terminal/*` and
the plugin refuses. Confirm whether your use case actually needs
those tools, and either pick an adapter that runs without them or
wait for the tool-call routing slice to land.

**"Helper-role overlap detected" warning at agent startup.** Two
plugins on this host both claim the same role. Remove the duplicate
from one of them — same `(host, role)` pair from two plugins races.

## Related

- [QUICKSTART](../../QUICKSTART.md) — pair an agent on a host.
- [Agents, routines, helpers, and hooks](../explanation/agents-routines-helpers-hooks.md)
- [HTTP API reference](../reference/http-api.md) — full inbox endpoint shape.
