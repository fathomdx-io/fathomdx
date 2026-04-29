/**
 * Kitty — routine execution surface.
 *
 * Polls the delta lake for `routine-fire` deltas. For each new one, spawns
 * a standalone kitty window with `claude` and injects the routine prompt
 * via kitty's remote-control protocol. The user sees the routine running on
 * their desktop as a real interactive terminal — they can intervene at any
 * time.
 *
 * Fire delta shape:
 *   Tags:    [routine-fire, routine-id:<id>, workspace:<name>]
 *   Source:  any (dashboard, fathom-cli, scheduler, manual)
 *   Content: the prompt to inject into claude
 *
 * State file (~/.fathom/kitty-state.json) tracks the last-processed delta
 * timestamp so restarts don't re-fire historical events.
 */

import { spawn } from "child_process";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { homedir, hostname } from "os";
import { join, dirname } from "path";

const STATE_PATH = join(homedir(), ".fathom", "kitty-state.json");
const SOCKET_DIR = "/tmp";
const DEFAULT_RECEIPT_EXPIRY_DAYS = 30;

// Permission modes are deliberately file-only — accidentally widening the
// veto list from a browser is the exact risk the trust discussion flagged.
// Everything else is UI-editable.
export const CONFIG_SHAPE = {
  workspace_root: {
    type: "string",
    required: false,
    help: "Base directory for workspace-pinned routines. Default: ~/Dropbox/Work.",
  },
  default_workspace: {
    type: "string",
    required: false,
    help: "Directory claude opens in when a routine fires without a pinned workspace. Absolute path, or a ~-prefix for $HOME (e.g. '~/code/project', '/opt/apps'). Empty = $HOME.",
  },
  claude_command: {
    type: "string",
    required: false,
    help: "Claude CLI binary. Default: 'claude'.",
  },
  kitty_command: { type: "string", required: false, help: "Kitty binary. Default: 'kitty'." },
  kitty_background: {
    type: "string",
    required: false,
    help: "Background hex color for the spawned kitty window. Default: #17303a.",
  },
  auto_submit: {
    type: "string",
    required: false,
    help: "'true' to auto-submit prompts after injection, anything else to wait. Default: true.",
  },
  inject_ready_timeout_ms: {
    type: "number",
    required: false,
    help: "Max ms to wait for claude's TUI input field to render before injecting the prompt anyway. Plugin polls the screen and injects the moment the input box appears; this is the fallback ceiling. Default: 20000.",
  },
  allowed_permission_modes: {
    type: "string[]",
    required: false,
    editable_from_ui: false,
    help: "Which claude permission modes routines may request. File-only for safety.",
  },
  receipt_expiry_days: {
    type: "number",
    required: false,
    help: "Delta expiry in days for fire receipts (kitty-fire-receipt, kitty-fire-blocked). Default: 30. These are accountability breadcrumbs, not memory — the routine's summary delta is the durable artifact.",
  },
};

// Map of fire-delta-id → { socket, routineId, launched_at } for open windows.
// When a routine-summary delta lands tagged `fire-delta:<id>` matching one of
// these, the corresponding kitty window is closed via `kitten @ close-window`.
// Entries are pruned after MAX_FIRE_AGE_MS so claude sessions that never write
// a summary don't leak memory indefinitely (the window itself stays open —
// user can close it, or a future idle-watchdog can handle the cleanup).
const openFires = new Map();
const MAX_FIRE_AGE_MS = 6 * 60 * 60 * 1000; // 6h

function loadState() {
  try {
    return JSON.parse(readFileSync(STATE_PATH, "utf8"));
  } catch {
    return { last_seen: new Date().toISOString() };
  }
}

function saveState(state) {
  mkdirSync(dirname(STATE_PATH), { recursive: true });
  writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}

function tag(delta, prefix) {
  const t = (delta.tags || []).find((x) => x.startsWith(prefix));
  return t ? t.slice(prefix.length) : null;
}

function workspacePath(workspaceRoot, workspace) {
  if (!workspace) return workspaceRoot;
  // Avoid path traversal — workspace is a tag value, treat as literal segment
  const safe = workspace.replace(/[^a-zA-Z0-9_-]/g, "");
  return join(workspaceRoot, safe);
}

