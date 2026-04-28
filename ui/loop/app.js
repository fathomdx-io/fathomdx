// Fragment-loop observatory. Polls the mini-lake every 1.5s and renders:
//   Left column  → live processes (stage indicators + inline content).
//   Right column → chronological mini-lake deltas (seed + stages).
//   Overlay      → SVG bezier lines from input-deltas to process cards,
//                  and from process cards to their emitted stage-deltas.
//
// All state derives from the mini-lake. No out-of-band coordination.

const POLL_MS = 1500;

// Single grand loop — one Fathom mind, one queue, one puddle. The viz
// always watches the global convo; CONVO stays as a constant rather
// than a URL param because the puddle is in-process and single-tenant
// in the fathomdx api.
const CONVO = "grand";
const CONVO_TAG = `convo:${CONVO}`;

// Same-origin api endpoints. The viz is served by the api at /ui/loop/.
const API_BASE = location.origin;

// Bearer auth from the dashboard's existing localStorage key. Every
// puddle endpoint sits behind TokenAuthMiddleware; without a token the
// fetches 401 and the live-dot turns red.
function _authHeaders() {
  const k = localStorage.getItem("fathom-api-key") || "";
  return k ? { Authorization: `Bearer ${k}` } : {};
}

// ── DOM ────────────────────────────────────────────────
const $dot          = document.getElementById("live-dot");
const $sessionLabel = document.getElementById("session-label");
const $statAlive    = document.getElementById("stat-alive");
const $statDeltas   = document.getElementById("stat-deltas");
const $btnPause     = document.getElementById("btn-pause");
const $sndOn        = document.getElementById("snd-on");
const $procBody     = document.getElementById("processes-body");   // may be null (chat layout)
const $lakeBody     = document.getElementById("lake-body");
const $procEmpty    = $procBody ? $procBody.querySelector(".empty") : null;
const $lakeEmpty    = $lakeBody.querySelector(".empty");
const $tplProcess   = document.getElementById("tpl-process");        // may be null (chat layout)
const $tplDelta     = document.getElementById("tpl-delta");
const $links        = document.getElementById("links");
const $graph           = document.getElementById("graph");
// Phase chips were removed from the HTML — convergence dots are the
// only state indicator now. These stubs make the legacy toggle lines
// (e.g. `$settledLabel.hidden = false`) a harmless no-op.
const _stubLabel = () => ({ hidden: true });
const $patternLabel    = document.getElementById("pattern-label")    || _stubLabel();
const $ruminationLabel = document.getElementById("rumination-label") || _stubLabel();
const $maxLabel        = document.getElementById("max-label")        || _stubLabel();
const $contendingLabel = document.getElementById("contending-label") || _stubLabel();
const $settledLabel    = document.getElementById("settled-label")    || _stubLabel();
const $unresolvedLabel = document.getElementById("unresolved-label") || _stubLabel();

// Single similarity line on the graph (thought-stream's mean distance to siblings).
const SIMILARITY_COLOR = "#a78bfa";

$sessionLabel.textContent = `convo ${CONVO}`;

// ── Seed input + reset/stop buttons ────────────────────
const $seedForm   = document.getElementById("seed-form");
const $seedInput  = document.getElementById("seed-input");
const $btnStop    = document.getElementById("btn-stop");
const $btnFire    = document.getElementById("btn-fire");

function newSessionId() {
  // 8 hex chars, matches what the controller's uuid4().hex[:8] produces
  const arr = new Uint8Array(4);
  crypto.getRandomValues(arr);
  return [...arr].map(b => b.toString(16).padStart(2,"0")).join("");
}

// Drop a seed via the api's /v1/puddle/seed endpoint. The experiment's
// viz used to write tagged deltas directly to delta-store; fathomdx
// routes seeds through the api so the puddle module owns intent-shape.
async function postSeed(content) {
  const r = await fetch(`${API_BASE}/v1/puddle/seed`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ..._authHeaders() },
    body: JSON.stringify({ content, kind: "question" }),
  });
  if (!r.ok) throw new Error(`/v1/puddle/seed POST ${r.status}: ${await r.text()}`);
  return r.json();
}

$seedForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const seed = $seedInput.value.trim();
  if (!seed) { $seedInput.focus(); return; }
  try {
    await postSeed(seed);
  } catch (err) {
    alert("Failed to send: " + err.message);
    return;
  }
  $seedInput.value = "";
  $seedInput.blur();
  // Un-settle for the next chorus — back to RGB pulsing.
  if ($panelGraph) {
    $panelGraph.classList.remove("settled");
    $panelGraph.classList.remove("settle-flash");
  }
  if ($convergenceBars) {
    $convergenceBars.classList.remove("settled");
  }
  // Hide stale phase chips from the prior run.
  if ($settledLabel)    $settledLabel.hidden    = true;
  if ($unresolvedLabel) $unresolvedLabel.hidden = true;
  if ($maxLabel)        $maxLabel.hidden        = true;
  patternTs = null;
  ruminationTs = null;
  maxTs = null;
  // Show dots immediately — the seed delta round-trips through the poller
  // in a sec; this avoids an empty pause between submit and the first poll.
  showTypingIndicator();
  // Reset RGB convergence bars for the next chorus.
  resetConvergenceBars();
  metricSeriesByVoice.creator.length = 0;
  metricSeriesByVoice.preserver.length = 0;
  metricSeriesByVoice.destroyer.length = 0;
});

// "new convo" button is now hidden — the grand loop is single-tenant,
// there's nowhere new to navigate to. Hide if it exists in the DOM.
const $btnNewConvo = document.getElementById("btn-new-convo");
if ($btnNewConvo) $btnNewConvo.style.display = "none";

// stop / fire-pulse buttons are deferred — the fathomdx loop runs one
// fixed rotation per fire (no settle detection yet), and there's no
// pressure-watcher to consume a fire-pulse signal. Hide the controls
// until those subsystems land rather than show dead buttons.
if ($btnStop) $btnStop.style.display = "none";
if ($btnFire) $btnFire.style.display = "none";

// ── State ──────────────────────────────────────────────
const processes = new Map();
const deltas    = new Map();
const seenIds   = new Set();

// Per-session accordions. Q (seed) and A (witness) render inline; context
// (crystal/mood/fresh/recent/recall) and voice thoughts pack into per-session
// <details> elements positioned right after the seed card. Keyed by session id.
const sessionBlocks = new Map();
let _lastSeenSid = null;

// A single convo-level "identity" accordion at the top of the lake for
// crystal facets and mood — both are written without session tags
// (convo-scoped) and stable across sessions.
let _convoIdentityDet = null;
const _convoIdentityCounts = { crystal: 0, mood: 0 };

// Mirror deltas (telepathy stream) collect into rolling "batch" accordions
// that are interleaved inline with Q/A in the lake flow. Each Q or A card
// closes the current batch; the next mirror starts a new accordion below
// it. This shows the puddle filling in around the conversation timeline:
//
//   2 new deltas    ← initial telepathy seed
//   Q
//   54 new deltas   ← mirrors that arrived during deliberation
//   A
//   2 new deltas    ← mirrors arriving after answer
let _currentMirrorBatch = null;
// All batches (current + past) so the pruner can sweep expired mirrors
// out of every accordion and remove batches that empty out.
const _mirrorBatches = new Set();
let paused = false;
let lastDeltaTs = null;
// Single metric series — one point per thought emitted.
const metricSeries = [];
// Per-voice metric series, populated only in parliament mode (when metric
// deltas carry a voice:<name> tag). Keyed by voice name → array of {t, d}.
const metricSeriesByVoice = { creator: [], preserver: [], destroyer: [] };
// Voice line colors must match the CSS --voice-* vars.
const VOICE_COLORS = {
  creator:   "#f97316",
  preserver: "#06b6d4",
  destroyer: "#dc2626",
};
let patternTs = null;      // when the monitor announced a stable pattern
let ruminationTs = null;   // when the monitor announced rumination (near-identical)
let maxTs = null;          // when the spawn loop hit MAX_PROCESSES without pattern

