/**
 * Kitty — claude-code dispatch surface.
 *
 * Polls the delta lake for `route:claude-code` cards (witness dispatches)
 * targeted at this host. For each new dispatch, spawns a standalone kitty
 * window with `claude` and injects the prompt via kitty's remote-control
 * protocol. The user sees the work running on their desktop as a real
 * interactive terminal — they can intervene at any time.
 *
 * Dispatch delta shape:
 *   Tags:    [feed-card, route:claude-code, host:<myhost>, task-corr:<id>]
 *   Source:  witness
 *   Content: JSON payload whose body is the prompt
 *
 * Routines flow through this same path. The cron scheduler (or Fire Now,
 * or the witness's `routine-fire:<id>` route) writes a `routine-due`
 * intent into the puddle; the witness reads it, deliberates, and — when
 * fresh data is needed — emits a `route:claude-code` dispatch card. So
 * routines that need claude-code arrive HERE, just via one extra River
 * tick. The legacy `routine-fire` direct-to-kitty consumer was retired
 * 2026-04-30 — there's no longer a "skip the River" path.
 *
 * State file (~/.fathom/kitty-state.json) tracks the last-processed delta
 * timestamp so restarts don't re-fire historical events.
 */

import { spawn } from "child_process";
import { readFileSync, writeFileSync, renameSync, mkdirSync, existsSync } from "fs";
import { homedir, hostname } from "os";
import { join, dirname, resolve } from "path";

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

// Map of task-correlation → { socket, claude_session_id, cwd, spawned_at,
// launched_iso, task_delta_id } for open claude-code-channel tasks.
//
// Distinct from openFires because the keys are different (correlation vs
// fire-delta-id), the close trigger is different (`task-complete` vs
// `routine-summary`), and the lifecycle supports mid-task continuation —
// a second `to:claude-code:<corr>` for an already-open task injects into
// the same window via kittySendText instead of respawning.
//
// `claude_session_id` is null until the handshake matches the first hook
// delta from the spawned session; once set, the loop watcher uses it to
// route subsequent session deltas back as intents.
const openTasks = new Map();

// Set of correlation ids we've seen `task-complete` deltas for during
// this run. Witness's reply to a closure intent should route as
// `chat-reply` (the witness branch handles that), but if anything ever
// re-emits a `to:claude-code:<corr>` for an already-closed corr, this
// gate makes us refuse to respawn — the task is over; a new window
// would have no continuity with the prior session anyway.
//
// Bounded only by process lifetime — agent restart clears it. That's
// fine: respawn-after-restart is a rare path, and the cost (one
// orphaned re-spawn) is small compared to keeping a persistent set
// across runs.
const knownCompletedCorrs = new Set();

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

// Pre-accept claude-code's per-folder workspace-trust dialog so a fresh
// spawn into a directory the user hasn't manually opened in claude
// before doesn't block at the "Do you trust this folder?" prompt. The
// dialog also renders `❯` (its menu cursor), which previously fooled
// the input-readiness check into injecting the prompt straight into
// the trust dialog. Pre-writing the trust state in ~/.claude.json is
// equivalent to the user accepting the dialog manually once: future
// claude sessions in that directory carry the same accepted state.
//
// Atomic write via temp-file + rename so a concurrent claude process
// reading the config can't observe a half-written file. Failures are
// logged but never thrown — claude will just show its own dialog,
// which the user can dismiss.
function ensureClaudeTrustsDir(rawDir) {
  if (!rawDir) return;
  const absDir = resolve(rawDir);
  const path = join(homedir(), ".claude.json");
  if (!existsSync(path)) return;
  let cfg;
  try {
    cfg = JSON.parse(readFileSync(path, "utf8"));
  } catch (e) {
    console.warn(`  kitty: couldn't parse ~/.claude.json, skipping trust pre-write: ${e.message}`);
    return;
  }
  cfg.projects = cfg.projects || {};
  const proj = cfg.projects[absDir] || {};
  if (proj.hasTrustDialogAccepted === true) return;
  proj.hasTrustDialogAccepted = true;
  cfg.projects[absDir] = proj;
  const tmp = `${path}.fathom-tmp.${process.pid}`;
  try {
    writeFileSync(tmp, JSON.stringify(cfg, null, 2));
    renameSync(tmp, path);
    console.log(`  kitty: pre-trusted ${absDir} in ~/.claude.json`);
  } catch (e) {
    console.warn(`  kitty: couldn't write ~/.claude.json: ${e.message}`);
  }
}