// Expand ~ / ~/ prefixes. Everything else passes through as-is; the path
// is whatever the user put in default_workspace, which they own. Empty
// string falls back to $HOME — claude-code needs somewhere to run.
function expandDefaultDir(p) {
  if (!p || p === "~") return homedir();
  if (p.startsWith("~/")) return join(homedir(), p.slice(2));
  return p;
}

async function pollOnce(config, pusher, state) {
  let fires, summaries;
  try {
    [fires, summaries] = await Promise.all([
      pusher.query({ tags_include: "routine-fire", time_start: state.last_seen, limit: 50 }),
      // Summaries poll from the earliest open fire, so a slow routine whose
      // summary lands after state.last_seen advances still gets matched.
      pusher.query({
        tags_include: "routine-summary",
        time_start: state.oldest_open_fire || state.last_seen,
        limit: 50,
      }),
    ]);
  } catch (e) {
    console.error(`  kitty: poll failed: ${e.message}`);
    return;
  }

  // Sort oldest-first so we fire in order
  fires.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));

  for (const d of fires) {
    if (d.timestamp <= state.last_seen) continue; // safety filter
    fire(d, config, pusher);
    state.last_seen = d.timestamp;
  }
  if (fires.length) saveState(state);

  // Close windows whose routine just wrote a summary. Summary tags include
  // `fire-delta:<fire_id>` so we can find the matching open window.
  for (const s of summaries) {
    const fireId = tag(s, "fire-delta:");
    if (!fireId) continue;
    const entry = openFires.get(fireId);
    if (!entry) continue;
    console.log(`  🐈 close ${entry.routineId} (summary ${s.id.slice(0, 8)} landed)`);
    closeWindow(entry.socket);
    openFires.delete(fireId);
  }

  // Prune stale entries whose summary never arrived.
  const now = Date.now();
  for (const [fireId, entry] of openFires) {
    if (now - entry.launched_at > MAX_FIRE_AGE_MS) openFires.delete(fireId);
  }
  // Track the oldest open fire's delta timestamp so the next summary poll
  // reaches back far enough to catch it.
  state.oldest_open_fire = openFires.size
    ? [...openFires.values()].map((e) => e.launched_iso).sort()[0]
    : null;
}

export function closeWindow(socket) {
  if (!existsSync(socket)) return;
  runKitten(["@", "--to", `unix:${socket}`, "close-window"], (code, err) => {
    if (code !== 0) console.error(`  ✗ close-window failed (${code}): ${err.trim()}`);
  });
}

// ── Public helpers — exported for future engagement plugins. ──

/**
 * Spawn a detached kitty window running `claude` in the given workspace and
 * schedule a prompt injection once the TUI is input-ready.
 *
 * Returns { socket, title, spawnedAt } so callers can inject more text later
 * (via kittySendText) or close the window (via runKitten close-window).
 * Does NOT track state — caller owns the lifecycle map.
 */
export function spawnClaudeInKitty({
  workspaceCwd,
  prompt,
  permissionMode = "auto",
  sessionLabel, // e.g. "chat-bubbly-brown-beaver"
  claudeBin = "claude",
  kittyBin = "kitty",
  kittyBackground = "#17303a",
  autoSubmit = true,
  injectReadyTimeoutMs = 20000,
  pusher, // optional — for logging a launch receipt
  receiptExpiresAt, // optional ISO; receipt delta TTL
}) {
  const stamp = Date.now();
  const title = `fathom-${sessionLabel}-${stamp}`;
  const socket = join(SOCKET_DIR, `kitty-${title}`);

  const claudeArgs = claudeArgsForMode(permissionMode);
  const args = [
    "--listen-on",
    `unix:${socket}`,
    "-o",
    "allow_remote_control=yes",
    "-o",
    `background=${kittyBackground}`,
    "--title",
    title,
    "--directory",
    workspaceCwd,
    "--detach",
    claudeBin,
    ...claudeArgs,
  ];
  const child = spawn(kittyBin, args, { stdio: "ignore", detached: true });
  child.unref();
  child.on("error", (e) => console.error(`  kitty spawn failed: ${e.message}`));

  // Wait until claude's TUI input field has actually rendered before we inject.
  // A fixed sleep was racy on cold starts (MCP servers + hooks can push first
  // paint past 5s); polling kitty's screen text catches it the moment it lands.
  (async () => {
    const ready = await waitForClaudeReady(socket, { maxWaitMs: injectReadyTimeoutMs });
    if (!ready) {
      console.warn(
        `  ⚠ kitty: claude readiness not detected within ${injectReadyTimeoutMs}ms — injecting anyway (${sessionLabel})`
      );
    }
    injectPrompt(socket, prompt, sessionLabel, null, pusher, autoSubmit, receiptExpiresAt);
  })();

  return { socket, title, spawnedAt: stamp };
}

