# Routine Spec

A routine is a prompt + a schedule. The cron tick fires it INTO the River (the witness), which decides what to do — dispatch claude-code, write a feed card, fire an alert, propose a state change, or stay silent. The routine spec doesn't pre-pick a route.

## Anatomy

A routine is a **spec delta** with three things:

1. **Tags** — `spec`, `routine`, and `routine-id:<stable-id>`, plus an optional `workspace:<name>`.
2. **Content** — YAML frontmatter + a prompt body (the text the witness reads as the user-given instruction).
3. **Source** — `consumer-dashboard` (when created via the UI), `claude-code:<workspace>` (when hand-written), or `routine-scheduler` for internal writes.

Example:

```
Tags:   spec, routine, routine-id:gold-mac-ratio, workspace:trader-agent
Source: consumer-dashboard
Content:
---
id: gold-mac-ratio
name: Gold-to-Mac Ratio
schedule: "0 * * * *"
enabled: true
workspace: trader-agent
host: myras-fedora-laptop
permission_mode: auto
single_fire: false
deleted: false
---

# Purpose
Track when gold's purchasing power crosses one Mac.

# Needs
claude-code on myras-fedora-laptop — live price fetch.

# Steps
1. Fetch gold spot price (Kitco).
2. Fetch refurbished 128GB iPhone 15 Pro Max price (Apple refurb).
3. Compute ratio. Compare to last fire.

# Ending
Stay silent on quiet days. If the ratio drops to 1.0 or lower (gold has
caught up to a Mac), send me a hard alert.
```