// Pull the prompt body from a witness card. Witness writes JSON
// {kicker, title, body, tail, ...}; older or hand-written test deltas
// might be plain text — fall through gracefully.
function extractTaskBody(delta) {
  const raw = (delta.content || "").trim();
  if (!raw) return "";
  if (raw.startsWith("{")) {
    try {
      const payload = JSON.parse(raw);
      return (payload.body || "").trim();
    } catch {
      /* fall through to raw */
    }
  }
  return raw;
}

// The footer instructs claude to write a closure delta when the task
// wraps up. The `[fathom-task:<corr>]` line is also the handshake
// marker — we match on this substring in the first hook delta from
// the spawned session to learn its claude session id.
function buildTaskPrompt(body, corr) {
  return [
    `[fathom-task:${corr}]`,
    "",
    body,
    "",
    "---",
    `When this task is complete, write a closure delta. Prefer the MCP tool; fall back to the CLI only if MCP is unavailable.`,
    "",
    `MCP (preferred):`,
    `  tool: mcp__fathom__write`,
    `  args: { content: "<your reply or summary>", tags: ["task-complete", "task-corr:${corr}", "kind:claude-code-reply"], source: "claude-code:task" }`,
    "",
    `CLI (fallback):`,
    `  \`fathom delta write "<your reply or summary>" --tags task-complete,task-corr:${corr},kind:claude-code-reply --source claude-code:task\``,
    "",
    "Intermediate progress: write deltas as you go — your normal hook deltas are picked up automatically.",
  ].join("\n");
}

// Find the timestamp of the oldest task awaiting handshake. The
// handshake-candidate poll uses this as time_start so we don't ask
// for the entire claude-code source history.
function oldestUnmatchedTaskIso() {
  let oldest = null;
  for (const entry of openTasks.values()) {
    if (entry.claude_session_id) continue;
    if (!oldest || entry.launched_iso < oldest) oldest = entry.launched_iso;
  }
  return oldest;
}