// Read the current visible screen of the kitty window. Used to detect when
// claude's input field has rendered and is accepting keys.
function getKittyScreenText(socket) {
  return new Promise((resolve) => {
    if (!existsSync(socket)) return resolve("");
    const child = spawn(
      "kitten",
      ["@", "--to", `unix:${socket}`, "get-text", "--extent", "screen"],
      {
        stdio: ["ignore", "pipe", "pipe"],
      }
    );
    let out = "";
    child.stdout.on("data", (b) => (out += b.toString()));
    child.on("close", (code) => resolve(code === 0 ? out : ""));
    child.on("error", () => resolve(""));
  });
}

// Claude-code's TUI mounts an input row once the model's ready for input.
// We watch for two markers, either of which means the field will accept keys:
//   ❯  — prompt-cursor character at the start of the input row
//   ⏵⏵ — the bottom-line mode indicator ("⏵⏵ auto mode on ...")
// Both appear together when the TUI hits its idle/ready state. Older Ink
// banners can include `│ >` in chrome, so we keep that as a defensive fallback.
function looksLikeClaudeInput(text) {
  if (!text) return false;
  if (text.includes("❯")) return true;
  if (text.includes("⏵⏵")) return true;
  return /[│|]\s+>\s/.test(text);
}

async function waitForClaudeReady(socket, { maxWaitMs = 20000, pollMs = 400 } = {}) {
  const start = Date.now();
  // Poll for both kitty's listener appearing AND claude's input field rendering.
  // The socket file is created by kitty after spawn (a beat or two), then claude
  // boots inside the window and mounts its TUI. Either step can lag on cold
  // starts, so we wait for the full chain rather than bailing on socket-missing.
  while (Date.now() - start < maxWaitMs) {
    if (existsSync(socket)) {
      const text = await getKittyScreenText(socket);
      if (looksLikeClaudeInput(text)) return true;
    }
    await new Promise((r) => setTimeout(r, pollMs));
  }
  return false;
}

/**
 * Send text into an already-running kitty session at `socket`. No enter key —
 * just types the text. Use this for mid-engagement message delivery where the
 * agent's claude-code sees it like the user typed it.
 *
 * Returns a promise resolving to true on success.
 */
export function kittySendText(socket, text, { submit = true } = {}) {
  return new Promise((resolve) => {
    if (!existsSync(socket)) {
      console.error(`  kitty: socket ${socket} not found — window may have closed`);
      resolve(false);
      return;
    }
    runKitten(["@", "--to", `unix:${socket}`, "send-text", text], (code, err) => {
      if (code !== 0) {
        console.error(`  ✗ send-text failed (${code}): ${err.trim()}`);
        resolve(false);
        return;
      }
      if (!submit) {
        resolve(true);
        return;
      }
      setTimeout(() => {
        runKitten(["@", "--to", `unix:${socket}`, "send-key", "enter"], (code2, err2) => {
          if (code2 !== 0) {
            console.error(`  ✗ send-key enter failed (${code2}): ${err2.trim()}`);
            resolve(false);
          } else {
            resolve(true);
          }
        });
      }, 800);
    });
  });
}

/** True if the kitty window at this socket is still alive. */
export function kittySocketAlive(socket) {
  return existsSync(socket);
}

// Map a permission-mode tag value → claude-code CLI args.
// `auto`   → classifier auto-approves safe actions, blocks risky ones
// `normal` → no flag (claude prompts for each tool — user approves in kitty)
// Anything else falls back to normal (defensive).
function claudeArgsForMode(mode) {
  if (mode === "auto") return ["--permission-mode", "auto"];
  return [];
}