// ── Audio (subtle ticks per new delta) ─────────────────
let audioCtx = null;
function ensureAudio() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return audioCtx;
}
const STAGE_FREQ = {
  "thought":  415,  // G#
  "seed":     330,  // E low
  "synthesis": 880, // A high
  "spawn":    196,  // G very low
  "die":      147,  // D very low
};
function tick(kind) {
  if (!$sndOn.checked) return;
  const ctx = ensureAudio();
  const freq = STAGE_FREQ[kind] || 400;
  const t0 = ctx.currentTime;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "sine";
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(0.09, t0 + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.18);
  osc.connect(gain).connect(ctx.destination);
  osc.start(t0);
  osc.stop(t0 + 0.2);
}

// ── API ────────────────────────────────────────────────
// Reads everything in the puddle for the global convo. The /v1/puddle/deltas
// endpoint mirrors delta-store's /deltas shape (tag filters, return list of
// dicts) so the rest of the viz didn't need adapting.
async function fetchLake() {
  const u = `${API_BASE}/v1/puddle/deltas?tags_include=${encodeURIComponent(CONVO_TAG)}&limit=1000`;
  const r = await fetch(u, { headers: _authHeaders() });
  if (!r.ok) throw new Error(`/v1/puddle/deltas ${r.status}`);
  return r.json();
}

// ── Helpers ────────────────────────────────────────────
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function getPid(tags) {
  const t = tags.find(t => t.startsWith("process:"));
  return t ? t.slice("process:".length) : null;
}
function getStage(tags) {
  const t = tags.find(t => t.startsWith("stage:"));
  return t ? t.slice("stage:".length) : null;
}
function getEvent(tags) {
  const t = tags.find(t => t.startsWith("event:"));
  return t ? t.slice("event:".length) : null;
}
function getVoice(tags) {
  const t = tags.find(t => t.startsWith("voice:"));
  return t ? t.slice("voice:".length) : null;
}
function getSessionId(tags) {
  const t = tags.find(t => t.startsWith("session:"));
  return t ? t.slice("session:".length) : null;
}

// ── Session accordions ─────────────────────────────────
function ensureSessionBlock(sid) {
  if (sessionBlocks.has(sid)) return sessionBlocks.get(sid);
  const contextDet = document.createElement("details");
  contextDet.className = "accordion accordion-context";
  contextDet.style.display = "none";
  contextDet.innerHTML =
    '<summary><span class="accordion-label">context</span>' +
    '<span class="accordion-stats"></span></summary>' +
    '<div class="accordion-body"></div>';
  const voicesDet = document.createElement("details");
  voicesDet.className = "accordion accordion-voices";
  voicesDet.style.display = "none";
  voicesDet.innerHTML =
    '<summary><span class="accordion-label">deliberation</span>' +
    '<span class="accordion-stats"></span></summary>' +
    '<div class="accordion-body"></div>';
  $lakeBody.appendChild(contextDet);
  $lakeBody.appendChild(voicesDet);
  const block = {
    sid,
    contextDet,
    voicesDet,
    counts: {
      crystal: 0, mood: 0, fresh: 0, recent: 0, recall: 0,
      now: 0, interim: 0, murmur: 0, memorySaved: 0,
      creator: 0, preserver: 0, destroyer: 0, otherThought: 0,
    },
  };
  sessionBlocks.set(sid, block);
  return block;
}

function ensureConvoIdentityBlock() {
  if (_convoIdentityDet) return _convoIdentityDet;
  const det = document.createElement("details");
  det.className = "accordion accordion-context accordion-identity";
  det.style.display = "none";
  det.innerHTML =
    '<summary><span class="accordion-label">identity</span>' +
    '<span class="accordion-stats"></span></summary>' +
    '<div class="accordion-body"></div>';
  // Pin to top of lake (after the empty-state placeholder).
  $lakeBody.insertBefore(det, $lakeEmpty.nextSibling);
  _convoIdentityDet = det;
  return det;
}

function updateConvoIdentityStats() {
  if (!_convoIdentityDet) return;
  const c = _convoIdentityCounts;
  const parts = [];
  if (c.crystal) parts.push(`${c.crystal} crystal facet${c.crystal === 1 ? "" : "s"}`);
  if (c.mood)    parts.push(`${c.mood} mood`);
  _convoIdentityDet.querySelector(".accordion-stats").textContent = parts.join(" · ");
  _convoIdentityDet.style.display = parts.length ? "" : "none";
}

function ensureMirrorBatchBlock() {
  if (_currentMirrorBatch) return _currentMirrorBatch;
  const det = document.createElement("details");
  det.className = "accordion accordion-mirror-batch";
  det.innerHTML =
    '<summary><span class="accordion-stats"></span></summary>' +
    '<div class="accordion-body"></div>';
  // Insert before the convergence-bars sentinel if present, otherwise append.
  if ($convergenceBars && $convergenceBars.parentNode === $lakeBody) {
    $lakeBody.insertBefore(det, $convergenceBars);
  } else {
    $lakeBody.appendChild(det);
  }
  _currentMirrorBatch = {
    det,
    body: det.querySelector(".accordion-body"),
    label: det.querySelector(".accordion-stats"),
    count: 0,
  };
  _mirrorBatches.add(_currentMirrorBatch);
  return _currentMirrorBatch;
}

function updateMirrorBatchLabel(batch) {
  batch.label.textContent =
    `${batch.count} new delta${batch.count === 1 ? "" : "s"}`;
}

// Close the current mirror batch: a Q or A card landed, so the next mirror
// should start a fresh accordion below it.
function closeMirrorBatch() {
  _currentMirrorBatch = null;
}


// Unified prune: drop any rendered delta whose age exceeds its data-ttl-ms
// (matches the server's expires_at), then remove any accordion whose body
// is now empty. Crystal/mood deltas have no ttlMs and are never pruned —
// they're durable on the server too.
function pruneExpiredDeltas() {
  const now = Date.now();

  // 1. Sweep individual delta elements by their per-kind TTL.
  for (const el of Array.from(document.querySelectorAll("[data-ttl-ms]"))) {
    const id = el.dataset && el.dataset.id;
    const meta = id ? deltas.get(id) : null;
    if (!meta) continue;
    const ttl = parseInt(el.dataset.ttlMs, 10);
    if (!ttl) continue;
    const age = now - new Date(meta.timestamp).getTime();
    if (age <= ttl) continue;

    // Decrement the right counter on the parent block, if any.
    const cls = el.className || "";
    let blockSid = null;
    for (const [sid, blk] of sessionBlocks) {
      if (blk.voicesDet && blk.voicesDet.contains(el)) { blockSid = sid; break; }
      if (blk.contextDet && blk.contextDet.contains(el)) { blockSid = sid; break; }
    }
    if (blockSid) {
      const c = sessionBlocks.get(blockSid).counts;
      if (cls.includes("voice-creator"))        c.creator     = Math.max(0, c.creator     - 1);
      else if (cls.includes("voice-preserver")) c.preserver   = Math.max(0, c.preserver   - 1);
      else if (cls.includes("voice-destroyer")) c.destroyer   = Math.max(0, c.destroyer   - 1);
      else if (cls.includes("recall"))          c.recall      = Math.max(0, c.recall      - 1);
      else if (cls.includes("interim murmur"))  c.murmur      = Math.max(0, c.murmur      - 1);
      else if (cls.includes("interim"))         c.interim     = Math.max(0, c.interim     - 1);
      else if (cls.includes("memory-saved"))    c.memorySaved = Math.max(0, c.memorySaved - 1);
      else if (cls.includes("now"))             c.now         = Math.max(0, c.now         - 1);
    }
    // Mirror batches track count on the batch object.
    for (const batch of _mirrorBatches) {
      if (batch.body.contains(el)) {
        batch.count = Math.max(0, batch.count - 1);
        updateMirrorBatchLabel(batch);
        break;
      }
    }

    el.remove();
    deltas.delete(id);
    if (blockSid) updateBlockStats(blockSid);
  }

  // 2. Drop empty mirror batches.
  for (const batch of Array.from(_mirrorBatches)) {
    if (batch.body.children.length === 0) {
      batch.det.remove();
      _mirrorBatches.delete(batch);
      if (batch === _currentMirrorBatch) _currentMirrorBatch = null;
    }
  }

  // 3. Drop empty session-block accordions (deliberation + per-session
  //    context). When both halves are empty, drop the block from the Map
  //    so a future delta in the same session can re-create them clean.
  //    Identity (convo-level crystal+mood) stays — durable on server,
  //    pinned to the top of the lake.
  for (const [sid, block] of Array.from(sessionBlocks)) {
    const voicesEmpty  = !block.voicesDet  || block.voicesDet.querySelector(".accordion-body").children.length === 0;
    const contextEmpty = !block.contextDet || block.contextDet.querySelector(".accordion-body").children.length === 0;
    if (voicesEmpty && block.voicesDet && block.voicesDet.parentNode) {
      block.voicesDet.remove();
    }
    if (contextEmpty && block.contextDet && block.contextDet.parentNode) {
      block.contextDet.remove();
    }
    if (voicesEmpty && contextEmpty) {
      sessionBlocks.delete(sid);
    }
  }
}

