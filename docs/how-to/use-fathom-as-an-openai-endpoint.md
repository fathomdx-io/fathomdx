---
title: How to use Fathom as an OpenAI endpoint
description: Point OpenAI-compatible clients at Fathom so third-party tools can talk to your mind without special integration.
audience: developer
quadrant: how-to
last_verified: 2026-04-24
owners: [api/server.py, api/chat_listener.py]
---

# How to use Fathom as an OpenAI endpoint

Fathom speaks the OpenAI chat-completions wire format at `POST /v1/chat/completions`. Any client that expects an OpenAI-compatible endpoint — Slack and Discord bots, n8n workflow nodes, drop-in chatbot widgets, LiteLLM, llm-cli — can point at Fathom and get replies back from your lake.

**This is the single most important thing to know before you start:** Fathom is OpenAI-*shaped*, not OpenAI-*semantic*. Same wire format, different substrate. The conversation lives in Fathom's memory, not in your client's `messages` array. Read the section below on [what Fathom honors](#what-fathom-honors-from-your-request) before you build anything non-trivial on top.

## Prerequisites

- A running Fathom stack reachable from your client machine.
- An API key from the dashboard with the `chat` scope (the default for new keys). **Settings → API Keys → Create**.
- An OpenAI-compatible client. Anything that lets you override `base_url` will work.

## Point your client at Fathom

Set two values on your client:

- `base_url` → `https://<your-fathom-host>/v1` (or `http://localhost:8201/v1` for a local stack)
- `api_key` → your Fathom API key

The official OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8201/v1",
    api_key="fth_...",
)

