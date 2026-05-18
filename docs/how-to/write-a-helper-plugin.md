---
title: How to write a helper plugin
description: Add a named capability that chat, MCP clients, and the CLI can invoke. Maps onto Fathom's tool registry with two paths depending on whether your helper backs an HTTP endpoint or runs inline.
audience: developer
quadrant: how-to
last_verified: 2026-04-24
owners: [api/routes/lake.py, api/_tool_schema.py, api/tools.py]
---

# How to write a helper plugin

A helper (internally called a *tool* in the code) is a named capability Fathom's chat, MCP clients, and CLI can invoke. Fetching weather, summarizing a URL, drafting a response, calling a database: each is a helper. You add one by registering it in the tool catalogue. Chat's LLM sees it as an OpenAI-style function; MCP exposes it as an MCP tool; the CLI reaches it via `/v1/tools/<name>`.

This page covers both shapes a helper can take.

## Prerequisites

- A clone of the fathomdx repo.
- Familiarity with Python and FastAPI.
- A running dev stack (`docker compose up -d`) so you can iterate.

## How helpers are wired

A helper is an HTTP-backed tool. The work lives behind an endpoint
(new or existing). The tool is a thin wire map over that endpoint,
registered in `LAKE_TOOLS` in `api/routes/lake.py`. Every surface —
chat (via the threaded harness), MCP, and the CLI — reads from this
one registry. The `surfaces` field on each entry controls which
clients see the tool.

The previous "inline tool" / `CHAT_ONLY_TOOLS` path was retired
2026-05-18 along with the in-process chat-LLM path. If your helper
needs chat-scoped context (session id, recent deltas), expose it as
an HTTP endpoint and pass that context as an explicit argument or
look it up via `delta_client` inside the handler.

## Step 1: build the endpoint

Add (or reuse) a FastAPI route under `api/routes/`. Standard FastAPI: path, Pydantic body, auth dependency.

```python
# api/routes/weather.py
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from .. import auth

router = APIRouter()

class WeatherReq(BaseModel):
    location: str
    days: int = 1

@router.post("/v1/helpers/weather", dependencies=[Depends(auth.require_scope("helpers:invoke"))])
async def weather(req: WeatherReq):
    # ...fetch, format, return...
    return {"forecast": [...]}
```

Register the router in `api/server.py`:

```python
from .routes import weather
app.include_router(weather.router)
```

## Step 2: add to LAKE_TOOLS

In `api/routes/lake.py`, add an entry to the `LAKE_TOOLS` list. Required fields:

```python
{
    "name": "weather",
    "description": (
        "Get the weather forecast for a location. Specify days=1 for "
        "today's forecast or up to days=7 for a week ahead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "City name or postal code."},
            "days": {"type": "integer", "description": "How many days to forecast.", "default": 1},
        },
        "required": ["location"],
    },
    "endpoint": {"method": "POST", "path": "/v1/helpers/weather"},
    "scope": "helpers:invoke",
    "surfaces": ["chat", "mcp", "cli"],
    "response_kind": "text",
},
```

Key fields:

- `name`, `description`, `parameters` are the OpenAI function-calling shape the LLM sees.
- `endpoint` is the HTTP dispatch target. `request_map` (optional) renames parameter keys on the way to the endpoint.
- `scope` gates which tokens can invoke the tool. Define new scopes in `api/auth.py` if you need one.
- `surfaces` controls visibility. `chat` exposes to the inference loop. `mcp` exposes through `fathom-mcp`. `cli` exposes through the `fathom` CLI.
- `response_kind` hints the client how to render the reply (`tree`, `text`, `write_receipt`, etc.). Pick an existing kind or add one in the MCP formatter (`addons/mcp-node/index.js`).

## Step 3: rebuild and test

```bash
docker compose build api
docker compose up -d api
```

Verify the tool appears in the catalogue:

```bash
curl http://localhost:8201/v1/tools | jq '.tools[].name'
```

Your tool name should be in the list. Test the HTTP endpoint directly:

```bash
curl -X POST http://localhost:8201/v1/helpers/weather \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"location": "Oakland, CA", "days": 3}'
```

If that works, chat, MCP, and the CLI pick up the tool automatically. Ask Fathom chat "what's the weather in Oakland?" and the inference should invoke the tool without any further wiring.
## Tagging helper output

Whenever your helper writes a delta as a side effect (a weather forecast delta, a summary delta, a draft delta), tag it with:

- The helper name: `helper:weather`.
- The context it ran in: `chat:<slug>` if invoked in chat, `routine-id:<id>` if invoked from a routine.
- Any natural topic tags: `location:oakland`, `day:2026-04-25`, whatever fits the domain.

Good tagging means the helper's outputs compose with everything else in the lake. A future recall for "what did you tell me about the weather last week" finds them because they're tagged for it.

## Choosing names and scopes

- **Tool names are stable.** Once a helper ships and chat starts invoking it by name, renaming is a breaking change for downstream tool callers. Pick the right name up front.
- **Scope sensitive tools.** A helper that spends money (paid API, SMS, external write) should require a dedicated scope, not just `lake:write`. Mint tokens scoped to what they're allowed to do.
- **One tool per capability.** Don't bundle five unrelated actions into one tool with an `action` enum unless the actions genuinely belong to one concept (the `routines` tool is a good example of when bundling is correct; an `everything` tool would not be).

## Things to know

- **The tool registry is one source of truth.** Chat (via the threaded harness), MCP, and CLI all read from `LAKE_TOOLS`. No parallel registry per surface.
- **The `surfaces` field is your visibility control.** Set `["chat"]` to hide from MCP and CLI; set `["mcp"]` to expose only to MCP hosts; etc. The harness keeps its own cognitive primitives (semantic, expand, plan, lenses) outside this registry — they're harness-internal.
- **Helpers are not agents.** A helper is a single capability fired on demand. An agent runs continuously (see [agents, routines, helpers, and hooks](../explanation/agents-routines-helpers-hooks.md)).
- **Hot reload doesn't cover tool registration.** Schema changes need a `docker compose up -d api` to take effect. Handler code inside existing tools usually hot-reloads; the registry read happens at startup.
- **The response reaches the LLM verbatim.** Whatever your endpoint returns becomes the tool-call result the next inference step sees. Keep it concise and structured; JSON with a few named fields beats paragraphs of prose for the model.
- **Write a delta for any interesting call.** If a helper did real work (fetched weather, summarized a URL, drafted text), that work should leave a trace in the lake. Future recalls should be able to find "what was the forecast last Tuesday" or "when did I last ask you to draft that email" without re-running the helper.
- **MCP tool schemas are generated from this registry.** The `/v1/tools` endpoint feeds both `fathom-mcp` and the OpenAPI contract. Adding to `LAKE_TOOLS` automatically updates both.