function updateBlockStats(sid) {
  const block = sessionBlocks.get(sid);
  if (!block) return;
  const c = block.counts;

  const ctxParts = [];
  if (c.recall)  ctxParts.push(`${c.recall} moment${c.recall === 1 ? "" : "s"} recalled`);
  if (c.interim) ctxParts.push(`${c.interim} interim`);
  if (c.murmur)  ctxParts.push(`${c.murmur} murmur`);
  if (c.memorySaved) ctxParts.push(`${c.memorySaved} saved`);
  block.contextDet.querySelector(".accordion-stats").textContent = ctxParts.join(" · ");
  block.contextDet.style.display = ctxParts.length ? "" : "none";

  const total = c.creator + c.preserver + c.destroyer + c.otherThought;
  const voiceParts = [];
  if (total) voiceParts.push(`${total} thought${total === 1 ? "" : "s"}`);
  if (c.creator)   voiceParts.push(`${c.creator} creator`);
  if (c.preserver) voiceParts.push(`${c.preserver} preserver`);
  if (c.destroyer) voiceParts.push(`${c.destroyer} destroyer`);
  block.voicesDet.querySelector(".accordion-stats").textContent = voiceParts.join(" · ");
  block.voicesDet.style.display = total ? "" : "none";
}

// ── Render: Mini-lake column ───────────────────────────
function renderDelta(d) {
  if (seenIds.has(d.id)) return;
  const tags = d.tags || [];

  // Skip internal-only deltas from the right column.
  if (tags.includes("process-event") ||
      tags.includes("metric") ||
      tags.includes("pattern-boundary") ||
      tags.includes("rumination-boundary") ||
      tags.includes("max-boundary") ||
      tags.includes("tool-event") ||
      tags.includes("tool-intent") ||
      tags.includes("loop-control:start") ||
      tags.includes("loop-control:stop") ||
      tags.includes("loop-control:init") ||
      tags.includes("loop-control:fire-pulse") ||
      tags.includes("convo-initialized")) {
    seenIds.add(d.id);
    return;
  }

  const isSeed       = tags.includes("seed");
  const isSynthesis  = tags.includes("synthesis");
  const isInterim    = tags.includes("interim");
  const isMurmur     = tags.includes("murmur");
  const isMemorySaved= tags.includes("memory-saved");
  const isThought    = tags.includes("thought");
  const isCrystal    = tags.includes("crystal");
  const isMood       = tags.includes("mood");
  const isRecent     = tags.includes("recent");
  const isFresh      = tags.includes("fresh");
  const isNow        = tags.includes("now");
  const isRecall     = tags.includes("recall-result");
  const isMirror     = tags.includes("mirror");  // telepathy stream
  const isPattern    = tags.includes("pattern-phase");
  const isRumination = tags.includes("rumination-phase");
  // Grand-loop additions:
  const isIntent     = tags.includes("intent");
  const intentKindTag = tags.find(t => t.startsWith("kind:"));
  const intentKindStr = intentKindTag ? intentKindTag.slice("kind:".length) : null;
  const isFeedCard   = tags.includes("feed-card");
  const isDm         = tags.includes("dm");
  const isAlert      = tags.includes("alert");
  const isNeifama    = tags.includes("neifama");
  const isRoutineFire = tags.includes("routine-fire");
  const isToolCall   = tags.includes("tool-call");
  const pid          = getPid(tags);

  let kind = "";
  let cssClass = "";
  if (isCrystal) {
    const facetTag = tags.find(t => t.startsWith("facet:"));
    const facetName = facetTag ? facetTag.slice(6).replace(/-/g, " ") : "facet";
    kind = `◆ ${facetName}`;
    cssClass = "crystal";
    tick("seed");
  } else if (isNow) {
    kind = "⌚ now";
    cssClass = "now";
    tick("seed");
  } else if (isMood) {
    const feelingTag = tags.find(t => t.startsWith("feeling:"));
    const feeling = feelingTag ? feelingTag.slice("feeling:".length) : "mood";
    kind = `♥ ${feeling}`;
    cssClass = "mood";
    tick("seed");
  } else if (isRecall) {
    const klassTag = tags.find(t => t.startsWith("recall-class:"));
    const klass = klassTag ? klassTag.slice("recall-class:".length) : "first";
    const resonantTag = tags.find(t => t.startsWith("resonant:"));
    const srcTag = tags.find(t => t.startsWith("from-source:"));
    const src = srcTag ? srcTag.slice("from-source:".length) : "?";
    if (resonantTag) {
      const facet = resonantTag.slice("resonant:".length).replace(/-/g, " ");
      kind = `↯ resonant · ${facet} · ${src}`;
      cssClass = "recall recall-resonant";
    } else {
      const relTag = tags.find(t => t.startsWith("relation:"));
      const rel = relTag ? relTag.slice("relation:".length).replace(/-/g, " ") : "recall";
      kind = `↺ ${klass} · ${rel} · ${src}`;
      cssClass = `recall recall-${klass}`;
    }
    tick("seed");
  } else if (isFresh) {
    const srcTag = tags.find(t => t.startsWith("from-source:"));
    const src = srcTag ? srcTag.slice("from-source:".length) : "lake";
    kind = `≈ ${src}`;
    cssClass = "fresh";
    tick("seed");
  } else if (isRecent) {
    const srcTag = tags.find(t => t.startsWith("from-source:"));
    const src = srcTag ? srcTag.slice("from-source:".length) : "lake";
    kind = `~ ${src}`;
    cssClass = "recent";
    tick("seed");
  } else if (isIntent && intentKindStr && intentKindStr !== "question") {
    // Non-question intent (resonance / drop-in / pressure / alert /
    // routine-due) — first-class in the chat feed with a kind kicker.
    const kindLabel = intentKindStr.replace(/-/g, " ");
    kind = `⌑ intent · ${kindLabel}`;
    cssClass = `intent intent-${intentKindStr}`;
    tick("seed");
  } else if (isNeifama) {
    kind = "∅ NEIFAMA";
    cssClass = "neifama";
    tick("synthesis");
  } else if (isDm) {
    const toTag = tags.find(t => t.startsWith("to:"));
    const to = toTag ? toTag.slice(3) : "?";
    kind = `✉ dm · ${to}`;
    cssClass = "synthesis dm";
    tick("synthesis");
  } else if (isAlert) {
    const lvlTag = tags.find(t => t.startsWith("alert-level:"));
    const lvl = lvlTag ? lvlTag.slice("alert-level:".length) : "?";
    kind = `⚡ alert · ${lvl}`;
    cssClass = "synthesis alert feed-card";
    tick("synthesis");
  } else if (isRoutineFire) {
    const ridTag = tags.find(t => t.startsWith("routine-id:"));
    const rid = ridTag ? ridTag.slice("routine-id:".length) : "?";
    kind = `▷ routine · ${rid}`;
    cssClass = "synthesis routine";
    tick("synthesis");
  } else if (isToolCall) {
    const toolTag = tags.find(t => t.startsWith("tool:"));
    const tool = toolTag ? toolTag.slice("tool:".length) : "?";
    kind = `⚙ tool · ${tool}`;
    cssClass = "synthesis tool-call";
    tick("synthesis");
  } else if (isSeed) {
    kind = "Q";
    cssClass = "seed";
    tick("seed");
  } else if (isSynthesis && isFeedCard) {
    // Chat-reply route — answer to a Q. Render as plain text response,
    // no kicker, no axes, no card boxing. Just the body, like a chat
    // message. Class `chat-reply` strips the feed-card treatment.
    kind = "";
    cssClass = "synthesis chat-reply";
    tick("synthesis");
  } else if (isFeedCard) {
    // Routed feed-card (no `synthesis` tag — durable observation).
    kind = "▶ feed-card";
    cssClass = "synthesis feed-card";
    tick("synthesis");
  } else if (isSynthesis) {
    kind = "A";
    cssClass = "synthesis";
    tick("synthesis");
  } else if (isInterim) {
    kind = "⋯ still thinking";
    cssClass = "interim";
    tick("seed");
  } else if (isMurmur) {
    kind = "♪ murmur";
    cssClass = "interim murmur";
    tick("synthesis");
  } else if (isMemorySaved) {
    kind = "▣ saved";
    cssClass = "recall memory-saved";
    tick("seed");
  } else if (isThought) {
    const phaseClass = isRumination ? " rumination" : (isPattern ? " pattern" : "");
    const voice = getVoice(tags);
    const voiceClass = voice ? ` voice-${voice}` : "";
    const voicePrefix = voice ? `${voice} · ` : "";
    kind = `· ${voicePrefix}${pid ? pid.slice(0,6) : "?"}`;
    cssClass = `thought${phaseClass}${voiceClass}`;
    tick("thought");
  } else {
    kind = "delta";
  }

  const node = $tplDelta.content.cloneNode(true);
  const $art = node.querySelector(".delta");
  $art.dataset.id = d.id;
  if (pid) $art.dataset.pid = pid;

  // Stamp the per-kind server TTL onto the element so the pruner can
  // sweep it once the lake has reaped it. Crystal/mood are durable on
  // the server (no expires_at), so no ttlMs — pruner skips anything
  // without the attribute. Mirror is checked before recall because
  // mirrors carry recall-result tag too and we want mirror semantics.
  const _isVoiceThought_ttl = isThought && !!getVoice(tags);
  let ttlMs = 0;
  if (_isVoiceThought_ttl)    ttlMs = 2 * 60 * 1000;   // POEM_TTL_S
  else if (isMirror)          ttlMs = 5 * 60 * 1000;   // MIRROR_TTL_S
  else if (isRecall)          ttlMs = 5 * 60 * 1000;   // RECALL_TTL_S
  else if (isMemorySaved)     ttlMs = 2 * 60 * 1000;   // EPHEMERAL_TTL_S
  else if (isNow)             ttlMs = 2 * 60 * 1000;   // EPHEMERAL_TTL_S
  else if (isMurmur)          ttlMs = 30 * 60 * 1000;  // Q_A_TTL_S
  else if (isInterim)         ttlMs = 30 * 60 * 1000;  // Q_A_TTL_S
  else if (isNeifama)         ttlMs = 30 * 60 * 1000;  // Q_A_TTL_S
  else if (isDm || isAlert || isRoutineFire || isToolCall)
                              ttlMs = 30 * 60 * 1000;  // Q_A_TTL_S
  // Intents — match INTENT_TTL_BY_KIND on the worker.
  else if (isIntent && intentKindStr === "question")        ttlMs = 30 * 60 * 1000;
  else if (isIntent && intentKindStr === "alert")           ttlMs = 15 * 60 * 1000;
  else if (isIntent && intentKindStr === "routine-due")     ttlMs = 30 * 60 * 1000;
  else if (isIntent)                                        ttlMs = 5 * 60 * 1000;
  else if (isSeed)            ttlMs = 30 * 60 * 1000;  // Q_A_TTL_S
  else if (isSynthesis)       ttlMs = 30 * 60 * 1000;  // Q_A_TTL_S
  else if (isFeedCard)        ttlMs = 30 * 60 * 1000;  // Q_A_TTL_S
  if (ttlMs > 0) $art.dataset.ttlMs = String(ttlMs);
  for (const c of cssClass.split(/\s+/).filter(Boolean)) $art.classList.add(c);
  $art.querySelector(".d-kind").textContent = kind;
  $art.querySelector(".d-ts").textContent = fmtTime(d.timestamp);
  const $body = $art.querySelector(".d-body");
  const _classes = cssClass.split(/\s+/);
  if (_classes.includes("chat-reply")) {
    // Chat reply — just the body, no kicker/axes/level. Reads as plain
    // chat text, like a person answering. JSON content from witness has
    // a `body` field; parse and pull just that.
    try {
      const payload = JSON.parse(d.content);
      $body.textContent = (payload.body || "").trim() || d.content;
    } catch {
      $body.textContent = d.content;
    }
  } else if (_classes.includes("feed-card")) {
    // Feed-card — full card schema: kicker / title / body / tail /
    // body_image / link / level + axes meta. Mirrors prod's curated
    // feed surface so this output is structurally drop-in compatible.
    try {
      const payload = JSON.parse(d.content);
      $body.innerHTML = "";

      const $kicker = document.createElement("div");
      $kicker.className = "feed-card-kicker";
      $kicker.textContent = payload.kicker || "";
      $body.appendChild($kicker);

      if (payload.title) {
        const $title = document.createElement("div");
        $title.className = "feed-card-title";
        $title.textContent = payload.title;
        $body.appendChild($title);
      }

      if (payload.body_image) {
        const $img = document.createElement("img");
        $img.className = "feed-card-image";
        // body_image may be a media_hash or URL. fathomdx's lake serves
        // hash-keyed media at /v1/media/<hash>; the dashboard's existing
        // image path. If the value already looks like a URL or absolute
        // path, pass through unchanged.
        const v = payload.body_image;
        $img.src = (v.startsWith("http") || v.startsWith("/")) ? v : `${API_BASE}/v1/media/${v}`;
        $img.alt = "";
        $img.loading = "lazy";
        $img.addEventListener("error", () => $img.style.display = "none");
        $body.appendChild($img);
      }

      const $cardBody = document.createElement("div");
      $cardBody.className = "feed-card-body";
      $cardBody.textContent = payload.body || "";
      $body.appendChild($cardBody);

      if (payload.link || (payload.links && payload.links.length)) {
        const $linkRow = document.createElement("div");
        $linkRow.className = "feed-card-links";
        const allLinks = [];
        if (payload.link) allLinks.push(payload.link);
        if (Array.isArray(payload.links)) allLinks.push(...payload.links);
        for (const url of allLinks) {
          const $a = document.createElement("a");
          $a.href = url;
          $a.target = "_blank";
          $a.rel = "noreferrer";
          $a.textContent = url;
          $linkRow.appendChild($a);
        }
        $body.appendChild($linkRow);
      }

      if (payload.tail) {
        const $tail = document.createElement("div");
        $tail.className = "feed-card-tail";
        $tail.textContent = payload.tail;
        $body.appendChild($tail);
      }

      const $meta = document.createElement("div");
      $meta.className = "feed-card-meta";
      const lvl = payload.level || "?";
      const $lvl = document.createElement("span");
      $lvl.className = `level level-${lvl}`;
      $lvl.textContent = lvl;
      $meta.appendChild($lvl);
      const settledTxt = payload.settled
        ? `settled @ ${payload.settle_level != null ? payload.settle_level.toFixed(2) : "?"}`
        : "unresolved";
      const $settled = document.createElement("span");
      $settled.textContent = settledTxt;
      $meta.appendChild($settled);
      const a = payload.axes || {};
      const axesShort = ["salience","novelty","resonance","confidence","comfort"]
        .map(k => `${k[0]}${a[k] != null ? a[k].toFixed(2) : "?"}`)
        .join(" ");
      const $axes = document.createElement("span");
      $axes.textContent = axesShort;
      $meta.appendChild($axes);
      $body.appendChild($meta);
    } catch {
      $body.textContent = d.content;
    }
  } else {
    $body.textContent = d.content;
  }

  $art.addEventListener("mouseenter", () => highlightForDelta(d.id));
  $art.addEventListener("mouseleave", clearHighlights);

  $lakeEmpty.style.display = "none";

  // Decide where this delta lives:
  //   Q (seed) and A (synthesis) → inline in the lake (the visible chat).
  //   Voice thoughts             → per-session deliberation accordion.
  //   Context (crystal/mood/etc) → per-session context accordion.
  //   Anything else              → inline.
  const sid = getSessionId(tags) || _lastSeenSid;
  if (sid) _lastSeenSid = sid;

  const isVoiceThought = isThought && getVoice(tags);
  const isContext = isCrystal || isMood || isFresh || isRecent || isRecall ||
                    isNow || isInterim || isMurmur || isMemorySaved;

  // First-class landings — Q/A, intents (non-question), routed outputs.
  // All close the current mirror batch and render inline so they're
  // visible in the lake's chronological flow.
  const isFirstClass =
    isSeed || isSynthesis ||
    (isIntent && intentKindStr && intentKindStr !== "question") ||
    isNeifama || isDm || isAlert || isRoutineFire || isToolCall ||
    (isFeedCard && !isSynthesis);

  if (isFirstClass) {
    closeMirrorBatch();
    $lakeBody.appendChild($art);
    if (isSeed && sid) ensureSessionBlock(sid);
  } else if (isCrystal || isMood) {
    // Convo-level identity (crystal facets + current mood). Pinned at top.
    const det = ensureConvoIdentityBlock();
    det.querySelector(".accordion-body").appendChild($art);
    if (isCrystal) _convoIdentityCounts.crystal++;
    else if (isMood) _convoIdentityCounts.mood++;
    updateConvoIdentityStats();
  } else if (isMirror) {
    // Vampire-tap mirror — accumulates into the current batch accordion,
    // interleaved with Q/A in the lake's chronological flow.
    const batch = ensureMirrorBatchBlock();
    batch.body.appendChild($art);
    batch.count++;
    updateMirrorBatchLabel(batch);
  } else if (isVoiceThought && sid) {
    const block = ensureSessionBlock(sid);
    block.voicesDet.querySelector(".accordion-body").appendChild($art);
    const v = getVoice(tags);
    if (v === "creator")        block.counts.creator++;
    else if (v === "preserver") block.counts.preserver++;
    else if (v === "destroyer") block.counts.destroyer++;
    else                        block.counts.otherThought++;
    updateBlockStats(sid);
  } else if (isContext && sid) {
    const block = ensureSessionBlock(sid);
    block.contextDet.querySelector(".accordion-body").appendChild($art);
    if (isRecall)             block.counts.recall++;
    else if (isInterim)       block.counts.interim++;
    else if (isMurmur)        block.counts.murmur++;
    else if (isMemorySaved)   block.counts.memorySaved++;
    else if (isNow)           block.counts.now++;
    updateBlockStats(sid);
  } else {
    // No session context — render inline.
    $lakeBody.appendChild($art);
  }

  // Dots show whenever voices are actively deliberating. Two triggers:
  //   1. A fresh intent landed (Q from composer or any non-question
  //      intent — reflection/drift/bridging/alert/resonance/drop-in).
  //      This catches the moment a tick is about to start.
  //   2. A voice thought landed (recent-ish). This is the live signal —
  //      voices ARE deliberating right now. Critical when ticks chain
  //      back-to-back: the first tick's output hides dots, the second
  //      tick's voices keep spawning, and we need to re-show dots based
  //      on actual activity (not on "did an intent JUST land").
  // Both gated on age < 60s so page-load echoes of past ticks don't
  // flash the dots inappropriately.
  const isTriggerIntent =
    isSeed || (isIntent && intentKindStr && intentKindStr !== "question");
  const _age = Date.now() - new Date(d.timestamp).getTime();
  if (isTriggerIntent && _age < 60_000) showTypingIndicator();
  if (isVoiceThought && _age < 60_000) showTypingIndicator();

  // Routed output landed → drop the dots. Any route counts: chat-reply,
  // feed-card, NEIFAMA, dm, alert, routine-fire, tool-call.
  const isRoutedOutput =
    isSynthesis || isNeifama || isDm || isAlert ||
    isRoutineFire || isToolCall || (isFeedCard && !isSynthesis);
  if (isRoutedOutput) hideTypingIndicator();

  const recordKind = isSeed ? "seed" : (isSynthesis ? "synthesis" : (isThought ? "thought" : "delta"));
  deltas.set(d.id, {
    el: $art,
    kind: recordKind,
    pid,
    timestamp: d.timestamp,
    content: d.content,
  });
  seenIds.add(d.id);
  lastDeltaTs = d.timestamp;

  // Auto-scroll if already near the bottom.
  // Keep the convergence dots pinned at the bottom (last child of the
  // lake body) every time anything renders. Then auto-scroll the lake
  // to the bottom so new content stays visible.
  if ($convergenceBars && $convergenceBars.parentNode === $lakeBody) {
    $lakeBody.appendChild($convergenceBars);
  }
  $lakeBody.scrollTop = $lakeBody.scrollHeight;
}

