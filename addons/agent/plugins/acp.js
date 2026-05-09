/**
 * ACP — generic Agent Client Protocol dispatcher.
 *
 * One plugin, many target roles. Each target is an ACP adapter the
 * helper can spawn (openclaw-acp-bridge, codex-acp, gemini-cli-acp,
 * mythos-acp once it's a thing). Each target advertises its own
 * helper-role via the heartbeat, polls the inbox for tasks at that
 * role, spawns the adapter on demand, runs the ACP prompt dance, and
 * streams session/update notifications back as helper-update replies.
 *
 * NOTE: claude-code lives in the kitty plugin, NOT here. Kitty owns
 * the visible-window UX for terminal-program helpers (claude on Linux
 * via kitty, the same shape via Warp or another spawner on other
 * OSes). ACP is reserved for adapters whose value is the structured
 * protocol — programs without their own native chat window worth
 * watching, or remote-only agents like Mythos.
 *
 * Configuration (~/.fathom/agent.json under plugins.acp):
 *
 *   {
 *     "enabled": true,
 *     "poll_interval_ms": 3000,
 *     "targets": [
 *       {
 *         "role": "openclaw",
 *         "command": "npx",
 *         "args": ["-y", "@openclaw/acp-bridge"],
 *         "description": "chat-synthesis, multi-channel routing"
 *       },
 *       {
 *         "role": "codex",
 *         "command": "npx",
 *         "args": ["-y", "codex-acp"],
 *         "description": "OpenAI Codex coding agent"
 *       }
 *     ]
 *   }
 *
 * Phase 3.0 is HEADLESS: client-method requests from the adapter
 * (fs/read_text_file, terminal/create, session/request_permission)
 * are refused with method-not-found. Adapters that need tools to do
 * useful work will return errors; they prove the wire works without
 * yet hooking up tool execution. Tool-call routing is a follow-up
 * slice.
 */

import { hostname } from "os";

import { AcpClient } from "../acp_client.js";

// Map<corr, AcpClient> — in-flight tasks per host. Used to skip
// inbox items we're already handling (the inbox returns dispatches
// based on absence of `task-complete`, so a running task can resurface
// on every poll until it finishes).
const inFlight = new Map();

// Set<corr> — dispatches that just completed but whose helper-complete
// delta may not yet be durable in the lake (a few-second indexing
// window). The inbox endpoint hides corrs whose complete is in the
// lake; this set covers the gap before that filter starts taking
// effect. Bounded by COMPLETED_GRACE_MS — entries auto-evict via
// setTimeout.
const recentlyCompleted = new Set();
const COMPLETED_GRACE_MS = 30 * 1000;

function buildHelperRoles(targets) {
  if (!Array.isArray(targets)) return [];
  return targets
    .filter((t) => t && typeof t.role === "string" && t.role.trim())
    .map((t) => ({
      role: t.role.trim(),
      description: typeof t.description === "string" ? t.description.trim() : "",
    }));
}

function findTarget(targets, role) {
  if (!Array.isArray(targets)) return null;
  for (const t of targets) {
    if (t && t.role === role) return t;
  }
  return null;
}

