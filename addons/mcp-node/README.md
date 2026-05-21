# fathom-mcp

MCP server for the [Fathom](https://github.com/fathomdx-io/fathomdx) memory lake. Connects any MCP host — Claude Code, Claude Desktop, Cursor, or anything else speaking the protocol — to a self-hosted or cloud Fathom instance.

**Requires [Node.js](https://nodejs.org/) 18 or newer.** The MCP host launches `fathom-mcp` via `npx`, which ships with Node.

You probably want [`fathom-connect`](https://www.npmjs.com/package/fathom-connect) — that's the interactive installer that wires this server into your MCP host's config for you. This package is what the host launches once that's done.

## Manual setup

If you'd rather configure your MCP host by hand, add this block:

```json
{
  "mcpServers": {
    "fathom": {
      "command": "npx",
      "args": ["-y", "fathom-mcp"],
      "env": {
        "FATHOM_API_URL": "http://localhost:8201",
        "FATHOM_API_KEY": "fth_..."
      }
    }
  }
}
```

Generate the API key in your Fathom dashboard's Settings → API Keys page. Grant **`lake:read`** and **`lake:write`** at minimum — read powers `remember` / `recall` / `mind_*` / `see_image`, write powers `write` / `engage` / `propose_contact` / `mint_routine`.

## What it exposes

Tools are discovered from `GET /v1/tools` on your Fathom instance, scoped to your API key's permissions. The full surface includes:

- `remember` — semantic search over the lake.
- `recall` — structured filter by tag / source / time window.
- `deep_recall` — compositional multi-step recall plan.
- `write` — persist a delta.
- `engage` — affirm / refute / reply-to a delta.
- `introspect` — call Fathom directly; spawns a full multi-turn synthesis.
- `see_image` — fetch an image referenced by `media_hash`.
- `mind_stats` / `mind_tags` — lake totals and tag catalogue.
- `propose_contact` — surface an unknown person for review.
- `dispatch_helper` / `mint_routine` / `send_message` — agent coordination primitives.

The identity crystal is also exposed as an MCP resource.

## License

MIT. See [LICENSE](./LICENSE).