// ── Render: Processes column ───────────────────────────
function ensureProcessCard(pid, spawnedAt, inputIds, voice = null) {
  if (processes.has(pid)) return processes.get(pid);
  // Chat layout has no processes pane — record minimal bookkeeping for
  // input-id linking, skip DOM construction.
  if (!$tplProcess || !$procBody) {
    const proc = {
      pid, el: null, spawnedAt, deadAt: null,
      inputIds: inputIds || [],
      stages: {}, outputIds: {},
    };
    processes.set(pid, proc);
    return proc;
  }
  const node = $tplProcess.content.cloneNode(true);
  const $art = node.querySelector(".process");
  $art.dataset.pid = pid;
  if (voice) {
    $art.classList.add(`voice-${voice}`);
    $art.dataset.voice = voice;
  }
  $art.querySelector(".pid").textContent = pid;
  $art.querySelector(".proc-age").textContent = fmtTime(spawnedAt);

  $art.addEventListener("mouseenter", () => highlightForProcess(pid));
  $art.addEventListener("mouseleave", clearHighlights);

  $procEmpty.style.display = "none";
  $procBody.appendChild($art);

  const proc = {
    el: $art,
    pid,
    spawnedAt,
    deadAt: null,
    inputIds: inputIds || [],
    stages: {},        // stage -> content
    outputIds: {},     // stage -> deltaId
  };
  processes.set(pid, proc);
  tick("spawn");
  return proc;
}

