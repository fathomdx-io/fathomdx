/*
  Mind placer — a dev tool for dropping waypoint pins on the real mind.

  Renders two 2D projections of the strata dataset:
  - Top view (X/Z plane), for picking horizontal position
  - Front view (X/Y plane), for picking elevation — the pile's silhouette

  Click either view to drop a crosshair. Fill in a key, label, and caption,
  then "Add waypoint" to append to the list. "Copy JSON" spits out an array
  shaped for main.js's `waypoints` array so you can paste it in.
*/

// ── Config — keep SPREAD / CELL_SIZE / DISC_THICKNESS in sync with the mind.
const SPREAD = 25;
const CELL_SIZE = 0.6;
const DISC_THICKNESS = 0.06;
const STACK_DAMPEN = 0.55;   // matches the mind's jittered stacking

const MIND_MIN = -SPREAD;
const MIND_MAX = SPREAD;

// ── DOM ────────────────────────────────────────────────
const topCanvas = document.getElementById('top');
const frontCanvas = document.getElementById('front');
const draftX = document.getElementById('draft-x');
const draftY = document.getElementById('draft-y');
const draftZ = document.getElementById('draft-z');
const draftKey = document.getElementById('draft-key');
const draftLabel = document.getElementById('draft-label');
const draftText = document.getElementById('draft-text');
const addBtn = document.getElementById('draft-add');
const listEl = document.getElementById('waypoint-list');
const copyBtn = document.getElementById('copy');
const clearBtn = document.getElementById('clear');
const outEl = document.getElementById('out');

const topCtx = topCanvas.getContext('2d');
const frontCtx = frontCanvas.getContext('2d');

// ── State ──────────────────────────────────────────────
let moments = null;      // parsed from /strata.json
let wafers = null;       // per-moment layout with y elevations
let maxPileY = 1;
const draft = { x: null, y: null, z: null };
const waypoints = [];    // placed by user

// ── Palette (same golden-angle spacing as main.js) ─────
function buildPalette(sources) {
  const p = new Map();
  const g = 137.508;
  for (let i = 0; i < sources.length; i++) {
    p.set(sources[i], ((i * g) % 360) / 360);
  }
  return p;
}

// ── Data + layout ──────────────────────────────────────
async function loadStrata() {
  const resp = await fetch('/strata.json');
  const raw = await resp.json();
  const parsed = raw.map((d) => ({
    t: new Date(d.t).getTime(),
    s: d.s,
    m: d.m,
    wx: d.x * SPREAD,
    wz: d.z * SPREAD,
  }));
  parsed.sort((a, b) => a.t - b.t);

  const heightMap = new Map();
  const layout = [];
  for (let i = 0; i < parsed.length; i++) {
    const d = parsed[i];
    const cellIx = Math.round(d.wx / CELL_SIZE);
    const cellIz = Math.round(d.wz / CELL_SIZE);
    const key = `${cellIx},${cellIz}`;
    const stackIndex = heightMap.get(key) || 0;
    heightMap.set(key, stackIndex + 1);
    const y = stackIndex * DISC_THICKNESS * STACK_DAMPEN + DISC_THICKNESS / 2;
    if (y > maxPileY) maxPileY = y;
    layout.push({ x: d.wx, y, z: d.wz, s: d.s, m: d.m });
  }
  moments = parsed;
  wafers = layout;

  const srcCounts = new Map();
  for (const w of wafers) srcCounts.set(w.s, (srcCounts.get(w.s) || 0) + 1);
  const uniq = [...srcCounts.keys()].sort((a, b) => srcCounts.get(b) - srcCounts.get(a));
  return buildPalette(uniq);
}

// ── Rendering ──────────────────────────────────────────
function renderTop(palette) {
  const { width: w, height: h } = topCanvas;
  topCtx.fillStyle = '#050a0d';
  topCtx.fillRect(0, 0, w, h);

  // Grid.
  topCtx.strokeStyle = '#17303a';
  topCtx.lineWidth = 1;
  for (let g = -SPREAD; g <= SPREAD; g += 5) {
    const px = ((g - MIND_MIN) / (MIND_MAX - MIND_MIN)) * w;
    const py = ((g - MIND_MIN) / (MIND_MAX - MIND_MIN)) * h;
    topCtx.beginPath();
    topCtx.moveTo(px, 0); topCtx.lineTo(px, h);
    topCtx.moveTo(0, py); topCtx.lineTo(w, py);
    topCtx.stroke();
  }

  // Wafers as tiny color dots. Use additive compositing so dense cells
  // glow — mimics the "pile" silhouette.
  topCtx.globalAlpha = 0.6;
  for (const d of wafers) {
    const px = ((d.x - MIND_MIN) / (MIND_MAX - MIND_MIN)) * w;
    const py = ((d.z - MIND_MIN) / (MIND_MAX - MIND_MIN)) * h;
    const hue = palette.get(d.s) ?? 0;
    topCtx.fillStyle = `hsl(${hue * 360}, 75%, 62%)`;
    topCtx.fillRect(px - 0.5, py - 0.5, 1.5, 1.5);
  }
  topCtx.globalAlpha = 1;

  // Placed waypoints (amber pins).
  for (const wp of waypoints) drawPin(topCtx, wp.worldX, wp.worldZ, 'top', wp.key);
  if (draft.x != null) drawCrosshair(topCtx, draft.x, draft.z, 'top');
}

