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

## Decide which kind of helper you're writing

Two paths:

- **HTTP-backed tool.** The work lives behind an HTTP endpoint (new or existing). The tool is a thin wire map over that endpoint. Use this when the helper is "call a service and return the result" and when non-chat callers (direct HTTP clients, the MCP server, the CLI) should also be able to hit it. Registered in `LAKE_TOOLS` in `api/routes/lake.py`.
- **Inline tool.** The work happens inside the chat inference path, with no HTTP endpoint and no external dispatch. Use this for behaviors that only make sense inside a chat turn and need access to chat-scoped context (session id, participant, recent deltas). Registered in `CHAT_ONLY_TOOLS` in `api/_tool_schema.py`, with execution logic in `api/tools.py::execute()`.

Most helpers are HTTP-backed. Reach for inline only when the helper genuinely needs chat-turn context.

## Path A: HTTP-backed tool

### Step 1: build the endpoint

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

### Step 2: add to LAKE_TOOLS

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
- `response_kind` hints the client how to render the reply (`tree`, `text`, `write_receipt`, etc.). Pick an existing kind or add one in `api/_tool_explain.py` and the MCP formatter.

### Step 3: rebuild and test

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

## Path B: inline tool

For helpers that only run inside a chat turn.

### Step 1: add the schema

In `api/_tool_schema.py`, add an entry to `CHAT_ONLY_TOOLS`. This is raw OpenAI function-calling shape (no `endpoint`, no `scope`, no `surfaces`):

```python
{
    "type": "function",
    "function": {
        "name": "summarize_session",
        "description": "Summarize the current chat session into one paragraph.",
        "parameters": {
            "type": "object",
            "properties": {
                "style": {
                    "type": "string",
                    "enum": ["brief", "detailed"],
                    "default": "brief",
                },
            },
        },
    },
},
```

### Step 2: implement the handler

In `api/tools.py`, extend the `execute()` dispatcher to handle your tool:

```python
async def execute(tool_name: str, arguments: dict, session_id: str | None = None) -> dict:
    if tool_name == "summarize_session":
        return await _execute_summarize_session(arguments, session_id)
    # ...existing tools...

async def _execute_summarize_session(args: dict, session_id: str | None) -> dict:
    style = args.get("style", "brief")
    # Pull recent deltas for this session, synthesize, return.
    deltas = await delta_client.query(tags_include=[f"chat:{session_id}"], limit=50)
    # ...call the LLM or format the deltas...
    return {"summary": "..."}
```

The handler has access to `delta_client` for lake queries and anything else the inline-tool context gives it.

### Step 3: rebuild and test

```bash
docker compose build api && docker compose up -d api
```

Inline tools only show up in chat (not in MCP or CLI by design). Open the dashboard chat, ask something that would invoke your tool, watch the inference decide to call it.

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

- **The tool registry is one source of truth.** Chat, MCP, and CLI all read from `LAKE_TOOLS + CHAT_ONLY_TOOLS`. No parallel registry per surface.
- **The `surfaces` field is your visibility control.** Set `["chat"]` to hide from MCP and CLI; set `["mcp"]` to expose only to MCP hosts; etc.
- **Helpers are not agents.** A helper is a single capability fired on demand. An agent runs continuously (see [agents, routines, helpers, and hooks](../explanation/agents-routines-helpers-hooks.md)).
- **Hot reload doesn't cover tool registration.** Schema changes need a `docker compose up -d api` to take effect. Handler code inside existing tools usually hot-reloads; the registry read happens at startup.
- **The response reaches the LLM verbatim.** Whatever your endpoint (or inline handler) returns becomes the tool-call result the next inference step sees. Keep it concise and structured; JSON with a few named fields beats paragraphs of prose for the model.
- **Write a delta for any interesting call.** If a helper did real work (fetched weather, summarized a URL, drafted text), that work should leave a trace in the lake. Future recalls should be able to find "what was the forecast last Tuesday" or "when did I last ask you to draft that email" without re-running the helper.
- **MCP tool schemas are generated from this registry.** The `/v1/tools` endpoint feeds both `fathom-mcp` and the OpenAPI contract. Adding to `LAKE_TOOLS` automatically updates both.
