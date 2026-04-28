#!/usr/bin/env node
/**
 * Fathom MCP server — generic adapter that reads tools from the API.
 *
 * Connects to any Fathom instance (self-hosted or cloud). Discovers
 * available tools from GET /v1/tools, filtered by the token's scopes.
 * Exposes the identity crystal as an MCP resource.
 *
 * Environment:
 *   FATHOM_API_URL  — base URL (default: http://localhost:8201)
 *   FATHOM_API_KEY  — bearer token from Settings → API Keys
 *
 * Usage:
 *   npx fathom-mcp
 *   FATHOM_API_URL=https://api.hifathom.com FATHOM_API_KEY=fth_... npx fathom-mcp
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const API_URL = (process.env.FATHOM_API_URL || "http://localhost:8201").replace(/\/$/, "");
const API_KEY = process.env.FATHOM_API_KEY || "";

// ── HTTP helpers ─────────────────────────────────

function authHeaders(json = true) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  if (API_KEY) h["Authorization"] = `Bearer ${API_KEY}`;
  return h;
}

// ── Result formatting (keyed by tool's response_kind) ────────────────

function formatMomentList(data) {
  const items = data.results || data.deltas || (Array.isArray(data) ? data : []);
  if (!items.length) return "No moments surfaced.";

  const lines = [`${items.length} moments:\n`];
  for (const raw of items) {
    const d = raw.delta || raw;
    const ts = (d.timestamp || "").slice(0, 16);
    const tags = (d.tags || []).slice(0, 4).join(", ");
    const src = d.source || "";
    const content = (d.content || "").slice(0, 400);
    const media = d.media_hash ? ` [image: ${d.media_hash}]` : "";
    lines.push(`[${ts} · ${src} · ${tags}]${media}\n${content}\n`);
  }
  return lines.join("\n");
}

function formatRecall(data) {
  const total = data.total_count || 0;
  const tree = data.tree || [];
  if (!total || !tree.length) return "No moments surfaced.";
  const header = `${total} moments across ${tree.length} step(s):\n`;
  return header + "\n" + (data.as_prompt || "");
}

function formatStats(data) {
  return `Your mind: ${data.total ?? "?"} moments, ${data.embedded ?? "?"} embedded (${data.percent ?? "?"}% coverage)`;
}

function formatTags(data) {
  // /v1/tags returns {tag: count}. Top 50 keeps context bounded.
  if (!data || typeof data !== "object") return "No tags.";
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return "No tags.";
  const top = entries.slice(0, 50);
  const lines = [`${entries.length} tags (top ${top.length}):\n`];
  for (const [tag, count] of top) lines.push(`  ${tag} (${count})`);
  return lines.join("\n");
}

function formatByKind(kind, data) {
  switch (kind) {
    case "tree":
      return formatRecall(data);
    case "moments":
      return formatMomentList(data);
    case "stats":
      return formatStats(data);
    case "tags":
      return formatTags(data);
    case "write_receipt":
      return `Written. ID: ${data.id || "?"}`;
    default:
      return JSON.stringify(data, null, 2).slice(0, 2000);
  }
}

// ── Tool execution ───────────────────────────────

// Substitute {placeholder} segments in a URL path template using the
// caller's args. Consumed args are returned separately so they don't
// leak into the body or query string as duplicate fields.
function applyPathTemplate(pathTemplate, args) {
  const names = [...pathTemplate.matchAll(/\{(\w+)\}/g)].map((m) => m[1]);
  let path = pathTemplate;
  const consumed = new Set();
  for (const name of names) {
    const val = args[name];
    if (val == null) throw new Error(`missing path param: ${name}`);
    path = path.replace(`{${name}}`, encodeURIComponent(String(val)));
    consumed.add(name);
  }
  const remaining = {};
  for (const [k, v] of Object.entries(args)) if (!consumed.has(k)) remaining[k] = v;
  return { path, remaining };
}

// Returns a single MCP content block: `{type:"text",text}` or
// `{type:"image",data,mimeType}`. The CallToolRequestSchema handler
// wraps it into `{content: [block]}`.
async function executeTool(toolDef, args) {
  const { method, path: pathTemplate } = toolDef.endpoint;
  const requestMap = toolDef.request_map || {};
  const { path, remaining } = applyPathTemplate(pathTemplate, args || {});

  const mapped = {};
  for (const [k, v] of Object.entries(remaining)) {
    if (v == null) continue;
    mapped[requestMap[k] || k] = v;
  }

  let r;
  if (method === "POST") {
    r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders(true),
      body: JSON.stringify(mapped),
    });
  } else {
    const params = {};
    for (const [k, v] of Object.entries(mapped)) {
      params[k] = Array.isArray(v) ? v.join(",") : String(v);
    }
    const qs = Object.keys(params).length ? "?" + new URLSearchParams(params) : "";
    r = await fetch(`${API_URL}${path}${qs}`, { headers: authHeaders(false) });
  }
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);

  const kind = toolDef.response_kind || "json";

  if (kind === "image") {
    const buf = Buffer.from(await r.arrayBuffer());
    const mimeType = r.headers.get("content-type") || "image/webp";
    return { type: "image", data: buf.toString("base64"), mimeType };
  }

  const data = await r.json();
  return { type: "text", text: formatByKind(kind, data) };
}

// ── MCP server ───────────────────────────────────

const MCP_SURFACE = "mcp";
const FALLBACK_INSTRUCTIONS =
  "You have a Fathom lake of memories. Call remember before answering anything " +
  "about the past; write when you learn something; read the fathom://crystal " +
  "resource at the start of every conversation.";

async function fetchInstructions() {
  try {
    const r = await fetch(
      `${API_URL}/v1/agent-instructions?surface=${encodeURIComponent(MCP_SURFACE)}`,
      { headers: authHeaders(false) }
    );
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    const data = await r.json();
    return (data.text || "").trim() || FALLBACK_INSTRUCTIONS;
  } catch (e) {
    console.error(`Could not load instructions from ${API_URL}: ${e.message}`);
    return FALLBACK_INSTRUCTIONS;
  }
}

async function main() {
  // Load tool definitions from the API, scoped to the MCP surface so
  // chat-only tools (routines, explain) never appear here.
  let tools = [];
  try {
    const r = await fetch(`${API_URL}/v1/tools?surface=${encodeURIComponent(MCP_SURFACE)}`, {
      headers: authHeaders(false),
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    const data = await r.json();
    tools = data.tools || [];
  } catch (e) {
    console.error(`Could not load tools from ${API_URL}: ${e.message}`);
    process.exit(1);
  }

  const toolMap = {};
  for (const t of tools) toolMap[t.name] = t;

  const instructions = await fetchInstructions();

  const server = new Server(
    { name: "Fathom", version: "2.1.0" },
    {
      capabilities: { tools: {}, resources: {} },
      instructions,
    }
  );

  // Tools — dynamic from /v1/tools
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: tools.map((t) => ({
      name: t.name,
      description: t.description,
      inputSchema: t.parameters || { type: "object", properties: {} },
    })),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const toolDef = toolMap[name];
    if (!toolDef) {
      return { content: [{ type: "text", text: `Unknown tool: ${name}` }] };
    }
    try {
      const block = await executeTool(toolDef, args || {});
      return { content: [block] };
    } catch (e) {
      return { content: [{ type: "text", text: `Error: ${e.message}` }] };
    }
  });

  // Resources — identity crystal
  server.setRequestHandler(ListResourcesRequestSchema, async () => ({
    resources: [
      {
        uri: "fathom://crystal",
        name: "Identity Crystal",
        description:
          "Fathom's identity — a first-person synthesis of who this mind is. Read this at the start of every conversation for persistent context.",
        mimeType: "text/plain",
      },
    ],
  }));

  server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
    const { uri } = request.params;
    if (uri === "fathom://crystal") {
      try {
        const r = await fetch(`${API_URL}/v1/crystal`, { headers: authHeaders(false) });
        if (r.ok) {
          const data = await r.json();
          const text = data.text || "No crystal generated yet.";
          const created = data.created_at || "unknown";
          return {
            contents: [
              {
                uri,
                mimeType: "text/plain",
                text: `Identity crystal (crystallized ${created}):\n\n${text}`,
              },
            ],
          };
        }
      } catch {
        /* fall through to the "no crystal" fallback below */
      }
      return {
        contents: [
          {
            uri,
            mimeType: "text/plain",
            text: "No identity crystal available. Generate one from the Fathom dashboard.",
          },
        ],
      };
    }
    throw new Error(`Unknown resource: ${uri}`);
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
