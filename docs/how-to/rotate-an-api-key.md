---
title: How to rotate an API key
description: Mint a new token, point clients at it, revoke the old one. Covers agents, Claude Code connections, and any integration using a bearer token from Settings → API Keys.
audience: operator
quadrant: how-to
last_verified: 2026-04-24
owners: [api/routes/auth.py]
---

# How to rotate an API key

When a token leaks, a host gets compromised, or you just want to cycle credentials on a schedule, rotating an API key is the same four-step dance: mint a new one, update whoever uses it, confirm the new one works, revoke the old one.

## Prerequisites

- Admin access to the Fathom dashboard (the bootstrap contact, or any contact with the `tokens:manage` scope).

## Step 1: List what's out there

In the dashboard, open **Settings → API Keys**. Every active token appears with its name, scope, created date, and last-used timestamp. The one you want to rotate is in that list.

From the CLI:

```bash
curl -H "Authorization: Bearer <admin-token>" http://localhost:8201/v1/tokens
```

Each row has a `token_id`. You'll need it in step 4.

## Step 2: Mint the replacement

In the dashboard: click **Create new key**. Give it a name that matches what you're rotating (`claude-code-laptop`, `agent-nas`, `openwebui-integration`, etc.). Pick the same scopes the old token has. Copy the token when it appears. **You won't see it again.**

From the CLI:

```bash
curl -X POST http://localhost:8201/v1/tokens \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "claude-code-laptop", "scopes": ["lake:read", "lake:write"]}'
```

Response carries the raw token string in `token`. Grab it; it won't be retrievable later.

## Step 3: Update whoever was using the old one

Where the old token lived depends on what it was for. The common cases:

**Claude Code connection.** Rerun `npx fathom-connect` on the affected machine:

```bash
npx fathom-connect
```

Pick Claude Code, paste the new token. Connect overwrites the old entry in `~/.claude.json` and `~/.claude/settings.json`. Restart Claude Code.

**Paired agent.** Agents store their token at `~/.fathom/agent.json` on the agent host. Re-pair:

```bash
# on the Fathom host, mint a new pair code in the dashboard's Agent section,
# then on the agent host:
npx fathom-agent init --pair-code pair_<new-code>
```

This overwrites `~/.fathom/agent.json` with the new token. `fathom-agent run` picks up the new credentials on next restart.

**Claude Desktop / Cursor / other MCP hosts.** Rerun `npx fathom-connect` and pick the right host; it writes the new token into the host's MCP config.

**Custom integrations.** Wherever your code reads `FATHOM_API_KEY` (env var, config file, secret manager), update it.

## Step 4: Confirm the new token works

For each client you updated, verify it can reach the lake:

```bash
# on the client's host
curl -H "Authorization: Bearer <new-token>" http://localhost:8201/v1/stats
```

A `200` with a stats object means the token is accepted. A `401` means something in step 3 didn't land; go back and re-check.

## Step 5: Revoke the old token

Only after step 4 passes for every client that was using the old token.

In the dashboard: find the old token in **Settings → API Keys** and click **Revoke**. The dashboard confirms; click through. The token is immediately invalidated.

From the CLI:

```bash
curl -X DELETE http://localhost:8201/v1/tokens/<old-token-id> \
  -H "Authorization: Bearer <admin-token>"
```

Any client still using the old token now starts getting `401 Unauthorized`. If you missed a client in step 3, it becomes visible here.

## Rotating the admin token

The bootstrap admin token is special: it's the one that created the first contact. You can't revoke it without first minting a replacement admin token and making sure you have it.

Process:

1. Mint a new admin-scope token via `POST /v1/tokens` (the new token replaces the old one in your terminal's environment).
2. Verify the new admin token with `/v1/auth/me`.
3. Use the new admin token to revoke the old one.

Don't revoke the last admin token. There's no back-door recovery; you'd have to restore from a backup taken before the revocation.

## Rotating on a schedule

If you want tokens to rotate automatically, wrap the above in a routine:

```bash
# prompt for a rotation routine, running quarterly
```

A monthly or quarterly rotation is reasonable for personal use. More frequently than that is probably more operational overhead than it's worth for a single-operator lake.

## Things to know

- **Tokens are bearer tokens.** Anyone holding one does what it can do. No second factor. Rotate immediately on compromise.
- **Revoked tokens fail fast.** Clients get a clear 401, not mysterious silent failures.
- **`~/.fathom/agent.json` and `~/.claude.json` are the two most common storage spots.** Check them both when rotating.
- **The old token stays in the lake's audit.** Revocation changes its state but doesn't erase its history. You can still query "what did token X do before it was revoked?" for forensic purposes.
- **Scope the new token tightly.** If an integration only needs to read, give it `lake:read` and not `lake:write`. Least-privilege beats broad-scope every time.
- **Backups predating the rotation still reference the old token.** If you restore a backup from before a rotation, the old token was live then; clients that held it would work again. For that reason, revoking isn't enough by itself for a true compromise story; the fix is "rotate and keep the lake clean of the compromised token's writes if they were malicious."
