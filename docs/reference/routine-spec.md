# Routine Spec

A routine is a prompt + a schedule. The cron tick fires it INTO the River (the witness), which decides what to do — dispatch claude-code, write a feed card, fire an alert, propose a state change, or stay silent. The routine spec doesn't pre-pick a route.

## Anatomy

A routine is a **spec delta** with three things:

1. **Tags** — `spec`, `routine`, and `routine-id:<stable-id>`, plus an optional `workspace:<name>`.
2. **Content** — YAML frontmatter + a prompt body (the text the witness reads as the user-given instruction).
3. **Source** — `consumer-dashboard` (when created via the UI), `claude-code:<workspace>` (when hand-written), or `routine-scheduler` for internal writes.

Example:

```
Tags:   spec, routine, routine-id:gold-check, workspace:trader-agent
Source: consumer-dashboard
Content:
---
id: gold-check
name: Gold Price Pulse
schedule: "0 * * * *"
enabled: true
workspace: trader-agent
host: myras-fedora-laptop
permission_mode: auto
single_fire: false
deleted: false
---

Check the current gold spot price. Compare to the last 24h. If it moved
more than 1%, surface a feed card. Otherwise stay silent.
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
