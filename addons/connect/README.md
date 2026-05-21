# fathom-connect

One command to connect Claude Code, Claude Desktop, or Cursor to your Fathom memory lake.

**Requires [Node.js](https://nodejs.org/) 18 or newer.** Install Node first if you don't have it — `npx` ships with Node.

```
npx fathom-connect
```

You'll be asked where you're connecting, your Fathom API URL (defaults to `http://localhost:8201`), and an API key. The wizard tests the connection, writes the MCP server entry, and — for Claude Code — installs the hook scripts that inject the identity crystal, capture deltas, and run recall on every prompt.

## What it sets up

**Claude Code** — full experience.

- MCP server `fathom` written to `~/.claude.json` (user scope, all projects).
- Hooks installed to `~/.fathom/hooks/`:
  - `fathom-crystal-hook.sh` — `SessionStart`, injects the identity crystal.
  - `fathom-delta-hook.sh` — `UserPromptSubmit` + `Stop`, captures prompts and responses to the lake (async).
  - `fathom-recall-hook.sh` — `UserPromptSubmit`, surfaces relevant memories.
- Hook entries patched into `~/.claude/settings.json`.

**Claude Desktop / Cursor** — MCP only.

- MCP config written to the platform's `claude_desktop_config.json` (macOS / Windows / Linux paths handled).

**Other** — prints the JSON config block on stdout so you can paste it into any MCP-speaking host.

## Requirements

- A running Fathom instance you can reach over HTTP. See [fathomdx](https://github.com/fathomdx-io/fathomdx) for self-hosting (`docker compose up -d`).
- An API key from your Fathom dashboard's Settings → API Keys page. Grant **`lake:read`** and **`lake:write`** at minimum — the recall hook reads, the delta-capture hook writes.
- Node.js 18 or newer on the machine running the MCP host.

## How it relates to the other packages

`fathom-connect` is the installer. The MCP server it points hosts at is [`fathom-mcp`](https://www.npmjs.com/package/fathom-mcp), launched via `npx -y fathom-mcp`. You don't need to install `fathom-mcp` yourself — `npx` handles it on first launch.

If you want lake access from the terminal instead of (or in addition to) MCP, see [`fathom-delta-cli`](https://www.npmjs.com/package/fathom-delta-cli). If you want a background watcher that pushes local activity into the lake, see [`fathom-agent`](https://www.npmjs.com/package/fathom-agent).

## License

MIT. See [LICENSE](./LICENSE).
