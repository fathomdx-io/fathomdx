# Quickstart

Self-host Fathom on a single Linux machine. About five minutes from clone to running.

## Prerequisites

- Docker or Podman with the compose plugin
- Git
- An API key from one of: Google AI Studio (Gemini), OpenAI, or a local Ollama install

## Install

```bash
git clone https://github.com/myrakrusemark/consumer-fathom.git
cd consumer-fathom
cp .env.example .env
```

Open `.env` and set `FATHOM_API_KEY`. If you want a provider other than Gemini, change `FATHOM_PROVIDER` too (`openai` or `ollama`).

## Run

```bash
docker compose up -d
```

First boot builds three images and pulls postgres. Give it a minute or two. When it's done, the stack is listening on:

| URL | What |
|---|---|
| http://localhost:8201 | API and UI. This is where you go. |
| http://localhost:4246 | Delta-store (the lake's HTTP API) |
| http://localhost:4260 | Source-runner (external source poller) |

Everything is bound to 127.0.0.1 by default.

## Verify

```bash
curl http://localhost:4246/health   # {"status":"ok"}
curl http://localhost:8201/v1/stats # delta counts, should start at zero
```

Then open `http://localhost:8201` in a browser and say hello.

## From here, the dashboard drives

Everything else happens inside the dashboard. Pair a local agent, connect an MCP host (Claude Code, Claude Desktop, Cursor), wire up hooks, add sources to poll, mint API tokens. The dashboard walks you through each step and hands you the commands to run when something has to happen on your host.

If you prefer the terminal, the Node tools in `agent/`, `cli/`, `mcp-node/`, and `connect/` are the same flows unwrapped. They all talk to the API at `http://localhost:8201`.

## Updating

```bash
git pull
docker compose build
docker compose up -d
```

## Teardown

```bash
docker compose down            # stop, keep data
docker compose down -v         # stop and drop the lake (deletes pgdata volume)
rm -rf data/                   # drop delta-store media and source-runner state
```

## Troubleshooting

**`connection refused` on port 8201.** Give the API another 10 seconds. It waits for postgres and delta-store to come up. `docker compose logs api` will tell you what it's waiting on.

**`401 Unauthorized` from the UI.** You set `DELTA_API_KEY` but the api container and delta-store container have different values. They need to match, or both need to be blank.

**Gemini quota errors.** The free tier is fine for trying it out but rate-limits aggressively. If you hit the ceiling, grab an OpenAI key and set `FATHOM_PROVIDER=openai`.

**Podman on SELinux systems.** If bind mounts fail with permission errors, add `:z` to each volume mount in `docker-compose.yml`, or run `chcon -Rt container_file_t data/`.
