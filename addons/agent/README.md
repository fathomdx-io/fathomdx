# fathom-agent

Local agent for the [Fathom](https://github.com/fathomdx-io/fathomdx) memory lake. Runs as a long-lived process on your machine, loads plugins, and pushes deltas to your Fathom API.

**Requires [Node.js](https://nodejs.org/) 18 or newer.** Install Node first if you don't have it — `npx` ships with Node.

```
npx fathom-agent
```

First run prompts for your API URL and key, then writes `~/.fathom/agent.json`. After that it just runs.

Grant **`lake:read`** and **`lake:write`** on the API key. If you'll use the `acp` plugin to receive helper dispatches from other Fathom hosts, also grant **`helper`** (host-bound; issued via `/v1/helpers/<host>/tokens`).

## What it does

Pure plugin runner. The agent itself does nothing — every behavior is a plugin. Built-in plugins under `plugins/`:

- **heartbeat** — periodic liveness deltas so the lake knows the host is up; also walks plugin metadata for the dashboard's host roster.
- **homeassistant** — bridges Home Assistant events into the lake.
- **kitty** — fires scheduled prompts (routines) as kitty terminal windows running claude-code.
- **sysinfo-linux** — periodic system metrics (load, memory, thermals).
- **vault** — watches a notes directory and pushes file changes as deltas.
- **acp** — Agent Coordination Protocol client; lets other Fathom hosts dispatch this one as a helper.
- **local-ui** — small HTTP UI on the local machine for plugin status.

Drop your own `.js` files into `~/.fathom/plugins/` to extend it. A plugin is a module that exports `{ name, start(config, pusher) }`.

## Configuration

`~/.fathom/agent.json`:

```json
{
  "api_url": "http://localhost:8201",
  "api_key": "fth_...",
  "plugins": {
    "homeassistant": { "url": "http://homeassistant.local:8123", "token": "..." },
    "vault": { "path": "/path/to/notes" }
  }
}
```

Environment variables `FATHOM_API_URL` and `FATHOM_API_KEY` override the config when set.

## Related

For interactive MCP host setup (Claude Code, Desktop, Cursor), see [`fathom-connect`](https://www.npmjs.com/package/fathom-connect). For terminal-side lake access, see [`fathom-delta-cli`](https://www.npmjs.com/package/fathom-delta-cli).

## License

MIT. See [LICENSE](./LICENSE).
