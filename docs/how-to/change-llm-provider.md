---
title: How to change LLM provider
description: Add or swap providers (Gemini, OpenAI, Anthropic, local Ollama / LM Studio / vLLM). Fathom can speak to multiple at once. Changes take effect on next request.
audience: operator
quadrant: how-to
last_verified: 2026-04-24
owners: [.env.example, api/providers.py, api/llm_config.py]
---

# How to change LLM provider

Fathom can speak to several LLM providers at the same time. Set credentials for any subset; the providers you've configured become available in **Settings → Models**, where you assign each task (chat loop, search planning, summarization, embeddings) to whichever model you prefer.

This page covers four provider configurations: Gemini, OpenAI, Anthropic, and a local OpenAI-compat server (Ollama, LM Studio, vLLM, llama.cpp).

## Where the config lives

Provider credentials live in the `.env` file at the root of your install (default `~/.fathom/src/.env`). The relevant block:

```bash
# Gemini — https://aistudio.google.com/apikey
GEMINI_API_KEY=

# OpenAI — https://platform.openai.com/api-keys
OPENAI_API_KEY=

# Anthropic — https://console.anthropic.com/settings/keys
ANTHROPIC_API_KEY=

# Local OpenAI-compat server (Ollama, LM Studio, vLLM, llama.cpp)
LOCAL_BASE_URL=
```

A blank entry means "this provider isn't configured." Providers without credentials are hidden from the Models tab. Set any combination.

## Switch from Gemini to OpenAI

Edit `.env`:

```bash
GEMINI_API_KEY=        # leave blank or remove
OPENAI_API_KEY=sk-...
```

Restart the api so the new env is picked up:

```bash
docker compose up -d
```

Compose detects the env change and recreates the api container. Open **Settings → Models** in the dashboard. OpenAI now appears with its default models; Gemini is gone. Reassign each task slot (chat, search, etc.) to OpenAI models.

## Add a second provider alongside the first

Edit `.env`:

```bash
GEMINI_API_KEY=AIza...     # keep
OPENAI_API_KEY=sk-...      # add
```

Restart:

```bash
docker compose up -d
```

Now both providers appear in **Settings → Models**. You can run chat on Gemini and embeddings on OpenAI, or any other split. Fathom doesn't care which provider does which task as long as both are reachable.

## Use a local model via Ollama

Install Ollama (https://ollama.com) on your host. Pull a model:

```bash
ollama pull llama3.1:8b
```

Confirm Ollama is listening:

```bash
curl http://localhost:11434/api/tags
```

Edit `.env`. The local provider speaks OpenAI-compat at `/v1/`, so the `LOCAL_BASE_URL` points at Ollama's API root with that path:

```bash
LOCAL_BASE_URL=http://host.docker.internal:11434/v1/
```

`host.docker.internal` works on macOS and Windows. On Linux, use your LAN IP or set up the Docker host alias yourself (compose can do this with `extra_hosts`). The reason: the api container needs a route to your host's Ollama, and `localhost` from inside the container is the container itself, not the host.

Restart:

```bash
docker compose up -d
```

In **Settings → Models**, the "local" provider appears. Default models suggested are `llama3.1:8b` for medium and `qwen2.5:32b` for hard. Override these to match what you've actually pulled.

No API key is needed for the local provider.

## Use LM Studio, vLLM, or llama.cpp server

Same pattern as Ollama. They all expose OpenAI-compat at `/v1/`. Set `LOCAL_BASE_URL` to whichever URL their server listens on:

```bash
# LM Studio default
LOCAL_BASE_URL=http://host.docker.internal:1234/v1/

# llama.cpp's --server default
LOCAL_BASE_URL=http://host.docker.internal:8080/v1/
```

Restart, configure the model name in **Settings → Models** to match what you've loaded.

## Use Anthropic

Anthropic's Claude API speaks OpenAI-compat on `/v1/`. Set:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Restart. The Anthropic provider appears with `claude-haiku-4-5` and `claude-sonnet-4-6` as defaults.

## Things to know

- **Provider changes need an api restart.** `docker compose up -d` is enough; the env reload happens during container recreation. Don't edit `.env` and expect it to take effect on a running container.
- **Models tab vs `.env`.** `.env` controls *which providers are configured*. The Models tab controls *which models do which task*. Both are real settings; don't conflate them.
- **The `LLM_PROVIDER` legacy config.** Pre-multi-provider installs used `LLM_PROVIDER` + `LLM_API_KEY` to point at a single backend. Still supported as a fallback when the per-provider key is blank, but new installs should use the per-provider keys instead. `LLM_PROVIDER=ollama` is normalized to `local` internally.
- **Embeddings are a separate slot.** If your embeddings model changes (different provider, different dimensions), past deltas stay embedded with the old model until they're re-embedded. Search across the boundary will be lossy until the re-embed finishes. The api logs the re-embed progress.
- **Quotas are real.** Gemini's free tier is generous but rate-limited. OpenAI bills per token. Anthropic bills per token. Local models cost VRAM but no money. Pick deliberately.
- **Mixed-provider setups are fine.** A common pattern: local for chat (privacy + cost), Gemini for search/summarization (free tier), OpenAI for embeddings (high-quality vectors). Settings → Models lets you wire each task however you like.