The four-section body (Purpose / Needs / Steps / Ending) is convention, not enforced by code — see [Writing the prompt](#writing-the-prompt) below.

## Writing the prompt

The witness reads the prompt body as the user-given instruction. Routines that follow a four-section convention give the witness clearer signal than freeform prose — same reason a good email has a subject line, body, and call-to-action.

### The four sections

```
# Purpose
[One sentence. What I'm trying to accomplish.]

# Needs
[What this needs to actually run — claude-code on a host, a specific tool,
or "substrate only" if the lake already has the data. Fathom uses this as
a strong hint when picking a route.]

# Steps
[The instructions — what to look for, what to filter, what to compare against.
Numbered or prose, whichever fits.]

# Ending
[How you want to know it ran. Plain language. The witness reads this to pick
the route — feed card, chat reply, alert, silent, or something else.
Examples below.]
```

The witness still reads the *whole* body, so prose outside these sections is fine. But the headers are load-bearing: Fathom looks at `# Ending` to decide what to deliver back to you.

### `# Ending` — how you want to be notified

This is where you express the route preference in language. The witness translates to its actual route. Common patterns:

| What you write under `# Ending` | Witness picks |
|---|---|
| "Send me a card with the result." | `feed-card` |
| "DM me a quick line." | `chat-reply` to your active surface |
| "Card most days; soft alert if anything major lands." | `feed-card` by default, `alert:soft` when the prompt's condition is met |
| "Stay silent unless X — then alert me hard." | `silent` by default, `alert:hard` when X |
| "Do nothing on screen, just write the result back to the lake." | `silent` (the work still produces deltas) |
| "Email Jerry the summary." | `tool:<email-tool>` (if such a tool exists) |
| "Propose a routine cleanup if you find any candidates." | `tool:routines` proposal card with Edit/Deny/Approve |

You don't have to use any of these phrasings exactly. Write it however you'd describe it to a person; Fathom reads the intent.

### Why not encode `output:` and `escalate_if:` as frontmatter?

We considered it. The reason we landed on prose under `# Ending` instead:

- **The witness is an LLM.** Asking it to read "Card most days, alert if X" as a directive is exactly what it's good at. Pushing that into structured fields forces precision the user doesn't have to want.
- **The Ending section is where edge-case conditions live.** "Card unless gold-to-mac ratio drops below 1.0" is a real preference; encoding the predicate as YAML would be lossy.
- **Routine writers are the user, not other code.** The schema should match how the user thinks, not how the witness parses.

Keep frontmatter for **routine identity and scheduling** (id, name, schedule, host, permission). Keep prose for **everything about what to do and how to deliver it**.

### Examples

**Gold-to-Mac ratio — silent unless threshold crosses**

```
# Purpose
Track when gold's purchasing power crosses one Mac.

# Needs
claude-code on myras-fedora-laptop — live price fetch.

# Steps
1. Fetch gold spot price (Kitco).
2. Fetch refurbished 128GB iPhone 15 Pro Max price (Apple refurb).
3. Compute ratio. Compare to last fire's ratio in the lake.

# Ending
Stay silent on quiet days. If the ratio drops to 1.0 or lower (gold has
caught up to a Mac), send me a hard alert. Lead with the ratio + delta.
```

**Menya Rui — soft alert when open + closing soon**

```
# Purpose
Catch the window when Menya Rui is open AND closing soon.

# Needs
claude-code on myras-fedora-laptop — Google Maps lookup.

# Steps
1. Check Menya Rui's current open status.
2. Read its closing time today.
3. Compute time-to-close.

# Ending
Stay silent unless they're open and closing in 90 minutes or less. Then
soft-alert me with the closing time.
```

**Hard-problem heartbeat — daily card, two paragraphs**

```
# Purpose
Daily heartbeat on the hard-problem workspace.

# Needs
claude-code on myras-fedora-laptop — read fresh vault state.

# Steps
1. Read today's hard-problem vault entries.
2. Identify what was accomplished today vs. yesterday.
3. Decide the next concrete step.

# Ending
Send me a card with two paragraphs: what was accomplished (concrete, no
hand-waving), and the plan for next round (one specific action).
```

**Daily news briefing — card most days, alert on big news**

```
# Purpose
Morning news briefing — Trump health, AI/robotics, STL events.

# Needs
claude-code on myras-fedora-laptop — web fetch.

# Steps
1. Check world, national, and St. Louis news.
2. Filter for: Trump health changes, AI/robotics breakthroughs, STL events.
3. Surface only what's new since last fire.

# Ending
Card most days. Soft alert if anything genuinely major breaks (Trump
health change, AI breakthrough, STL emergency). Stay silent if literally
nothing new.
```

**Weekly retrospective — substrate only**

```
# Purpose
Weekly look-back at what landed in the lake.

# Needs
Substrate only — no claude-code needed.

# Steps
1. Pull what landed in the lake this week (commits, vault entries, chats).
2. Group by theme.
3. Surface one thing worth remembering next month.

# Ending
Send me a card. Three sections, one paragraph each.
```

## Frontmatter fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | *required* | Stable identifier. Cannot be changed (the `routine-id:` tag carries it). |
| `name` | string | *required* | Human-readable label. Shown in the dashboard. |
| `schedule` | cron | — | 5-field cron string. Evaluated in the api container's local TZ. |
| `interval_minutes` | int | — | Legacy. Parsed for back-compat but ignored by the scheduler. Use `schedule`. |
| `enabled` | bool | `true` | When false, scheduler skips. Dashboard greys it out. |
| `workspace` | string | `""` | Path under `~/Dropbox/Work/`. Only used when the witness routes to claude-code; the kitty plugin `cd`s there before launching claude. |
| `host` | string | `""` | If set, only the agent whose `host` matches will spawn claude-code (when the witness picks that route). Empty = fleet-wide. Informational for non-claude-code routes. |
| `permission_mode` | `auto` \| `normal` | `auto` | Only meaningful for claude-code-routed fires. See "Per-host kill switch" in [set-up-a-routine.md](../how-to/set-up-a-routine.md). |
| `single_fire` | bool | `false` | When true, the scheduler soft-deletes the spec after firing once (writes a tombstone with `deleted: true`). |
| `deleted` | bool | `false` | Tombstone — scheduler and dashboard skip. History stays in the lake. |

## Lifecycle

There are two firing paths and they have different shapes.

### Path A — Cron tick (River-mediated)

The default for any scheduled routine. Cron tick → River → witness routes.

```
spec delta             routine-due intent       witness output           (downstream)
(edited by you)        (puddle, kind:           (one or more cards;     (e.g. claude-code
                        routine-due, body =      varies by route)         closure → next
                        prompt)                                           witness tick)
─────────────          ──────────────────       ────────────────         ────────────────
[spec, routine,        intent + tags carry      route can be             whatever the route's
 routine-id:X]   ──▶   routine-id, host pin,    feed-card, chat-reply,   downstream is —
                       permission_mode          claude-code:<host>,      claude-code spawns
                                                alert:<level>, tool:..., a kitty window;
                                                or no card (silent)      feed-card lands
(cron tick)            (witness deliberates)    (witness emits)          (consumer reads)

In parallel, the scheduler also writes a `routine-tick` marker delta
into the lake — durable receipt for hydration on restart. Kitty
doesn't consume these. They exist only so a process restart can
reconstruct what fired before the crash.
```

The witness's pick depends on the prompt. "Check the news, synthesize an update" → claude-code dispatch + a follow-up synthesis tick. "Summarize this week from the lake" → one feed-card. "Stay silent unless X moved" → no card emitted on quiet days. See [set-up-a-routine.md](../how-to/set-up-a-routine.md#what-a-routine-can-touch) for the full route table.

### Path B — Manual fire / Fire Now (direct)

The legacy, claude-code-only path. Used by:
- The "Fire Now" button on the Routines page.
- The chat-tool `routines` action `fire`.
- The witness's own `routine-fire:<id>` route (Phase 2 of the revival branch — the witness can directly fire a routine when it judges the routine itself is the right response).

```
spec delta             routine-fire delta       kitty window           routine-summary delta
(edited by you)        (lake, dispatched        (spawned by            (written by claude
                        directly to kitty)       kitty plugin)          inside the routine)
─────────────          ──────────────────       ──────────────         ─────────────────────
[spec, routine,        [routine-fire,           (claude runs           [routine-summary,
 routine-id:X]   ──▶   routine-id:X,    ──▶     the prompt)    ──▶     routine-id:X,
                       fired-at:<iso>,                                  fire-delta:<fire-id>]
                       host:<x>]
```

This skips River deliberation. Use it when you want claude-code to run the prompt verbatim, no witness in the loop.

The `fire-delta:<fire-id>` tag on the summary lets the dashboard pair a run with its result.

## Tag conventions

| Kind | Required tags | Optional tags | Source |
|---|---|---|---|
| **spec** | `spec`, `routine`, `routine-id:<id>` | `workspace:<name>` | `consumer-dashboard`, `claude-code:<ws>`, or manual |
| **routine-due intent** (Path A) | `intent`, `kind:routine-due`, `routine-id:<id>` | `host:<x>`, `permission-mode:<mode>` | `routine-scheduler` |
| **routine-tick** (Path A) | `routine-tick`, `routine-id:<id>` | `host:<x>` | `routine-scheduler` |
| **fire** (Path B) | `routine-fire`, `routine-id:<id>` | `workspace:<name>`, `permission-mode:<mode>`, `host:<x>`, `fired-at:<iso>` | `consumer-dashboard`, `routine-scheduler` (legacy), or manual |
| **summary** (Path B only) | `routine-summary`, `routine-id:<id>` | `fire-delta:<fire-id>` | `claude-code:routine` (written by the running routine) |

Path A's downstream artifacts (witness cards, claude-code closures) carry their own tag families and aren't routine-specific — they look the same as anything else the witness emits, just stamped with `addresses:<intent-id>` pointing back at the `routine-due` intent.

## CRUD

**Create** or **update**: write a new spec delta with the same `routine-id:<id>` tag. The scheduler and dashboard always take the latest-by-timestamp per id.

**Delete**: write a new spec delta with `deleted: true`. Don't literally remove deltas from the lake — history stays.

**Pause**: write a new spec delta with `enabled: false`. Resume = another spec delta with `enabled: true`.

The dashboard's Routines page does all of this through `/v1/routines`, `/v1/routines/<id>` (PUT, DELETE), `/v1/routines/<id>/fire` (POST). The chat-tool `routines` action covers the same surface from inference turns.

## Who reads what

- **`api/routine_scheduler.py`** — reads spec deltas every 60s, writes `routine-due` intents into the puddle on cron-elapsed AND a `routine-tick` marker into the lake. Honors `single_fire` by soft-deleting the spec after firing once.
- **`api/routines.py`** — CRUD over spec deltas. `fire()` is the legacy direct-to-kitty path (Path B); used by Fire Now and the chat tool.
- **`api/loop/witness.py`** — reads `routine-due` intents alongside other intents, deliberates, picks a route. Output cards stamp `addresses:<intent-id>` to close the intent. Also has its own `routine-fire:<id>` route (Phase 2) for proactively firing routines based on substrate.
- **`addons/agent/plugins/kitty.js`** — polls for `routine-fire` deltas (Path B) AND `route:claude-code` deltas (Path A → witness dispatched claude-code). Spawns kitty + claude in either case.
- **`api/routes/routines.py`** — HTTP CRUD for the dashboard.
- **Dashboard `RoutinesPage`** — renders, and POSTs back to `/v1/routines` for CRUD.

## Gotchas

- **`routine-id` is immutable.** It's also the stable key across every delta in the lifecycle. Changing it means creating a different routine.
- **Soft-delete != gone from search.** Tombstones still match `fathom delta search`. Filter with `--not-tags deleted` if you want to hide them. (Or filter client-side on `meta.deleted`.)
- **Schedule TZ.** Cron is evaluated in the api container's local TZ (see `TZ` in `.env`). If you're away from home, your routines still fire on container time.
- **`interval_minutes` is dead.** Parser accepts it but the scheduler ignores it. Use `schedule`.
- **Path A doesn't write `routine-fire` deltas.** If you're scripting against the lake and waiting for those to detect activity, switch to `routine-tick` or to the witness's `addresses:<intent-id>` outputs.
- **The witness might emit nothing.** A prompt like "stay silent unless X" can produce zero cards on quiet days. That's not a bug. The `routine-due` intent times out after the kind's TTL (48h by default for routine-due) and falls off the queue.