function updateProcessThought(proc, content, deltaId) {
  proc.thoughtContent = content;
  proc.outputIds.thought = deltaId;
  if (!proc.el) return; // chat layout — no DOM card to populate
  const $body = proc.el.querySelector(".thought-content");
  if ($body) $body.textContent = content;
}

function markProcessDead(proc, diedAt) {
  proc.deadAt = diedAt;
  tick("die");
  // Obliterate immediately — we only show active loops.
  if (proc.el && proc.el.parentNode) proc.el.parentNode.removeChild(proc.el);
  processes.delete(proc.pid);
  drawLinks();
}

// ── Highlights ─────────────────────────────────────────
function highlightForProcess(pid) {
  clearHighlights();
  const proc = processes.get(pid);
  if (!proc) return;
  for (const id of proc.inputIds) {
    const d = deltas.get(id);
    if (d) d.el.classList.add("highlighted");
  }
  for (const id of Object.values(proc.outputIds)) {
    const d = deltas.get(id);
    if (d) d.el.classList.add("highlighted");
  }
}
function highlightForDelta(id) {
  clearHighlights();
  // Highlight processes that used this delta as input OR that produced it.
  for (const proc of processes.values()) {
    if (!proc.el) continue;
    if (proc.inputIds.includes(id) || Object.values(proc.outputIds).includes(id)) {
      proc.el.classList.add("highlighted");
    }
  }
}
function clearHighlights() {
  document.querySelectorAll(".highlighted").forEach(el => el.classList.remove("highlighted"));
}