function renderFront(palette) {
  const { width: w, height: h } = frontCanvas;
  frontCtx.fillStyle = '#050a0d';
  frontCtx.fillRect(0, 0, w, h);

  const ySpan = Math.max(maxPileY * 1.1, 6);

  // Grid.
  frontCtx.strokeStyle = '#17303a';
  frontCtx.lineWidth = 1;
  for (let g = -SPREAD; g <= SPREAD; g += 5) {
    const px = ((g - MIND_MIN) / (MIND_MAX - MIND_MIN)) * w;
    frontCtx.beginPath();
    frontCtx.moveTo(px, 0); frontCtx.lineTo(px, h);
    frontCtx.stroke();
  }
  // Ground line.
  frontCtx.strokeStyle = '#244654';
  frontCtx.lineWidth = 1;
  frontCtx.beginPath();
  frontCtx.moveTo(0, h - 1); frontCtx.lineTo(w, h - 1);
  frontCtx.stroke();

  // Wafers. Draw back-to-front by Z so front ones occlude rear ones.
  const sorted = wafers.slice().sort((a, b) => b.z - a.z);
  frontCtx.globalAlpha = 0.35;
  for (const d of sorted) {
    const px = ((d.x - MIND_MIN) / (MIND_MAX - MIND_MIN)) * w;
    const py = h - (d.y / ySpan) * h;
    const hue = palette.get(d.s) ?? 0;
    frontCtx.fillStyle = `hsl(${hue * 360}, 75%, 62%)`;
    frontCtx.fillRect(px - 0.5, py - 0.5, 1.5, 1.5);
  }
  frontCtx.globalAlpha = 1;

  // Axis labels on the left showing real world Y values.
  frontCtx.fillStyle = '#5a6d74';
  frontCtx.font = '10px JetBrains Mono, monospace';
  for (let ty = 0; ty <= ySpan; ty += 5) {
    const py = h - (ty / ySpan) * h;
    frontCtx.fillText(`${ty.toFixed(0)}`, 4, py - 2);
  }

  for (const wp of waypoints) {
    drawPin(frontCtx, wp.worldX, wp.worldY, 'front', wp.key, ySpan);
  }
  if (draft.x != null) drawCrosshair(frontCtx, draft.x, draft.y, 'front', ySpan);
}

function drawCrosshair(ctx, wx, wOther, view, ySpan) {
  const w = ctx.canvas.width;
  const h = ctx.canvas.height;
  const px = ((wx - MIND_MIN) / (MIND_MAX - MIND_MIN)) * w;
  let py;
  if (view === 'top') {
    py = ((wOther - MIND_MIN) / (MIND_MAX - MIND_MIN)) * h;
  } else {
    py = h - (wOther / ySpan) * h;
  }
  ctx.strokeStyle = '#e8a24e';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(px, 0); ctx.lineTo(px, h);
  ctx.moveTo(0, py); ctx.lineTo(w, py);
  ctx.stroke();
  ctx.fillStyle = '#e8a24e';
  ctx.beginPath();
  ctx.arc(px, py, 4, 0, Math.PI * 2);
  ctx.fill();
}

