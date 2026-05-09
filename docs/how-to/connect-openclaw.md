---
title: How to connect OpenClaw to Fathom
description: Pair an OpenClaw agent as a dispatchable helper, so Fathom can hand it tasks over the Agent Client Protocol and read its replies back into the lake.
audience: developer
quadrant: how-to
last_verified: 2026-05-08
owners: [addons/agent/plugins/acp.js, addons/agent/acp_client.js, api/routes/helpers.py]
---

# How to connect OpenClaw to Fathom

[OpenClaw](https://openclaw.com) is a multi-channel chat-routing agent. Pairing it with Fathom turns it into a `(host, role)` helper Fathom's harness can dispatch tasks to: a question lands in chat, the harness drafts a `dispatch_helper` proposal, you approve it, and OpenClaw's reply flows back into the same chat thread.

The wire is the [Agent Client Protocol](https://agentclientprotocol.com) (ACP) — newline-delimited JSON-RPC over stdio. Fathom's `acp` plugin spawns the bridge subprocess on demand; OpenClaw's bridge connects back to the OpenClaw Gateway over WebSocket.

## Prerequisites

- A working Fathom stack with a paired agent on the host (see [Pair another machine](pair-another-machine.md)).
- OpenClaw installed and running — typically as a podman container named `openclaw` with its gateway listening on `ws://127.0.0.1:18789/ws`.
- A *helper token* for the host. If you don't have one yet, follow [Wire up helpers](wire-up-helpers.md) up through "Mint a helper token" — the rest of that page is the generic plumbing this guide builds on.

## 1. Configure OpenClaw's gateway bridge

OpenClaw's `acp` subcommand connects to its own gateway over WebSocket. Two pieces of config inside the OpenClaw container have to be set so the bridge can authenticate:

```bash
podman exec openclaw openclaw config set gateway.remote.url 'ws://127.0.0.1:18789/ws'
podman exec openclaw openclaw config set gateway.remote.password '<your-gateway-password>'
```

Replace `<your-gateway-password>` with whatever you set during OpenClaw setup. (The OpenClaw dashboard's default password works as the gateway password if you haven't changed it.)

Without `gateway.remote.password`, the bridge prints `gateway password missing` and refuses to start. Without the URL, it can't reach the gateway at all.

## 2. Pin the bridge to OpenClaw's main session

The ACP bridge can either mint a fresh ACP-only session per dispatch (default) or pin itself to an existing OpenClaw session. The default is the wrong choice here — the orphaned ACP session never reaches OpenClaw's main agent, so no model invocation happens and you get back an empty reply.

Pin the bridge to OpenClaw's main session by passing `--session agent:main:main` on the command line. You'll do this in the next step in `agent.json`.

## 3. Add the ACP target to `~/.fathom/agent.json`

Edit `~/.fathom/agent.json` on the host where the agent runs. Enable the `acp` plugin and add an `openclaw` target:

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
      }
    ]
  }
}
```

The `helper_token` field at the top level of `agent.json` is what authorizes the agent to read this host's inbox and write replies back. If it's missing, the agent's ACP plugin will fail to poll. See [Wire up helpers](wire-up-helpers.md) § "Mint a helper token".

Restart the agent (`fathom-agent run` if foregrounded, or `systemctl --user restart fathom-agent` if installed as a service). On startup you should see:

```
acp: polling /v1/helpers/<host>/inbox for roles [openclaw]
```

## 4. Verify in Fathom

In the dashboard's chat, ask Fathom to hand something off to OpenClaw — for example:

> Ask openclaw what the current price of gold is and bring me the reply.

The harness should call `dispatch_helper(host=<host>, role="openclaw", task=...)`, draft a proposal, and surface it as a card in your feed with an Approve button. Approve it.

Within a few seconds you'll see the dispatch card morph into a reply card showing OpenClaw's response. The full chain:

1. **Harness** drafts a `dispatch_helper` proposal stamped with the originating chat surface.
2. **You approve** in the dashboard. The approve flow writes a `route:helper:openclaw` dispatch delta.
3. **ACP plugin** picks up the dispatch from its inbox, spawns `podman exec -i openclaw openclaw acp --session agent:main:main`, runs the JSON-RPC dance (`initialize` → `session/new` → `session/prompt`), and accumulates the streamed `agent_message_chunk` text.
4. When the prompt completes, the plugin posts a single `kind:helper-complete` reply with the assembled body and a `task-complete` tag.
5. **`claude_code_watcher`** pairs the corr to the helper session, sees the closure, and threads the reply back to the originating chat surface.

## Troubleshooting

**Empty reply, or `[done · end_turn]` with no model output.** The ACP bridge minted a fresh session that bypassed OpenClaw's main agent. Check that `--session agent:main:main` is in the `args` array in `agent.json`.

**Agent log says `gateway password missing`.** Set the password inside the OpenClaw container with `podman exec openclaw openclaw config set gateway.remote.password '<password>'` (step 1 above).

**Inbox returns the same dispatch on every poll.** A completion marker (`kind:helper-complete`, `task-complete`, or `kind:helper-error`) should be in the lake for that corr. If the bridge crashed without writing one, the inbox keeps the dispatch live until the 24-hour lookback window expires. Restart the agent and check `acp:` log lines for an exception.

**Reply card shows raw protocol metadata (`{"sessionUpdate":"usage_update",...}`) instead of clean text.** That's a stale `acp.js` from before the per-update writes were dropped. Pull the latest source repo and restart the agent. The current plugin only writes the assembled closure body, never per-chunk updates.

**Dispatch never reaches OpenClaw.** Check the agent's startup log for `acp: polling ... for roles [openclaw]`. If the role list is empty, the per-target `enabled` flag is false or the role name doesn't match what the harness picked. The `HELPERS` block in the harness's prompt lists the live `(host, role)` pairs alphabetically — `openclaw @ <host>` should be there once the agent's heartbeat lands.

## Related

- [Wire up helpers](wire-up-helpers.md) — the generic helper-roles plumbing this page builds on.
- [Pair another machine](pair-another-machine.md) — provision a host's agent if you haven't yet.
- [Agents, routines, helpers, and hooks](../explanation/agents-routines-helpers-hooks.md) — where helpers fit in the broader topology.