// ── SVG link drawing ──────────────────────────────────
function drawLinks() {
  // No process cards in chat layout — nothing to link from.
  if (!$procBody) {
    if ($links) $links.innerHTML = "";
    return;
  }
  const stage = document.getElementById("stage-root");
  const rect = stage.getBoundingClientRect();
  $links.setAttribute("viewBox", `0 0 ${rect.width} ${rect.height}`);

  const paths = [];
  for (const proc of processes.values()) {
    if (!proc.el) continue;
    const pr = proc.el.getBoundingClientRect();
    const px = pr.right - rect.left - 6;   // right edge of process card
    const py = pr.top + pr.height / 2 - rect.top;

    // Input links
    for (const id of proc.inputIds) {
      const d = deltas.get(id);
      if (!d) continue;
      const dr = d.el.getBoundingClientRect();
      if (dr.bottom < rect.top || dr.top > rect.bottom) continue;
      const dx = dr.left - rect.left + 6;  // left edge of delta card
      const dy = dr.top + dr.height / 2 - rect.top;
      const cx1 = px + 80;
      const cx2 = dx - 80;
      paths.push(`<path class="input ${proc.deadAt ? "fade":""}" d="M ${px} ${py} C ${cx1} ${py}, ${cx2} ${dy}, ${dx} ${dy}" />`);
    }

    // Output links
    for (const id of Object.values(proc.outputIds)) {
      const d = deltas.get(id);
      if (!d) continue;
      const dr = d.el.getBoundingClientRect();
      if (dr.bottom < rect.top || dr.top > rect.bottom) continue;
      const dx = dr.left - rect.left + 6;
      const dy = dr.top + dr.height / 2 - rect.top;
      const cx1 = px + 80;
      const cx2 = dx - 80;
      paths.push(`<path class="output ${proc.deadAt ? "fade":""}" d="M ${px} ${py} C ${cx1} ${py}, ${cx2} ${dy}, ${dx} ${dy}" />`);
    }
  }
  $links.innerHTML = paths.join("");
}

// ── Poll loop ──────────────────────────────────────────
async function poll() {
  if (paused) return;
  try {
    const raw = await fetchLake();
    raw.reverse(); // chronological
    $statDeltas.textContent = raw.length;
    $dot.classList.remove("error");
    $dot.classList.add("live");

    // Pass 1: spawn events → create process cards.
    for (const d of raw) {
      const tags = d.tags || [];
      if (tags.includes("process-event") && getEvent(tags) === "spawn") {
        // Only handle each spawn event ONCE per page load. Without this
        // guard, the every-poll re-fetch combined with markProcessDead
        // deleting the pid from the Map causes spawn events to be
        // re-processed each cycle — re-creating the proc and re-firing
        // tick("spawn"), audible as a 1Hz thump when sound is on.
        if (seenIds.has(d.id)) continue;
        seenIds.add(d.id);
        const pid = getPid(tags);
        let parsed = {};
        try { parsed = JSON.parse(d.content); } catch {/*ignore*/}
        const inputIds = parsed.input_ids || [];
        const voice = getVoice(tags) || parsed.voice || null;
        if (pid && !processes.has(pid)) {
          ensureProcessCard(pid, d.timestamp, inputIds, voice);
        }
      }
    }

    // Pass 2: stage + seed + synthesis deltas. Also collects metric deltas
    // and plateau markers for the graph.
    for (const d of raw) {
      const tags = d.tags || [];
      if (tags.includes("process-event")) continue;

      // Metric deltas → graph series, not the main lake column.
      if (tags.includes("metric")) {
        if (!seenIds.has(d.id)) {
          try {
            const payload = JSON.parse(d.content);
            const dist = payload.distance;
            if (typeof dist === "number") {
              const tMs = new Date(d.timestamp).getTime();
              metricSeries.push({ t: tMs, d: dist });
              const voice = getVoice(tags) || payload.voice || null;
              if (voice && metricSeriesByVoice[voice]) {
                metricSeriesByVoice[voice].push({ t: tMs, d: dist });
                // Parliament mode detected (voice-tagged metric). If the
                // session hasn't yet hit pattern/max, mark CONTENDING.
                if (!patternTs && !maxTs) $contendingLabel.hidden = false;
                // Slide the corresponding convergence bar to its new x.
                updateConvergenceBars();
              }
            }
          } catch {/*ignore*/}
          seenIds.add(d.id);
        }
        continue;
      }

      // Phase boundaries → markers on the graph.
      if (tags.includes("pattern-boundary")) {
        if (!seenIds.has(d.id)) {
          patternTs = new Date(d.timestamp).getTime();
          // Parliament: pattern-boundary == settled.
          $settledLabel.hidden = false;
          $contendingLabel.hidden = true;
          triggerSettleAnimation();
          seenIds.add(d.id);
        }
        continue;
      }
      if (tags.includes("rumination-boundary")) {
        if (!seenIds.has(d.id)) {
          ruminationTs = new Date(d.timestamp).getTime();
          $ruminationLabel.hidden = false;
          seenIds.add(d.id);
        }
        continue;
      }
      if (tags.includes("max-boundary")) {
        if (!seenIds.has(d.id)) {
          maxTs = new Date(d.timestamp).getTime();
          $maxLabel.hidden = false;
          // Parliament: max-boundary without prior settle == unresolved.
          if (!patternTs) $unresolvedLabel.hidden = false;
          $contendingLabel.hidden = true;
          seenIds.add(d.id);
        }
        continue;
      }

      renderDelta(d);

      const pid = getPid(tags);
      if (pid && tags.includes("thought")) {
        const proc = processes.get(pid);
        if (proc) updateProcessThought(proc, d.content, d.id);
      }
    }

    // Pass 3: process deaths.
    for (const d of raw) {
      const tags = d.tags || [];
      if (tags.includes("process-event") && getEvent(tags) === "die") {
        // Same idempotency guard as spawn — without this the die tick
        // also re-fires every poll on past sessions still in the lake.
        if (seenIds.has(d.id)) continue;
        seenIds.add(d.id);
        const pid = getPid(tags);
        const proc = processes.get(pid);
        if (proc && !proc.deadAt) markProcessDead(proc, d.timestamp);
      }
    }

    // Pass 4: reap. Anything in our DOM whose id is no longer in the lake
    // (because expires_at fired) needs to be removed. Synthesis and seeds
    // are durable so they stay; everything else may fade.
    const liveIds = new Set(raw.map(d => d.id));
    for (const [id, info] of [...deltas.entries()]) {
      if (!liveIds.has(id)) {
        if (info.el && info.el.parentNode) info.el.parentNode.removeChild(info.el);
        deltas.delete(id);
        seenIds.delete(id);
      }
    }

    // Stats.
    const alive = [...processes.values()].filter(p => !p.deadAt).length;
    const total = processes.size;  // includes dying, excludes fully-gone
    $statAlive.textContent = alive;

    drawLinks();
    drawGraph();
    updateToolPressures(raw);
  } catch (err) {
    console.error("poll failed:", err);
    $dot.classList.remove("live");
    $dot.classList.add("error");
  }
}

