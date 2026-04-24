---
title: How to pair another machine
description: Add a second (or third) machine to your Fathom. Same lake, same memory, more bodies and more places to run routines.
audience: developer
quadrant: how-to
last_verified: 2026-04-24
owners: [addons/agent/, api/routes/pairing.py]
---

# How to pair another machine

Fathom is one mind per person, but that mind can have presence on multiple machines at once. Each paired agent reports its own heartbeat, runs its own plugins, and accepts its own routine fires. The lake stays single; the bodies multiply.

This page walks through pairing a second machine after you've already done the first one (in [tutorial 3](../tutorials/03-fathom-does-things.md)).

## Prerequisites

- A working Fathom stack reachable from the new machine. If your stack is on `localhost:8201`, the new machine needs network access to that host (same LAN, ZeroTier, Tailscale, or a port-forwarded server).
- Node.js 20 or newer on the new machine.
- For routines on the new machine: kitty installed and Claude Code authenticated. Heartbeats and most other plugins work without these.

## Step 1: Open the pairing modal

In the Fathom dashboard, go to the **Agent** section. Click **Pair another machine**. You'll see the same OS tiles you used the first time: Linux, Mac, Windows. Pick the one that matches the new machine.

Name the new machine. Pick something distinct from the existing one: `studio`, `nas`, `phone-vps`, `home-server`. The name labels every delta the agent writes from this host on.

The modal generates a fresh single-use pair code, valid for ten minutes.

## Step 2: Run the pair command on the new machine

Copy the command. Open a terminal on the new machine. Paste and run:

```bash
npx fathom-agent init --pair-code pair_<short-lived-code> --api-url http://<your-fathom-host>:8201
```

If the new machine can reach Fathom by hostname or IP, pass it as `--api-url`. If you're using ZeroTier or Tailscale, use the ZeroTier/Tailscale IP. If you're going through a reverse proxy, use the public URL.

The agent installs from npm, redeems the pair code for a long-lived API key, writes the key to `~/.fathom/agent.json` on the new machine, and starts heartbeating.

## Step 3: Confirm both machines are online

Back in the dashboard's **Agent** section, both hosts now appear. Each one shows its name, online state, and the time of its last heartbeat.

Verify from a terminal:

```bash
curl 'http://localhost:8201/v1/deltas?source=fathom-agent&tags_include=agent-heartbeat&limit=10'
```

You'll see entries from both hosts, distinguishable by the `host:<name>` tag.

## Step 4: Keep the agent alive on the new machine

The `npx fathom-agent init` step paired the agent but didn't make it persistent. To keep it running:

```bash
# Foreground (good for testing)
npx fathom-agent run

# As a service (survives reboot)
npx fathom-agent install
```

`fathom-agent install` drops a systemd user unit on Linux, a launchd plist on macOS. After install, `systemctl --user status fathom-agent` (or `launchctl list | grep fathom`) confirms it's running.

## Step 5: Decide what each host does

By default, every paired host runs the heartbeat and sysinfo plugins. The dashboard's **Agent** section lets you enable additional plugins per host:

- **Vault** if there's an Obsidian vault at a known path on this machine.
- **Home Assistant** if this machine can reach a Home Assistant instance.
- **Kitty** if you want this host to be a routine target (requires kitty + Claude Code).

A common pattern: your laptop runs heartbeat + sysinfo + kitty (for routines), and a NAS or always-on server runs heartbeat + the Home Assistant plugin (so HA observations keep flowing even when the laptop is asleep).

## Routing routines to a specific host

Each routine spec has a `host` field. When set, the kitty plugin only spawns the routine on that exact host. When blank, any host with the kitty plugin will pick it up.

For routines that need to fire reliably, set `host` to your always-on machine. For routines that need access to local state on a specific machine (a particular `workspace` directory, a specific repo, a local DB), set `host` to that machine.

The full spec is in [routine-spec.md](../reference/routine-spec.md).

## What you don't gain

Pairing more machines doesn't give you more Fathoms. It gives one Fathom more bodies. The lake is shared. The chat history is shared. The identity crystal is shared. Nothing about your data is partitioned by host.

If what you want is *separate* Fathoms (one for you, one for someone else), pair them to separate stacks, each with its own `LAKE_DIR` and its own dashboard. That's a different thing entirely.

## Networking notes

- **Same LAN, no firewall in the way.** Use the LAN IP. `--api-url http://192.168.1.42:8201`.
- **ZeroTier or Tailscale.** Use the overlay IP. The agent and the API just need a route between them.
- **Reverse proxy (Caddy, Nginx, Cloudflare Tunnel).** Use the public URL. The agent speaks plain HTTP to the API; if you've put it behind TLS, the URL is `https://...`.
- **Nothing reachable by network.** You can't pair an agent over a one-way connection. If the agent host can't initiate to the API host, pairing won't complete.

## Troubleshooting

- **`npx fathom-agent init` errors with `connection refused`.** The agent host can't reach the API URL you passed. Test with `curl <api-url>/health` from the same terminal.
- **`pair code expired`.** Codes live ten minutes. Mint a new one.
- **Agent connects but the dashboard shows offline.** Heartbeat timing or clock skew. Wait two minutes. If still offline, check the agent's logs (`fathom-agent run` keeps them in the foreground).
- **Routines fire on the wrong host.** Set the `host` field on the spec to the machine you want.
- **Pairing succeeded but `~/.fathom/agent.json` isn't there.** It's stored relative to `$HOME` on the agent host. If `$HOME` was unusual when `npx fathom-agent init` ran, the file may be elsewhere. Check `npx fathom-agent run`'s startup output for the path.