reply = client.chat.completions.create(
    model="fathom",
    messages=[{"role": "user", "content": "what did I decide about the feed drift threshold?"}],
)
print(reply.choices[0].message.content)
```

The `model` field is accepted but ignored — Fathom uses whichever provider/model is configured in your dashboard under **Settings → Models**. Passing `"fathom"` is a useful default so the intent is clear in your logs.

Streaming works the same way:

```python
stream = client.chat.completions.create(
    model="fathom",
    messages=[{"role": "user", "content": "..."}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

Fathom emits one content chunk per reply (it writes replies as whole deltas, not token-by-token), followed by a terminating chunk and `[DONE]`. Clients that concatenate chunks render correctly. Keep-alive SSE comments flow every 15 seconds so proxies don't drop the connection during long turns.

## Continuing a conversation

Each request either starts a new session or continues one. Sessions in Fathom are a tag (`chat:<slug>`), not a server-side conversation object — anyone who writes into the session's tag is a participant.

On the first call, omit `session_id`. Fathom mints one, returns it as an **extension field** on the response:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "session_id": "gruff-merry-dolphin",
  "choices": [...]
}
```

On subsequent calls, pass it back:

```python
reply = client.chat.completions.create(
    model="fathom",
    messages=[{"role": "user", "content": "follow-up question"}],
    extra_body={"session_id": "gruff-merry-dolphin"},
)
```

Most OpenAI SDKs have an `extra_body` (Python) or equivalent escape hatch for passing non-standard fields. If yours doesn't, fall back to raw HTTP — the request is plain JSON.

## What Fathom honors from your request

Fathom's chat loop re-orients from the lake every turn by session tag, not from the `messages` array you send. That shapes what parts of your payload actually do anything.

| Field | What Fathom does with it |
|---|---|
| `messages[-1]` where role is `user` | Persisted as a `participant:user` delta. This is what triggers Fathom to respond. |
| `messages[*]` where role is `system` | Persisted once per session as a `participant:client-system` delta (deduped by content hash). It enters the lake as a recorded wish — context Fathom can ground in, not a privileged directive. See [system messages](#system-messages) below. |
| `messages[*]` where role is `assistant`, `tool`, `function` | **Ignored.** Fathom's prior turns live in the lake, not in your client's replay. Doctoring these is inert. |
| `session_id` | Scopes the conversation. Must match a session you've written into before, or be omitted entirely (to mint a new one). |
| `stream` | If `true`, response comes as SSE chunks. Otherwise, blocking JSON. |
| `model`, `temperature`, `max_tokens` | Accepted for wire compatibility, currently ignored. Fathom uses its configured model. |

**Things OpenAI clients expect that Fathom does not do:**

- **No regenerate-from-here.** If your client sends back an edited `messages` array to branch the conversation, Fathom responds as if those edits never happened — because in the lake, they didn't.
- **No context-window management.** Fathom manages its own recall; you can't stuff history into the request to "remind" it.
- **No function/tool calling.** The `tools` parameter is accepted for wire compatibility but ignored. Fathom has its own tool surface (lake search, recall, write) used internally during a turn.
- **Reply latency matches a real conversation.** The request blocks until Fathom's chat listener picks up your delta (~3s) and completes a turn (5-30s typical, up to 120s for tool-heavy turns). That's not a bug — it's the shape of conversing with a mind, not calling a stateless completion API.

## System messages

OpenAI clients put identity/behavior directives in the `system` slot and expect the server to honor them. Fathom does something softer.

The first time a client sends a particular system message in a session, Fathom persists it as a `participant:client-system` delta — a durable note that the client asked for that framing. Subsequent re-sends of the same system content are deduped and silently dropped. Fathom's chat loop sees the system delta as recent session context (along with its own identity crystal, sediment, and the user message) and chooses whether to ground in it.

What this means in practice:

- System messages **are recorded** in the session's lake tag and contribute to the context Fathom reads.
- System messages **do not override Fathom's identity.** You cannot tell Fathom "you are a helpful pirate assistant" and expect a different mind to pick up the phone. Fathom is a persistent self; what you send is a request Fathom may or may not honor, the way a person might humor a stranger's framing.
- A request with only a system message and no user message will persist the system delta and return an empty completion. No inference fires until a user message lands.

If you need a persona that isn't Fathom, don't use Fathom. Use a stateless LLM.

## Good fits, bad fits

**Good fits** — workloads that match "send text, get text back from the same ongoing mind":

- Slack, Discord, Mastodon bots that bridge a channel to Fathom.
- n8n, Zapier, Make workflow nodes that want a chat step.
- Site chat widgets where users talk to Fathom about your content.
- CLI tools like `llm` with `--model openai:fathom`.
- LiteLLM routing groups that include Fathom as one of several providers.

**Bad fits** — workloads that treat the API as stateless or mutable:

- Agent harnesses that branch a conversation and replay with edits.
- Anything building a regenerate-from-here or conversation-tree UI.
- Pipelines that expect token-by-token streaming with per-token logprobs.
- Anything depending on `function_call` / `tool_calls` round-trips.

For those, skip the OpenAI endpoint and use Fathom's native lake API (`/v1/search`, `/v1/deltas`, `/v1/sessions`) directly.

## Troubleshooting

**The request hangs forever.** There's a 120-second cap; past that, Fathom returns an empty completion with `finish_reason: "timeout"`. If every request times out, check the API server logs — `docker compose logs api` — for chat-listener errors. The listener polls every 3 seconds; if it's not running, no replies land.

**I get an empty reply with `finish_reason: "stop"`.** Fathom chose silence. The chat loop writes `<...>` when it has nothing to say; the endpoint surfaces that as an empty assistant message. This is normal — a mind doesn't always have to speak.

**My system message doesn't seem to do anything.** That's the point — see [system messages](#system-messages). If you want the content of your system message to shape the reply, put the same guidance in the `user` message instead (or include it inline, e.g. `"Please answer concisely: ..."`). The user slot is where directives actually move the needle, because they're part of what Fathom is directly responding to.

**The same client sends the system message every turn — is it piling up in the lake?** No. Identical system content within a session is deduped by hash on the server side; only the first copy lands as a delta.

## Related

- `/v1/sessions/{id}` — read back the full session history as a message list.
- `/v1/search` — search the lake directly without going through a chat turn.
- [How to connect Claude Code to Fathom](connect-claude-code.md) — richer integration for AI coding sessions, with MCP tools and hooks.