// ── Tool pressure ──────────────────────────────────────
// Mirrors the controller's logic: count intent:<tool> deltas newer than
// the most recent tool-fired:<tool> marker. TTLs reap stale data, so a
// convo-wide pass is sufficient — old sessions' intents have expired.
const TOOL_THRESHOLDS = { search: 3, remember: 3, murmur: 2 };
function updateToolPressures(allDeltas) {
  const intents = { search: [], remember: [], murmur: [] };
  const lastFire = { search: 0, remember: 0, murmur: 0 };
  for (const d of allDeltas) {
    const tags = d.tags || [];
    const ts = new Date(d.timestamp).getTime();
    for (const tool of Object.keys(TOOL_THRESHOLDS)) {
      if (tags.includes(`intent:${tool}`)) intents[tool].push(ts);
      if (tags.includes(`tool-fired:${tool}`) && ts > lastFire[tool]) lastFire[tool] = ts;
    }
  }
  for (const [tool, threshold] of Object.entries(TOOL_THRESHOLDS)) {
    const count = intents[tool].filter(t => t > lastFire[tool]).length;
    const pct = Math.min(100, (count / threshold) * 100);
    const $bar = document.querySelector(`.tool-bar[data-tool="${tool}"]`);
    if (!$bar) continue;
    $bar.querySelector(".tb-fill").style.width = `${pct}%`;
    $bar.querySelector(".tb-count").textContent = `${count}/${threshold}`;
    $bar.classList.toggle("pressured", count >= threshold);
  }
}

// ── Graph ──────────────────────────────────────────────
//
// Monotone cubic interpolation (Fritsch-Carlson). Same algorithm as the
// dashboard's main-page graphs in fathomdx/ui/index.html. Prevents overshoot
// at extrema, which matters here because distance values are bounded [0,1].
function _smoothPath(points) {
  if (!points || !points.length) return "";
  const n = points.length;
  if (n === 1) return "M " + points[0][0] + " " + points[0][1];
  if (n === 2) {
    return "M " + points[0][0] + " " + points[0][1] +
           " L " + points[1][0] + " " + points[1][1];
  }
  const dx = new Array(n - 1);
  const m  = new Array(n - 1);
  for (let i = 0; i < n - 1; i++) {
    dx[i] = points[i + 1][0] - points[i][0];
    m[i]  = dx[i] === 0 ? 0 : (points[i + 1][1] - points[i][1]) / dx[i];
  }
  const t = new Array(n);
  t[0] = m[0];
  t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) {
    t[i] = (m[i - 1] * m[i] <= 0) ? 0 : (m[i - 1] + m[i]) / 2;
  }
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / m[i];
    const b = t[i + 1] / m[i];
    const s = a * a + b * b;
    if (s > 9) {
      const tau = 3 / Math.sqrt(s);
      t[i]     = tau * a * m[i];
      t[i + 1] = tau * b * m[i];
    }
  }
  let d = "M " + points[0][0] + " " + points[0][1];
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    const c1x = points[i][0] + h;
    const c1y = points[i][1] + t[i] * h;
    const c2x = points[i + 1][0] - h;
    const c2y = points[i + 1][1] - t[i + 1] * h;
    d += " C " + c1x + " " + c1y + " " + c2x + " " + c2y +
         " " + points[i + 1][0] + " " + points[i + 1][1];
  }
  return d;
}

const GRAPH_WINDOW_MS = 30_000;  // 30 sec of active ticks

// When pattern-boundary lands, pause graph rebuilds for ~2s so the CSS
// flash + fade-to-teal transitions can actually play on the same DOM
// nodes (otherwise drawGraph rebuilds the SVG and resets them mid-anim).
let _skipGraphRedraw = false;
const $panelGraph = document.getElementById("panel-graph");

function triggerSettleAnimation() {
  if ($panelGraph) {
    $panelGraph.classList.add("settled");
    $panelGraph.classList.add("settle-flash");
    setTimeout(() => $panelGraph.classList.remove("settle-flash"), 800);
  }
  // Convergence dots: snap to teal, keep pulsing — "writing the response".
  if ($convergenceBars) $convergenceBars.classList.add("settled");
  // Wait a beat after settle, then re-space the dots into a left-aligned
  // ellipsis. The pulse continues in teal while the witness writes.
  setTimeout(settleToEllipsis, 900);
  _skipGraphRedraw = true;
  setTimeout(() => { _skipGraphRedraw = false; }, 900);
}

// The thinking indicator IS the convergence dots — no separate typing
// element. showTypingIndicator/hideTypingIndicator now toggle the
// convergence-bars container; the dots pulse via CSS while not settled
// and slide horizontally as voice-similarity data lands.
function showTypingIndicator() {
  if (!$convergenceBars) return;
  $convergenceBars.hidden = false;
  // If we were in the post-settle ellipsis-teal state from a prior tick,
  // un-settle so dots go back to the RGB cross-voice pulse for the new
  // tick. Without this, ticks 2+ in a chain would render dots as
  // already-settled (teal, frozen at ellipsis positions) — no movement.
  $convergenceBars.classList.remove("settled");
  _convInEllipsisMode = false;
  for (const bar of $convergenceBars.querySelectorAll(".conv-bar")) {
    bar.style.transform = "";
  }
  // Add a brief CSS-transition window so the dots GLIDE from their
  // last position (e.g. settled-ellipsis) to the oscillator's first
  // frame, instead of snap-jumping. Removed after the transition
  // duration so the per-frame oscillator can take over without
  // transition lag.
  $convergenceBars.classList.add("transitioning");
  setTimeout(() => {
    if ($convergenceBars) $convergenceBars.classList.remove("transitioning");
  }, 750);
  // Kick the oscillator so dots animate immediately, even before any
  // metric delta has landed.
  _convStartOsc();
}
function hideTypingIndicator() {
  if ($convergenceBars) $convergenceBars.hidden = true;
}

// Convergence bars — three single dots (red/green/blue) at the bottom
// of the chat list. Each dot's x-position is the voice's most recent
// cross-voice similarity (1 - distance). When voices converge, the
// dots stack and additive-blend to white via mix-blend-mode:screen.
// On settle they snap to teal; after a beat they re-space themselves
// into a left-aligned ellipsis and pulse teal while the witness writes.
const $convergenceBars = document.getElementById("convergence-bars");
const CONV_BAR_WIDTH_PX = 80;
const CONV_BAR_DOT_PX = 7;
const CONV_BAR_RANGE = CONV_BAR_WIDTH_PX - CONV_BAR_DOT_PX;
// Evenly-spaced ellipsis positions (left-aligned, 7px dots + 4px gaps).
const CONV_ELLIPSIS_POSITIONS = { creator: 0, preserver: 11, destroyer: 22 };
let _convInEllipsisMode = false;
let _convOscRafId = null;