function drawPin(ctx, wx, wOther, view, key, ySpan) {
  const w = ctx.canvas.width;
  const h = ctx.canvas.height;
  const px = ((wx - MIND_MIN) / (MIND_MAX - MIND_MIN)) * w;
  let py;
  if (view === 'top') {
    py = ((wOther - MIND_MIN) / (MIND_MAX - MIND_MIN)) * h;
  } else {
    py = h - (wOther / ySpan) * h;
  }
  ctx.fillStyle = '#5ce1c6';
  ctx.strokeStyle = '#5ce1c6';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(px, py, 6, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(px, py, 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#5ce1c6';
  ctx.font = '11px JetBrains Mono, monospace';
  ctx.fillText(key || '?', px + 10, py + 4);
}

// ── Click handlers ─────────────────────────────────────
function pickTopView(ev) {
  const rect = topCanvas.getBoundingClientRect();
  const fx = (ev.clientX - rect.left) / rect.width;
  const fy = (ev.clientY - rect.top) / rect.height;
  const wx = MIND_MIN + fx * (MIND_MAX - MIND_MIN);
  const wz = MIND_MIN + fy * (MIND_MAX - MIND_MIN);
  draft.x = wx;
  draft.z = wz;
  // Infer Y from pile height at that cell (nearest neighbor).
  if (draft.y == null) draft.y = pileHeightAt(wx, wz) + 2;
  updateDraftUI();
  renderAll();
}

function pickFrontView(ev) {
  const rect = frontCanvas.getBoundingClientRect();
  const fx = (ev.clientX - rect.left) / rect.width;
  const fy = (ev.clientY - rect.top) / rect.height;
  const ySpan = Math.max(maxPileY * 1.1, 6);
  const wx = MIND_MIN + fx * (MIND_MAX - MIND_MIN);
  const wy = (1 - fy) * ySpan;
  draft.x = wx;
  draft.y = wy;
  if (draft.z == null) draft.z = 0;
  updateDraftUI();
  renderAll();
}

function pileHeightAt(wx, wz) {
  // Find the tallest wafer within a 1.5-unit radius. Good enough for
  // "suggested elevation above the pile."
  const r2 = 1.5 * 1.5;
  let maxY = 0;
  for (const d of wafers) {
    const dx = d.x - wx, dz = d.z - wz;
    if (dx * dx + dz * dz <= r2 && d.y > maxY) maxY = d.y;
  }
  return maxY;
}

function updateDraftUI() {
  draftX.textContent = draft.x != null ? draft.x.toFixed(2) : '—';
  draftY.textContent = draft.y != null ? draft.y.toFixed(2) : '—';
  draftZ.textContent = draft.z != null ? draft.z.toFixed(2) : '—';
}

// ── Waypoints list ─────────────────────────────────────
function addWaypoint() {
  if (draft.x == null || draft.z == null) return;
  const wp = {
    key: draftKey.value.trim() || `wp-${waypoints.length + 1}`,
    label: draftLabel.value.trim() || draftKey.value.trim() || 'waypoint',
    text: draftText.value.trim() || '',
    worldX: +draft.x.toFixed(2),
    worldY: +(draft.y ?? 2).toFixed(2),
    worldZ: +draft.z.toFixed(2),
  };
  waypoints.push(wp);
  draft.x = draft.y = draft.z = null;
  draftKey.value = '';
  draftLabel.value = '';
  draftText.value = '';
  updateDraftUI();
  renderList();
  renderAll();
}

function renderList() {
  listEl.innerHTML = '';
  for (let i = 0; i < waypoints.length; i++) {
    const wp = waypoints[i];
    const li = document.createElement('li');
    li.innerHTML = `
      <div class="info">
        <div class="key">${wp.key}</div>
        <div class="caption">${wp.text || '<em>no caption</em>'}</div>
        <div class="coords">${wp.worldX}, ${wp.worldY}, ${wp.worldZ}</div>
      </div>
      <button class="del" data-i="${i}" title="Remove">×</button>
    `;
    listEl.appendChild(li);
  }
}

listEl.addEventListener('click', (ev) => {
  const i = ev.target?.dataset?.i;
  if (i != null) {
    waypoints.splice(+i, 1);
    renderList();
    renderAll();
  }
});

function exportJSON() {
  const arr = waypoints.map((wp) => ({
    key: wp.key,
    label: wp.label,
    text: wp.text,
    worldX: wp.worldX,
    worldY: wp.worldY,
    worldZ: wp.worldZ,
  }));
  const text = JSON.stringify(arr, null, 2);
  navigator.clipboard.writeText(text).catch(() => {});
  outEl.textContent = text;
  outEl.hidden = false;
}

// ── Wire up ────────────────────────────────────────────
let palette;
function renderAll() {
  if (!palette) return;
  renderTop(palette);
  renderFront(palette);
}

async function main() {
  palette = await loadStrata();
  renderAll();

  topCanvas.addEventListener('click', pickTopView);
  frontCanvas.addEventListener('click', pickFrontView);
  addBtn.addEventListener('click', addWaypoint);
  copyBtn.addEventListener('click', exportJSON);
  clearBtn.addEventListener('click', () => {
    waypoints.length = 0;
    renderList();
    renderAll();
    outEl.hidden = true;
  });
}

main();
