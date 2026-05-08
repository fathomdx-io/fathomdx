/**
 * OpenClaw — chat-style helper dispatch surface.
 *
 * Polls the delta lake for `route:helper:openclaw` cards targeted at
 * this host. For each new dispatch, sends the task to a configured
 * OpenClaw / Pi HTTP API and writes the reply back as a delta tagged
 * with the dispatch's correlation id so the witness can pick it up.
 *
 * This plugin is the network-native counterpart to kitty: kitty spawns
 * a terminal window, openclaw makes an HTTP call. Same `route:helper:<role>`
 * envelope, different transport.
 *
 * Dispatch delta shape (same as kitty's, just a different role):
 *   Tags:    [feed-card, route:helper:openclaw, host:<myhost>,
 *             to:helper:<corr>]
 *   Source:  witness
 *   Content: JSON payload whose body is the task prompt
 *
 * Reply shape:
 *   Tags:    [helper-reply, helper-role:openclaw, host:<myhost>,
 *             to:helper:<corr>, task-corr:<corr>]
 *   Source:  openclaw
 *   Content: the assistant message returned by the API
 *
 * Configuration (~/.fathom/agent.json under plugins.openclaw):
 *   enabled:   bool
 *   api_url:   base URL — e.g. "https://api.openclaw.example/v1" or
 *              "https://api.inflection.ai/external/api/inference" for Pi
 *   api_key:   bearer token (optional — empty for unauthenticated APIs)
 *   model:     model identifier the API expects (optional)
 *   shape:     "openai" (default) — POSTs an OpenAI-compatible
 *              chat-completions payload. Other shapes can be added
 *              later (anthropic, custom). Without a known API surface
 *              the openai shape is the safest assumption.
 *
 * State file (~/.fathom/openclaw-state.json) tracks the last-processed
 * delta timestamp so restarts don't re-fire historical dispatches.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, renameSync } from "fs";
import { homedir, hostname } from "os";
import { join, dirname } from "path";

const STATE_PATH = join(homedir(), ".fathom", "openclaw-state.json");
const DEFAULT_TIMEOUT_MS = 120 * 1000;

export const CONFIG_SHAPE = {
  api_url: {
    type: "string",
    required: true,
    help: "Base URL of the OpenClaw / Pi API (e.g. https://api.openclaw.example/v1).",
  },
  api_key: {
    type: "string",
    required: false,
    help: "Bearer token for the API (optional). Stored in agent.json, never broadcast in heartbeats.",
  },
  model: {
    type: "string",
    required: false,
    help: "Model identifier the API expects in the chat-completions payload.",
  },
  shape: {
    type: "string",
    required: false,
    help: "Wire shape — 'openai' (default) for OpenAI-compatible chat completions.",
  },
  poll_interval_ms: {
    type: "number",
    required: false,
    help: "How often to poll the lake for new dispatches (ms). Default: 3000.",
  },
  timeout_ms: {
    type: "number",
    required: false,
    help: "Per-dispatch HTTP timeout (ms). Default: 120000.",
  },
};

function loadState() {
  try {
    return JSON.parse(readFileSync(STATE_PATH, "utf8"));
  } catch {
    return { last_seen: new Date(Date.now() - 60_000).toISOString() };
  }
}

function saveState(state) {
  mkdirSync(dirname(STATE_PATH), { recursive: true });
  const tmp = STATE_PATH + ".tmp";
  writeFileSync(tmp, JSON.stringify(state, null, 2));
  renameSync(tmp, STATE_PATH);
}

function corrFromTags(tags) {
  for (const t of tags || []) {
    if (t.startsWith("to:helper:")) return t.slice("to:helper:".length);
  }
  return null;
}

function extractTaskBody(delta) {
  const raw = (delta.content || "").trim();
  if (!raw) return "";
  // Witness payloads are JSON envelopes — pull the body field. Plain
  // strings (older surfaces) are taken as-is.
  if (raw.startsWith("{")) {
    try {
      const obj = JSON.parse(raw);
      return (obj.body || obj.task || "").trim();
    } catch {
      return raw;
    }
  }
  return raw;
}

async function callOpenAIShape(config, prompt, signal) {
  const url = `${config.api_url.replace(/\/+$/, "")}/chat/completions`;
  const headers = { "content-type": "application/json" };
  if (config.api_key) headers.authorization = `Bearer ${config.api_key}`;

  const body = {
    model: config.model || "openclaw",
    messages: [{ role: "user", content: prompt }],
    stream: false,
  };

  const res = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status} ${res.statusText}: ${text.slice(0, 400)}`);
  }
  const data = await res.json();
  const reply = data?.choices?.[0]?.message?.content;
  if (typeof reply !== "string") {
    throw new Error(`unexpected response shape — no choices[0].message.content`);
  }
  return reply;
}

async function dispatchOne(delta, config, pusher, myHost) {
  const corr = corrFromTags(delta.tags);
  if (!corr) {
    console.warn(`  openclaw: dispatch ${delta.id?.slice(0, 8)} missing to:helper:<corr>`);
    return;
  }
  const prompt = extractTaskBody(delta);
  if (!prompt) {
    console.warn(`  openclaw: dispatch ${corr.slice(0, 12)} has empty body — skipping`);
    return;
  }
  if (!config.api_url) {
    console.error(
      `  openclaw: dispatch ${corr.slice(0, 12)} arrived but api_url is not configured. ` +
        `Set plugins.openclaw.api_url in ~/.fathom/agent.json.`,
    );
    pusher?.push?.({
      content: `[openclaw] dispatch ${corr} skipped — api_url not configured on ${myHost}`,
      tags: [
        "helper-reply",
        "helper-error",
        "helper-role:openclaw",
        `host:${myHost}`,
        `to:helper:${corr}`,
        `task-corr:${corr}`,
      ],
      source: "openclaw",
    });
    return;
  }

  const shape = (config.shape || "openai").toLowerCase();
  const timeoutMs = config.timeout_ms || DEFAULT_TIMEOUT_MS;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);

  console.log(`  openclaw: dispatch ${corr.slice(0, 12)} → ${config.api_url} (shape=${shape})`);

  let reply;
  try {
    if (shape === "openai") {
      reply = await callOpenAIShape(config, prompt, ctrl.signal);
    } else {
      throw new Error(`unsupported shape '${shape}' — only 'openai' is implemented`);
    }
  } catch (e) {
    clearTimeout(timer);
    console.error(`  openclaw: dispatch ${corr.slice(0, 12)} failed: ${e.message}`);
    pusher?.push?.({
      content: `[openclaw] dispatch ${corr} failed: ${e.message}`,
      tags: [
        "helper-reply",
        "helper-error",
        "helper-role:openclaw",
        `host:${myHost}`,
        `to:helper:${corr}`,
        `task-corr:${corr}`,
      ],
      source: "openclaw",
    });
    return;
  }
  clearTimeout(timer);

  pusher?.push?.({
    content: reply,
    tags: [
      "helper-reply",
      "helper-role:openclaw",
      `host:${myHost}`,
      `to:helper:${corr}`,
      `task-corr:${corr}`,
    ],
    source: "openclaw",
  });
  console.log(`  openclaw: dispatch ${corr.slice(0, 12)} replied (${reply.length} chars)`);
}

async function pollOnce(config, pusher, state) {
  const myHost = config.host || hostname().split(".")[0];
  let dispatches;
  try {
    dispatches = await pusher.query({
      tags_include: `route:helper:openclaw,host:${myHost}`,
      time_start: state.last_seen,
      limit: 50,
    });
  } catch (e) {
    console.error(`  openclaw: poll failed: ${e.message}`);
    return;
  }
  dispatches.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));
  for (const d of dispatches) {
    if (d.timestamp <= state.last_seen) continue;
    await dispatchOne(d, config, pusher, myHost);
    state.last_seen = d.timestamp;
  }
  if (dispatches.length) saveState(state);
}

export default {
  name: "OpenClaw",
  category: "runtime",
  icon: "🪝",
  description: "Dispatch tasks to an OpenClaw / Pi HTTP API.",
  helperRoles: [
    {
      role: "openclaw",
      description: "chat-synthesis, web reach, no filesystem",
    },
  ],
  defaults: {
    enabled: false,
    api_url: "",
    api_key: "",
    model: "",
    shape: "openai",
    poll_interval_ms: 3000,
    timeout_ms: DEFAULT_TIMEOUT_MS,
  },

  start(config, pusher) {
    const state = loadState();
    console.log(
      `  openclaw: polling lake for route:helper:openclaw dispatches (last seen: ${state.last_seen})`,
    );
    if (!config.api_url) {
      console.warn(
        `  openclaw: api_url is empty — dispatches will be rejected until configured in agent.json`,
      );
    }

    const tick = () => pollOnce(config, pusher, state).catch((e) =>
      console.error(`  openclaw: tick failed: ${e.message}`),
    );
    const timer = setInterval(tick, config.poll_interval_ms || 3000);
    tick();

    return {
      stop() {
        clearInterval(timer);
        saveState(state);
      },
    };
  },
};