function fire(delta, config, pusher) {
  const routineId = tag(delta, "routine-id:") || "unknown";
  // Two different kinds of "where to run this":
  //   - a routine-pinned workspace tag → a subdir under workspace_root
  //     (the existing routine contract, preserved)
  //   - no tag → the agent's default_workspace, which is a full path the
  //     user chose at pair time (absolute or ~-prefixed)
  const routineWorkspace = tag(delta, "workspace:") || "";
  const requestedMode = tag(delta, "permission-mode:") || "auto";
  const targetHost = tag(delta, "host:") || "";

  // ── Host-pinning veto ──
  // A fire with `host:<name>` is reserved for that specific agent. Silently
  // skip fires not addressed to us so every agent's kitty plugin doesn't
  // race to spawn windows for host-pinned routines. Fires with no host tag
  // are fleet-wide and accepted everywhere.
  const myHost = config.host || hostname().split(".")[0];
  if (targetHost && targetHost !== myHost) return;

  // ── Agent veto ──
  // The dashboard controls routines; the agent controls its own execution.
  // `allowed_permission_modes` is the local kill switch: anything not in the
  // list is refused with a blocked-locally receipt delta so the dashboard can
  // surface it.
  const allowed = config.allowed_permission_modes || ["auto", "normal"];
  const receiptExpiryDays =
    config.receipt_expiry_days != null ? config.receipt_expiry_days : DEFAULT_RECEIPT_EXPIRY_DAYS;
  const receiptExpiresAt = receiptExpiryDays
    ? new Date(Date.now() + receiptExpiryDays * 86400000).toISOString()
    : null;
  if (!allowed.includes(requestedMode)) {
    console.log(
      `  🚫 vetoed ${routineId}: mode ${requestedMode} not allowed (allowed: ${allowed.join(",")})`
    );
    const blocked = {
      content: `[kitty-veto] Fire ${delta.id} for routine ${routineId} blocked locally — permission-mode "${requestedMode}" not in this agent's allow-list (${allowed.join(", ")}).`,
      tags: [
        "kitty-fire-blocked",
        `routine-id:${routineId}`,
        `fire-delta:${delta.id}`,
        `blocked-mode:${requestedMode}`,
      ],
      source: "kitty",
    };
    if (receiptExpiresAt) blocked.expires_at = receiptExpiresAt;
    pusher?.push?.(blocked);
    return;
  }

  const cwd = routineWorkspace
    ? workspacePath(config.workspace_root, routineWorkspace)
    : expandDefaultDir(config.default_workspace);
  const body = (delta.content || "").trim();
  const footer = [
    "",
    "---",
    "When you finish, write a one-line summary delta with these tags so the dashboard can link it to this run:",
    `\`fathom delta write "[${routineId}] <one-sentence summary>" --tags routine-summary,routine-id:${routineId},fire-delta:${delta.id} --source claude-code:routine\``,
  ].join("\n");
  const prompt = `${body}\n${footer}`;

  console.log(`  🐈 fire ${routineId} (cwd: ${cwd}, mode: ${requestedMode})`);

  const { socket, spawnedAt } = spawnClaudeInKitty({
    workspaceCwd: cwd,
    prompt,
    permissionMode: requestedMode,
    sessionLabel: routineId,
    claudeBin: config.claude_command,
    kittyBin: config.kitty_command,
    kittyBackground: config.kitty_background,
    autoSubmit: config.auto_submit !== false,
    injectReadyTimeoutMs: config.inject_ready_timeout_ms,
    pusher,
    receiptExpiresAt,
  });

  // Track the open window so a matching routine-summary delta can close it.
  openFires.set(delta.id, {
    socket,
    routineId,
    launched_at: spawnedAt,
    launched_iso: new Date(spawnedAt).toISOString(),
  });
}

