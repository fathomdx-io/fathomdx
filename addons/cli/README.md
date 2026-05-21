# fathom-delta-cli

Search, write, and introspect your [Fathom](https://github.com/fathomdx-io/fathomdx) memory lake from the terminal. Same primitives as the MCP server, callable from shell scripts and pipelines.

**Requires [Node.js](https://nodejs.org/) 18 or newer.** Install Node first if you don't have it — `npx` ships with Node.

```
npx fathom-delta-cli remember "what did we decide about the auth rewrite"
```

Or install globally and invoke as `fathom`:

```
npm install -g fathom-delta-cli
fathom remember "..."
```

## Setup

Point it at your Fathom instance with two environment variables:

```
export FATHOM_API_URL=http://localhost:8201       # default
export FATHOM_API_KEY=fth_...                     # from Settings → API Keys
```

Grant **`lake:read`** and **`lake:write`** on the key — read powers `remember` / `recall` / `mind`, write powers `write` / `engage` / `propose_contact`.

Self-host with `docker compose up -d` from the [fathomdx](https://github.com/fathomdx-io/fathomdx) repo, or point at a cloud instance.

## Commands

```
fathom remember <query> [--shallow] [--limit N]
    Deep recall by default — composes a multi-step plan and walks the DAG.
    --shallow falls back to a single similarity search.

fathom write <content> [--tags a,b,c] [--source <name>] [--image <path>]
    Persist one delta. Tag consistently.

fathom recall [--tags ...] [--source ...] [--since 24h] [--limit N]
    Structured filter — no semantic search, just precise time/tag windows.

fathom deep_recall <query>
    Force the compositional planner explicitly.

fathom introspect <question>
    Spawn a full Fathom synthesis on the question. Multi-turn, expensive.
    Reach for remember first if a simple search would answer.

fathom engage <id> --kind affirms|refutes|reply-to [--note ...]
    Shape future recall ranking. Aliases: fathom affirm, fathom refute, fathom reply.

fathom see_image <media_hash>
    Print the image to a TTY that supports inline images (kitty, wezterm).

fathom propose_contact <name> [--note ...]
    Surface an unknown person for admin review.

fathom mind            # lake totals and coverage
fathom mind tags       # tag catalogue
fathom instructions    # the server-side MCP/CLI instructions block
```

Hidden aliases for muscle memory: `search` → `remember`, `query` → `recall`, `stats` → `mind`.

## Related

For Claude Code / Claude Desktop / Cursor integration, see [`fathom-connect`](https://www.npmjs.com/package/fathom-connect). For the underlying MCP server, see [`fathom-mcp`](https://www.npmjs.com/package/fathom-mcp).

## License

MIT. See [LICENSE](./LICENSE).