async function dispatchOne(item, target, inbox, _config) {
  const corr = item.corr || "";
  if (!corr) return;
  if (inFlight.has(corr)) return;
  if (recentlyCompleted.has(corr)) return;

  if (!target.command) {
    console.error(`  acp: target ${target.role} missing command — skipping ${corr.slice(0, 12)}`);
    inbox
      .reply(corr, {
        kind: "error",
        content: `acp target ${target.role} has no command configured`,
      })
      .catch(() => {});
    return;
  }

  console.log(`  acp: dispatch ${corr.slice(0, 12)} → ${target.role} (${target.command})`);

  // Accumulate agent_message_chunk text across the turn so we can use
  // the full assembled reply as the closure delta's content. The
  // session/prompt response itself doesn't include the assembled text
  // — it's a stop-reason container — so without this accumulation the
  // closure delta carries only "[done · end_turn]" and downstream
  // closure-followup logic (claude_code_watcher → harness) has no
  // model output to relay.
  const messageBuffer = [];
  const client = new AcpClient({
    command: target.command,
    args: target.args || [],
    env: target.env || {},
    cwd: target.cwd,
    onUpdate: (params) => {
      // session/update notifications carry the agent's progress for
      // the current turn. The shape varies by sessionUpdate type:
      //   agent_message_chunk → params.update.content is {type, text}
      //   tool_call_update    → has params.update.content array
      //   session_info_update / usage_update / available_commands_update → metadata
      // We extract user-facing text when we recognize the shape, and
      // fall back to a JSON-stringified envelope for everything else
      // so the dashboard can still parse structured events.
      const update = params?.update || {};
      let content = "";
      const sub = update.sessionUpdate || "";
      const c = update.content;
      if (typeof c === "string") {
        content = c;
      } else if (c && typeof c.text === "string") {
        // agent_message_chunk { content: { type: "text", text: "..." } }
        content = c.text;
      } else if (Array.isArray(c)) {
        // some shapes use array-of-blocks
        content = c
          .map((b) => (b && typeof b.text === "string" ? b.text : ""))
          .filter(Boolean)
          .join("");
      }
      // Accumulate real text into messageBuffer for the final closure
      // body. Skip metadata events.
      if (content && sub === "agent_message_chunk") {
        messageBuffer.push(content);
      }
      if (!content) {
        // Metadata events (session_info_update, usage_update, etc.) —
        // useful to the dashboard for rendering session state, but
        // not user-facing text. Keep them as JSON so consumers can
        // distinguish from real model output.
        content = JSON.stringify({ sessionUpdate: sub, ...update }).slice(0, 4000);
      }
      inbox
        .reply(corr, { kind: "update", content })
        .catch((e) => console.error(`  acp: update reply failed: ${e.message}`));
    },
    // No client-method handler: phase 3.0 refuses fs/terminal/permission
    // requests with method-not-found. Adapters running headless against
    // a method-less client will simply lack tools.
    onClientMethod: null,
  });
  inFlight.set(corr, client);

  try {
    client.spawn();
    await client.initialize({});
    const sessionResp = await client.sessionNew({});
    const sessionId =
      sessionResp?.sessionId || sessionResp?.session_id || sessionResp?.session || "";
    if (!sessionId) {
      throw new Error("session/new returned no session id");
    }
    // Emit a task-spawn handshake delta so claude_code_watcher.py can
    // pair this corr to the helper session. Without it, closing this
    // task wouldn't mint a closure-followup chat-reply, and the
    // user's chat thread would show only the dispatch with no
    // assistant reply afterward. Mirrors what the kitty plugin does
    // when its prompt-marker matches a real claude-code session id.
    inbox
      .reply(corr, {
        kind: "update",
        content: `[task-spawn] task ${corr} → ${target.role} session ${sessionId}`,
        extra_tags: [`helper-session:${sessionId}`, "task-spawn"],
      })
      .catch((e) => console.error(`  acp: task-spawn reply failed: ${e.message}`));
    const promptResp = await client.sessionPrompt({
      sessionId,
      prompt: [{ type: "text", text: item.task || "" }],
    });
    const stop = promptResp?.stopReason || promptResp?.stop_reason || "ok";
    // Prefer the accumulated agent_message_chunk text (the actual
    // model output as it streamed). Fall back to a top-level content
    // field on the prompt response, then to a marker line.
    //
    // We deliberately DON'T strip per-adapter wrapping conventions
    // here (OpenClaw's <final>...</final>, others' <think>...</think>,
    // etc.). The plugin is generic over ACP; what conventions an
    // adapter uses around its model output is the adapter's
    // business, not ours. Downstream consumers — the dashboard
    // rendering helper-update content, the harness when it relays a
    // helper reply into a chat-reply — can post-process per-adapter
    // if they care to.
    let finalText = messageBuffer.join("").trim();
    if (!finalText && typeof promptResp?.content === "string") {
      finalText = promptResp.content;
    }
    if (!finalText) {
      finalText = `[done · ${stop}]`;
    }
    await inbox.reply(corr, { kind: "complete", content: finalText });
    console.log(
      `  acp: complete ${corr.slice(0, 12)} (stop=${stop}, ${finalText.length} chars)`,
    );
  } catch (e) {
    console.error(`  acp: dispatch ${corr.slice(0, 12)} failed: ${e.message}`);
    try {
      await inbox.reply(corr, { kind: "error", content: e.message || String(e) });
    } catch (replyErr) {
      console.error(`  acp: error reply also failed: ${replyErr.message}`);
    }
  } finally {
    inFlight.delete(corr);
    recentlyCompleted.add(corr);
    setTimeout(() => recentlyCompleted.delete(corr), COMPLETED_GRACE_MS).unref?.();
    try {
      await client.close();
    } catch {}
  }
}