async function pollOnce(config, pusher, state) {
  const myHost = config.host || hostname().split(".")[0];

  // Backfill new state fields on first run after upgrade — the saved
  // file from older versions only carries `last_seen` and the routine
  // bookkeeping. Without these defaults the claude-code queries would
  // fall back to `time_start: undefined` and re-fetch the world.
  if (!state.task_seen_at) state.task_seen_at = state.last_seen;

  // Build the handshake-candidate query window only if we actually have
  // tasks awaiting their session id. Skip the round-trip otherwise.
  const handshakeWindowStart = oldestUnmatchedTaskIso();

  // Routine fires no longer flow through this consumer — every routine
  // (cron, Fire Now, witness `routine-fire:<id>`) goes through the River.
  // The witness reads the routine-due intent and, when fresh data is
  // needed, dispatches via `route:claude-code` — the path below.
  let taskDispatches, taskCloses, handshakeCandidates;
  try {
    [taskDispatches, taskCloses, handshakeCandidates] = await Promise.all([
      // Claude-code-channel dispatches — witness cards routed at this
      // host. AND-semantics on tags_include (route:claude-code AND
      // host:<myhost>) means each agent only ever sees fires for itself,
      // even before the per-delta veto runs.
      pusher.query({
        tags_include: `route:claude-code,host:${myHost}`,
        time_start: state.task_seen_at,
        limit: 50,
      }),
      pusher.query({
        tags_include: "task-complete",
        time_start: state.oldest_open_task || state.task_seen_at,
        limit: 50,
      }),
      // Handshake candidates: hooks fire on every claude-code session on
      // this host, so this is potentially noisy. We filter client-side by
      // the [fathom-task:<corr>] marker we embedded in the spawned prompt.
      handshakeWindowStart
        ? pusher.query({
            source: "claude-code",
            time_start: handshakeWindowStart,
            limit: 100,
          })
        : Promise.resolve([]),
    ]);
  } catch (e) {
    console.error(`  kitty: poll failed: ${e.message}`);
    return;
  }

  // ── Claude-code-channel ─────────────────────────────────────────────
  taskDispatches.sort((a, b) => (a.timestamp || "").localeCompare(b.timestamp || ""));

  for (const d of taskDispatches) {
    if (d.timestamp <= state.task_seen_at) continue;
    dispatchClaudeCodeTask(d, config, pusher, myHost);
    state.task_seen_at = d.timestamp;
  }
  if (taskDispatches.length) saveState(state);

  // Match handshake candidates against open unmatched tasks. The
  // `[fathom-task:<corr>]` substring is the join key — uniqueness comes
  // from the corr value itself, which is per-task.
  for (const cand of handshakeCandidates) {
    const candHost = (cand.tags || []).find((t) => t.startsWith("host:"))?.slice(5);
    if (candHost && candHost !== myHost) continue;
    const content = cand.content || "";
    for (const [corr, entry] of openTasks) {
      if (entry.claude_session_id) continue;
      if (!content.includes(`[fathom-task:${corr}]`)) continue;
      const sid = (cand.tags || []).find((t) => t.startsWith("session:"))?.slice(8);
      if (!sid) break;
      const projectTag = (cand.tags || []).find((t) => t.startsWith("project:"))?.slice(8);
      entry.claude_session_id = sid;
      if (projectTag) entry.cwd = projectTag;
      console.log(`  🐈 handshake ${corr.slice(0, 12)} → claude session ${sid.slice(0, 8)}`);
      // Join delta — the loop's claude-code watcher uses this to know
      // that subsequent `session:<sid>` deltas belong to this task.
      pusher?.push?.({
        content: `[task-spawn] task ${corr} → claude session ${sid} on ${myHost}`,
        tags: [
          "task-spawn",
          `task-corr:${corr}`,
          `claude-code-session:${sid}`,
          `host:${myHost}`,
          ...(projectTag ? [`project:${projectTag}`] : []),
        ],
        source: "kitty",
      });
      break;
    }
  }

  // Close windows whose task just wrote a task-complete delta.
  for (const c of taskCloses) {
    const corr = tag(c, "task-corr:");
    if (!corr) continue;
    knownCompletedCorrs.add(corr);
    const entry = openTasks.get(corr);
    if (!entry) continue;
    console.log(`  🐈 close task ${corr.slice(0, 12)} (task-complete landed)`);
    closeWindow(entry.socket);
    openTasks.delete(corr);
  }

  // Detect tasks whose kitty window the user closed manually (or that
  // crashed) without writing a task-complete. The dashboard's status
  // strip filters by spawn-without-complete, so abandoned tasks
  // otherwise stay lit forever. We emit `task-abandoned` so the strip
  // can treat it as a closure signal.
  //
  // Grace window: kitty creates the socket a beat or two after spawn,
  // so a too-eager check would false-positive on every fresh task.
  // 15s is well clear of cold-start jitter.
  const now = Date.now();
  const ABANDON_GRACE_MS = 15 * 1000;
  for (const [corr, entry] of openTasks) {
    if (now - entry.spawned_at < ABANDON_GRACE_MS) continue;
    if (kittySocketAlive(entry.socket)) continue;
    console.log(`  🐈 abandon task ${corr.slice(0, 12)} (window gone, no task-complete)`);
    knownCompletedCorrs.add(corr);
    openTasks.delete(corr);
    pusher?.push?.({
      content: `[task-abandoned] task ${corr} closed without writing a completion delta on ${myHost}`,
      tags: [
        "task-abandoned",
        `task-corr:${corr}`,
        `host:${myHost}`,
        ...(entry.claude_session_id ? [`claude-code-session:${entry.claude_session_id}`] : []),
      ],
      source: "kitty",
    });
  }

  // Prune stale entries whose summary never arrived.
  for (const [fireId, entry] of openFires) {
    if (now - entry.launched_at > MAX_FIRE_AGE_MS) openFires.delete(fireId);
  }
  for (const [corr, entry] of openTasks) {
    if (now - entry.spawned_at > MAX_FIRE_AGE_MS) openTasks.delete(corr);
  }
  // Track the oldest open fire's delta timestamp so the next summary poll
  // reaches back far enough to catch it.
  state.oldest_open_fire = openFires.size
    ? [...openFires.values()].map((e) => e.launched_iso).sort()[0]
    : null;
  state.oldest_open_task = openTasks.size
    ? [...openTasks.values()].map((e) => e.launched_iso).sort()[0]
    : null;
}