function injectPrompt(
  socket,
  prompt,
  routineId,
  fireDeltaId,
  pusher,
  autoSubmit = true,
  receiptExpiresAt = null
) {
  if (!existsSync(socket)) {
    console.error(`  kitty: socket ${socket} not found — kitty may have failed to start`);
    return;
  }
  // Two-step injection: send-text writes the prompt into the input field,
  // then send-key enter submits it. Claude-code's TUI treats raw newlines as
  // literal multiline input, not submission — Enter must be a real keypress.
  runKitten(["@", "--to", `unix:${socket}`, "send-text", prompt], (code, err) => {
    if (code !== 0) {
      console.error(`  ✗ send-text failed (${code}): ${err.trim()}`);
      return;
    }
    if (!autoSubmit) {
      console.log(
        `  ✓ injected ${prompt.length}-char prompt → ${routineId} (awaiting user submit)`
      );
      return;
    }
    // Claude-code's Ink TUI needs time to commit a pasted multiline buffer
    // before an Enter is interpreted as submit rather than newline. A short
    // 250ms delay proved unreliable — the prompt typed but didn't submit.
    // 800ms gives Ink's re-render loop headroom on slower machines.
    setTimeout(() => {
      runKitten(["@", "--to", `unix:${socket}`, "send-key", "enter"], (code2, err2) => {
        if (code2 !== 0) {
          console.error(`  ✗ send-key enter failed (${code2}): ${err2.trim()}`);
          return;
        }
        console.log(`  ✓ injected + submitted ${prompt.length}-char prompt → ${routineId}`);
        // Receipt only makes sense for routine-fire-driven spawns. Callers
        // that spawn outside the routine-fire path pass fireDeltaId=null so
        // they don't pollute the lake with a fake fire-delta: tag.
        if (fireDeltaId && pusher?.push) {
          const receipt = {
            content: `[kitty-fire] routine ${routineId} launched. Prompt: ${prompt.slice(0, 200)}${prompt.length > 200 ? "…" : ""}`,
            tags: ["kitty-fire-receipt", `routine-id:${routineId}`, `fire-delta:${fireDeltaId}`],
            source: "kitty",
          };
          if (receiptExpiresAt) receipt.expires_at = receiptExpiresAt;
          pusher.push(receipt);
        }
      });
    }, 800);
  });
}

function runKitten(args, onDone) {
  const child = spawn("kitten", args, { stdio: "pipe" });
  let err = "";
  child.stderr.on("data", (b) => (err += b.toString()));
  child.on("close", (code) => onDone(code, err));
}

export default {
  name: "Kitty",
  category: "runtime",
  icon: "🐈",
  description: "Spawn kitty windows with claude when routines fire.",
  defaults: {
    workspace_root: join(homedir(), "Dropbox", "Work"),
    // Directory claude opens in when a routine fires without a pinned
    // workspace. A full path — absolute (/opt/apps) or ~-prefixed
    // (~/code/project). Empty string or "~" falls back to $HOME, since
    // claude-code still has to run somewhere.
    default_workspace: "",
    poll_interval_ms: 3000,
    // Ceiling for how long we'll wait for claude's TUI to become input-ready
    // before injecting the prompt anyway. Kitty plugin polls the screen text
    // and injects the moment it sees the input field — this is just the
    // fallback if the marker never appears. Bump if you have very slow cold
    // starts (lots of MCP servers, hooks, etc).
    inject_ready_timeout_ms: 20000,
    auto_submit: true,
    claude_command: "claude",
    kitty_command: "kitty",
    // Background color for routine-spawned kitty windows. Teal tint so they
    // stand out from regular kitty sessions on the desktop. Any hex color or
    // named color kitty accepts works here.
    kitty_background: "#17303a",
    // Agent veto list: only fires whose permission-mode tag is in this list
    // will spawn kitty. Any other fire writes a [kitty-fire-blocked] receipt
    // delta and is skipped. Set to ["normal"] to refuse all auto-mode routines
    // locally, or [] to stop all routine connectivity while keeping sources.
    allowed_permission_modes: ["auto", "normal"],
    receipt_expiry_days: DEFAULT_RECEIPT_EXPIRY_DAYS,
  },

  start(config, pusher) {
    const state = loadState();
    const allowed = config.allowed_permission_modes || ["auto", "normal"];
    console.log(`  kitty: polling lake for routine-fire deltas (last seen: ${state.last_seen})`);
    console.log(`  kitty: allowed permission modes = [${allowed.join(", ")}]`);

    const tick = () => pollOnce(config, pusher, state);
    const timer = setInterval(tick, config.poll_interval_ms || 3000);
    tick(); // fire one immediately

    return {
      stop() {
        clearInterval(timer);
        saveState(state);
      },
    };
  },
};