async function pollOnce(config, inbox, state, targetsByRole) {
  // Pull the host's whole inbox and bucket items by role server-side
  // role tagging. The inbox is per-host, so one fetch covers every
  // target this plugin handles.
  let resp;
  try {
    resp = await inbox.fetch({
      since: state.last_seen,
      limit: 50,
    });
  } catch (e) {
    console.error(`  acp: inbox.fetch failed: ${e.message}`);
    return;
  }
  for (const item of resp.items || []) {
    const target = targetsByRole.get(item.role);
    if (!target) continue; // not one of our roles — kitty or another plugin will pick it up
    // Spawn dispatch in background; pollOnce returns quickly so the
    // poll cadence stays steady even when a task takes minutes.
    dispatchOne(item, target, inbox, config).catch((e) =>
      console.error(`  acp: dispatch crashed: ${e.message}`),
    );
  }
  if (resp.cursor) state.last_seen = resp.cursor;
}

export default {
  name: "ACP",
  category: "runtime",
  icon: "🔌",
  description: "Dispatch tasks to ACP-speaking adapters (claude-code-acp, openclaw-acp, etc.).",
  // helperRoles is computed per-instance from `targets` in the operator's
  // config. The static field here is the best-effort default surfaced
  // when `targets` is empty or the config hasn't been read; the live
  // heartbeat-time list is the one that matters (heartbeat plugin reads
  // the start()'d plugin's helperRoles indirectly, so the meta map
  // walk picks this static one up). Operators set targets in their
  // agent.json; on next agent restart the heartbeat broadcasts their
  // roles. (A reload of the heartbeat plugin's meta map would be needed
  // to pick them up without a restart — separate concern.)
  helperRoles: [],
  defaults: {
    enabled: false,
    poll_interval_ms: 3000,
    targets: [],
  },

  start(_config_unused, _pusher, _context, inbox) {
    const config = _config_unused || {};
    const targets = Array.isArray(config.targets) ? config.targets : [];
    const helperRoles = buildHelperRoles(targets);
    if (!helperRoles.length) {
      console.log(
        `  acp: no targets configured — plugin idle. Add entries to ` +
          `plugins.acp.targets in ~/.fathom/agent.json.`,
      );
      return { stop() {} };
    }

    // Reject duplicate role names across targets — two targets sharing
    // a role would both claim every dispatch for that role, leading to
    // double-spawn. Better to refuse to start than silently misbehave.
    const seenRoles = new Set();
    const dupRoles = [];
    for (const r of helperRoles) {
      if (seenRoles.has(r.role)) dupRoles.push(r.role);
      else seenRoles.add(r.role);
    }
    if (dupRoles.length) {
      console.error(
        `  acp: duplicate role names in targets — refusing to start: ` +
          `[${[...new Set(dupRoles)].join(", ")}]. Each role must be ` +
          `unique within plugins.acp.targets.`,
      );
      return { stop() {} };
    }

    // Surface the configured roles for the heartbeat plugin's next
    // meta-map rebuild. Mutating the export's helperRoles is the only
    // way to communicate dynamic roles up to heartbeat without a
    // bigger plumbing refactor; the heartbeat caches the meta map per
    // process, so this assignment is read on the next plugin reload
    // or process restart.
    this.helperRoles = helperRoles;

    const targetsByRole = new Map();
    for (const t of targets) {
      if (t && t.role) targetsByRole.set(t.role, t);
    }

    const state = { last_seen: new Date(Date.now() - 60_000).toISOString() };
    const myHost = config.host || hostname().split(".")[0];
    console.log(
      `  acp: polling /v1/helpers/${myHost}/inbox for roles [${[...targetsByRole.keys()].join(", ")}]`,
    );

    const tick = () => pollOnce(config, inbox, state, targetsByRole).catch((e) =>
      console.error(`  acp: tick failed: ${e.message}`),
    );
    const timer = setInterval(tick, config.poll_interval_ms || 3000);
    tick();

    return {
      stop() {
        clearInterval(timer);
        // Best-effort terminate any in-flight subprocesses. close() drains
        // stdin; agents that hang get a SIGTERM after a short grace.
        for (const [corr, client] of inFlight) {
          client.close().catch(() => {});
          setTimeout(() => client.kill("SIGTERM"), 2000).unref?.();
        }
      },
    };
  },
};