// While bars are visible and not settled, drive the dots with a slow
// sine oscillation per voice so movement is visible even when the
// underlying cross-voice metric is noisy or missing. Real metric data
// (when it lands via updateConvergenceBars) overrides the oscillation
// for that frame; the next animation tick picks up where we are.
function _convAnimate() {
  if (!$convergenceBars) return;
  if ($convergenceBars.hidden || _convInEllipsisMode) {
    _convOscRafId = null;
    return;
  }
  const t = Date.now() / 1000;
  const voices = ["creator", "preserver", "destroyer"];
  voices.forEach((voice, i) => {
    const series = metricSeriesByVoice[voice];
    let x;
    if (series && series.length) {
      // Real metric data — use latest similarity but add a small wobble
      // so the dot still reads as "alive" rather than frozen at one pos.
      const latest = series[series.length - 1];
      const sim = Math.max(0, Math.min(1, 1 - latest.d));
      x = sim * CONV_BAR_RANGE + 4 * Math.sin(t * 1.5 + i * 2);
    } else {
      // No metric yet — oscillate over the bar range so the dot moves
      // visibly. Each voice on its own phase (i * 2.1 rad) so they
      // don't unison-march.
      x = CONV_BAR_RANGE / 2 + (CONV_BAR_RANGE / 2 - 4) * Math.sin(t * 1.2 + i * 2.1);
    }
    const bar = $convergenceBars.querySelector(`.conv-bar.voice-${voice}`);
    if (bar) bar.style.transform = `translateX(${x.toFixed(1)}px)`;
  });
  _convOscRafId = requestAnimationFrame(_convAnimate);
}

function _convStartOsc() {
  if (_convOscRafId == null) _convOscRafId = requestAnimationFrame(_convAnimate);
}

function updateConvergenceBars() {
  if (!$convergenceBars) return;
  // Ellipsis mode locks dot positions while the witness writes — don't
  // let trailing metric updates yank them around.
  if (_convInEllipsisMode) return;
  // Make sure bars are visible and the oscillator is running. The
  // oscillator handles all positioning now (real metric data + sine
  // wobble), so we don't set transforms directly here anymore.
  $convergenceBars.hidden = false;
  _convStartOsc();
}

function settleToEllipsis() {
  // Wait-a-beat then re-space the (now-teal) dots into a left-aligned
  // ellipsis. The dots keep pulsing in teal until the witness card
  // arrives and hides the whole bar.
  if (!$convergenceBars) return;
  _convInEllipsisMode = true;
  for (const [voice, x] of Object.entries(CONV_ELLIPSIS_POSITIONS)) {
    const bar = $convergenceBars.querySelector(`.conv-bar.voice-${voice}`);
    if (bar) bar.style.transform = `translateX(${x}px)`;
  }
}

function resetConvergenceBars() {
  if (!$convergenceBars) return;
  _convInEllipsisMode = false;
  for (const bar of $convergenceBars.querySelectorAll(".conv-bar")) {
    bar.style.transform = "";
  }
  $convergenceBars.hidden = true;
}

function drawGraph() {
  if (_skipGraphRedraw) return;
  if (!$graph) return;  // line graph removed — function kept as a stub
  const bbox = $graph.getBoundingClientRect();
  const W = bbox.width;
  const H = bbox.height;
  if (W < 50 || H < 30) return;

  const PAD_L = 8, PAD_R = 8, PAD_T = 8, PAD_B = 8;
  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;

  // Anchor the right edge to the LATEST actual tick across any voice —
  // not wall-clock now. When ticks stop, the graph holds steady instead
  // of scrolling forward over empty time.
  let tMax = -Infinity;
  for (const pts of Object.values(metricSeriesByVoice)) {
    for (const p of pts) if (p.t > tMax) tMax = p.t;
  }
  for (const p of metricSeries) if (p.t > tMax) tMax = p.t;
  if (tMax === -Infinity) {
    $graph.innerHTML = '';
    return;
  }
  const tMin = tMax - GRAPH_WINDOW_MS;
  const span = Math.max(tMax - tMin, 1);

  const yOfSim = sim => PAD_T + innerH - (Math.max(0, Math.min(1, sim)) * innerH);
  const xOfT   = t   => PAD_L + ((t - tMin) / span) * innerW;

  const svgParts = [];

  // Phase shaded regions (only when in window).
  if (ruminationTs !== null && ruminationTs >= tMin) {
    const x = xOfT(ruminationTs);
    svgParts.push(`<rect class="rumination-shade" x="${x.toFixed(1)}" y="${PAD_T}" width="${(W - PAD_R - x).toFixed(1)}" height="${innerH}" />`);
    svgParts.push(`<line class="rumination-line" x1="${x.toFixed(1)}" y1="${PAD_T}" x2="${x.toFixed(1)}" y2="${PAD_T + innerH}" />`);
  }
  if (maxTs !== null && maxTs >= tMin) {
    const x = xOfT(maxTs);
    svgParts.push(`<line class="max-line" x1="${x.toFixed(1)}" y1="${PAD_T}" x2="${x.toFixed(1)}" y2="${PAD_T + innerH}" />`);
  }

  // Faint y gridlines at 0.25 / 0.5 / 0.75 — no axis chrome, the surface
  // itself is the grid.
  for (const v of [0.25, 0.5, 0.75]) {
    const y = yOfSim(v);
    svgParts.push(`<line class="grid" x1="${PAD_L}" y1="${y}" x2="${W - PAD_R}" y2="${y}" />`);
  }

  // Per-voice smoothed curves. Each voice's points are filtered to the
  // 30-sec window and rendered with monotone-cubic interpolation —
  // really curvy, but stable, no overshoot at extrema. Stroke + fill are
  // controlled by CSS classes so the panel's .settled state can fade
  // the voice colors into the witness teal via transition.
  const voicesWithData = Object.entries(metricSeriesByVoice).filter(([, pts]) => pts.length >= 1);
  for (const [voiceName, pts] of voicesWithData) {
    const inWindow = pts.filter(p => p.t >= tMin);
    if (inWindow.length === 0) continue;
    const screen = inWindow.map(p => [xOfT(p.t), yOfSim(1 - p.d)]);
    if (screen.length >= 2) {
      const d = _smoothPath(screen);
      svgParts.push(`<path class="voice-line voice-${voiceName}" d="${d}" />`);
    }
    for (const [x, y] of screen) {
      svgParts.push(`<circle class="voice-dot voice-${voiceName}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.5" />`);
    }
  }

  $graph.setAttribute("viewBox", `0 0 ${W} ${H}`);
  $graph.innerHTML = svgParts.join("");
}

// ── Controls ───────────────────────────────────────────
$btnPause.addEventListener("click", () => {
  paused = !paused;
  $btnPause.textContent = paused ? "resume" : "pause";
  $dot.classList.toggle("paused", paused);
  $dot.classList.toggle("live", !paused);
});
$sndOn.addEventListener("change", () => {
  if ($sndOn.checked) ensureAudio();
});

// Redraw links on scroll (cards move, SVG is fixed to the stage)
if ($procBody) $procBody.addEventListener("scroll", drawLinks, { passive: true });
$lakeBody.addEventListener("scroll", drawLinks, { passive: true });
window.addEventListener("resize", drawLinks);

// ── Boot ───────────────────────────────────────────────
poll();
setInterval(poll, POLL_MS);
setInterval(drawLinks, 500);   // smooth link updates while scrolling/animating
setInterval(pruneExpiredDeltas, 10_000);  // sweep expired deltas + drop empty accordions every 10s