// Spawn a new claude-code window for a task dispatch, OR — if a window
// for this correlation is already open — inject the next prompt into it
// via kittySendText. The "same window for the whole task" property is
// what lets multi-turn back-and-forth between Fathom and a tasked claude
// session feel like an actual conversation rather than a chain of
// disconnected one-shots.
function dispatchClaudeCodeTask(delta, config, pusher, myHost) {
  // Pull the corr off the `to:claude-code:<corr>` tag. The witness
  // stamps this for every claude-code-routed card; without it we have
  // nothing to track the task by, so skip.
  let corr = null;
  for (const t of delta.tags || []) {
    if (t.startsWith("to:claude-code:")) {
      corr = t.slice("to:claude-code:".length);
      break;
    }
  }
  if (!corr) {
    console.warn(`  kitty: claude-code dispatch ${delta.id.slice(0, 8)} missing to:claude-code:<corr>`);
    return;
  }

  // Refuse to respawn a task that already wrapped. The witness's
  // reply to a closure intent is supposed to route as `chat-reply`,
  // but treat that as a soft contract — if anything routes back
  // here for a known-closed corr, ignore it.
  if (knownCompletedCorrs.has(corr)) {
    console.log(`  🐈 ignoring dispatch for already-closed task ${corr.slice(0, 12)}`);
    return;
  }

  const body = extractTaskBody(delta);
  if (!body) {
    // Empty body is a meaningful signal at the openai surface (loop
    // chose silence) but a no-op for the kitty surface — there's
    // nothing to inject. Skip without spawning.
    return;
  }

  const prompt = buildTaskPrompt(body, corr);

  // Mid-task continuation — inject into the existing window.
  const existing = openTasks.get(corr);
  if (existing && kittySocketAlive(existing.socket)) {
    console.log(`  🐈 task-cont ${corr.slice(0, 12)} (sendText into open window)`);
    kittySendText(existing.socket, prompt, { submit: true });
    return;
  }

  // Fresh dispatch — spawn a new window.
  const cwd = expandDefaultDir(config.default_workspace);
  console.log(`  🐈 task-spawn ${corr.slice(0, 12)} (cwd: ${cwd})`);
  const { socket, spawnedAt } = spawnClaudeInKitty({
    workspaceCwd: cwd,
    prompt,
    permissionMode: "auto",
    sessionLabel: `task-${corr.slice(0, 12)}`,
    claudeBin: config.claude_command,
    kittyBin: config.kitty_command,
    kittyBackground: config.kitty_background,
    autoSubmit: config.auto_submit !== false,
    injectReadyTimeoutMs: config.inject_ready_timeout_ms,
    pusher,
    receiptExpiresAt: null,
  });
  openTasks.set(corr, {
    socket,
    claude_session_id: null,
    cwd,
    spawned_at: spawnedAt,
    launched_iso: new Date(spawnedAt).toISOString(),
    task_delta_id: delta.id,
  });
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
  // Pre-accept the workspace trust dialog for this directory so the
  // injection path doesn't race against (or land inside) claude's
  // first-run prompt. No-op when already trusted.
  ensureClaudeTrustsDir(workspaceCwd);

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
    "When you finish, write a one-line summary delta so the dashboard can link it to this run. Prefer MCP; fall back to CLI only if MCP is unavailable.",
    "",
    "MCP (preferred):",
    "  tool: mcp__fathom__write",
    `  args: { content: "[${routineId}] <one-sentence summary>", tags: ["routine-summary", "routine-id:${routineId}", "fire-delta:${delta.id}"], source: "claude-code:routine" }`,
    "",
    "CLI (fallback):",
    `  \`fathom delta write "[${routineId}] <one-sentence summary>" --tags routine-summary,routine-id:${routineId},fire-delta:${delta.id} --source claude-code:routine\``,
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
    console.log(`  kitty: polling lake for route:claude-code dispatches (last seen: ${state.task_seen_at || state.last_seen})`);
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
