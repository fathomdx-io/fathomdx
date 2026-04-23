# Ralph progress — fathomdx

`ralph-prd.md` is the contract. This file is the log + coverage matrix.
`ralph-findings.json` is the metrics tracker.

## Next

**Perspective:** Cross-Repo Coherence
**Repo:** fathomdx
**Why next:** Dependency Audit is DONE. Two CVEs found and fixed
(Pillow CVE-2026-25990 + CVE-2026-40192 via floor bump to 12.2.0,
pytest CVE-2025-71176 via dev-extras floor to 9.0.3). All three
services now have proper `requirements.txt` with <major+1 caps;
delta-store and source-runner were fixed via moving deps out of
their Dockerfiles so pip-audit can see them.

Cross-Repo Coherence (#9) is next. Check that api ↔ addons contracts
match: does `addons/cli` call `/v1/search` with the shape `api`
actually expects? Do response envelopes line up? MCP-node against
/v1/tools? Compare implementations, not just docs.

**Pending cleanup** from the server.py split: `/v1/chat/completions`
+ `fathom_think` + `_resolve_tools` (~250 lines) still in server.py,
along with `/v1/crystal/*` cluster. Mechanical extraction, not
architectural. Fold in whenever.

## Coverage matrix

Single repo (`fathomdx`) so the "matrix" is a column. `-` = not started,
`IP` = in progress, `DONE` = complete, `N/A` = out of scope for this repo.

| # | Perspective                      | fathomdx |
|---|----------------------------------|----------|
| 1 | Dead Code & Cleanup              | DONE     |
| 2 | Senior Dev Audit                 | DONE     |
| 3 | Bug Hunt                         | DONE     |
| 4 | Quality Scaffold                 | DONE     |
| 5 | Test Creation                    | DONE     |
| 6 | Security Review                  | DONE     |
| 7 | Performance                      | DONE     |
| 8 | Dependency Audit                 | DONE     |
| 9 | Cross-Repo Coherence             | -        |
| 10| API Consistency                  | -        |
| 11| Docker & DevOps                  | -        |
| 12| Accessibility                    | N/A      |
| 13| Error Boundary Audit             | -        |
| 14| Utility Consolidation            | -        |
| 15| New Perspectives                 | -        |
| 16| Feed Experience                  | N/A      |
| 17| Chat & Conversation UX           | -        |
| 18| Onboarding Flow                  | N/A      |
| 19| Scout & Suggestions UX           | N/A      |
| 20| Backstage UX                     | N/A      |
| 21| Micro-interactions & Polish      | N/A      |
| 22| Mobile-First Audit               | N/A      |
| 23| Information Architecture         | N/A      |
| 24| Visual Language                  | N/A      |
| 25| Competitive UX Audit             | N/A      |

N/A cells are frontend / visual / UX concerns that live in `site/` or
`ui/`, which are out of scope for this run (see `ralph-prd.md`).

## Baseline snapshot (pre-loop, 2026-04-23)

- `ruff check .` → **121 errors**, 70 autofixable
- `ruff format --check .` → **36 files need reformat**
- `pytest` → **1 test**, passing (smoke test on `/health`)
- `npm run lint` → not yet run (addon node_modules not installed in CI env)
- `api/server.py` LOC → **1500+** (over the 800-line ceiling from PRD §Completion)

## Needs human

_Server.py split is underway (see iteration log below). Option A
picked, nine routers landed, server.py down to 688 LOC — under the
800 ceiling. Remaining chat + crystal extraction noted in "Next"
above; not architectural, just unfinished mechanical work._

### ~~api/server.py split (2 417 LOC, ceiling is 800)~~ — DONE 2026-04-23

After the Dead Code + Senior Dev passes, server.py still ships 2 417
lines and 80+ route handlers. It is well over the PRD-§Completion
ceiling and the biggest single file in the repo. Splitting it is
architectural — each option changes ownership of URL paths, and the
PRD explicitly forbids changing contracts under `/v1/*` without
approval, so this is not a safe Ralph-unilateral move.

Two plausible shapes for the split (pick one before next iteration):

1. **By resource** — one router file per cluster, `FastAPI.include_router`
   in server.py. Route counts: feed (12), sources (9), contacts (8),
   routines (6), sessions (5), tokens+pair+auth (7), moods+drift+
   pressure+crystal (9), media+deltas+recall+search (10). Lands
   close to seven ~300-line modules, keeps URL paths identical.

2. **By layer** — split per-concern: `api/models.py` for pydantic,
   `api/routes/` for handlers, `api/lifespan.py` for startup.
   Smaller per-file diffs but the feed cluster still ends up large.

Myra to choose. Once chosen, the split itself is ~45 minutes of
mechanical moves + one `pytest` + a `curl /v1/*` smoke.

## Iteration log

Format:
```
### YYYY-MM-DD HH:MM TZ — [Perspective] / fathomdx
- What changed (files, lines ±, bugs fixed)
- Key findings or decisions
- Commits: <sha> <sha>
```

---

### 2026-04-23 — Dependency Audit / fathomdx

Two commits, two CVEs found and fixed, three services now scannable
by pip-audit / dependabot for the first time.

**Fixes**

- `4b44055` — pinned upper bounds on every runtime dep, moved
  delta-store + source-runner deps out of their Dockerfiles into
  proper `requirements.txt` files, added pip-install layer
  separation so a code edit doesn't re-download torch.
  Pillow floor bumped to 12.2.0 for **CVE-2026-25990** + 
  **CVE-2026-40192** (both delta-store).
- `bcd6850` — pinned dev-extras upper bounds. pytest floor bumped
  to 9.0.3 for **CVE-2025-71176**.

**Audit summary**
- pip-audit on all three requirements.txt files: clean after the
  Pillow bump.
- npm audit on `addons/agent` and `addons/mcp-node` (the two with
  deps): zero vulnerabilities.
- No unused deps found — every package declared is actually
  imported.

**What changed structurally**
- `delta-store/Dockerfile` + `source-runner/Dockerfile` had their
  deps inlined into `RUN pip install ... fastapi uvicorn ...`.
  pip-audit and dependabot can't see those. Moved into
  `{delta-store,source-runner}/requirements.txt`; Dockerfile now
  `COPY requirements.txt` + `pip install -r`. Also added layer
  separation (requirements COPY before code COPY) so code-only
  rebuilds are cheap.

**Open for a future iteration**
- Consider adding `pip-audit` + `npm audit` as non-blocking steps
  in `.github/workflows/ci.yml` for early CVE detection. Not this
  iteration — extensibility, not an existing bug.

---

### 2026-04-23 — Performance / fathomdx

Three commits, 6 new tests, pytest 68 → 74. Every change removes
wall-clock latency from a hot path without touching semantics.

**Fixes (highest-impact first)**

1. `cb35bc8` — `fathom_think` system-prompt fan-out. Every chat turn
   sequentially awaited six lake reads (crystal, mood-maybe-synth,
   session row, agent status, contacts list, chat addressee). Serial:
   600-3000ms per turn. Now runs via `asyncio.gather` with
   `return_exceptions=True`; worst-case ≈ max individual latency
   (~500ms). Graceful degradation preserved — any single failed read
   falls back to the old silent-default value instead of 500-ing the
   whole turn.

2. `550affe` — `_has_fresh_card` N+1 killed. The feed loop iterated
   directive lines calling the freshness check per line, each one
   hitting the lake. For a 10-line crystal that's 10 round-trips
   before any real work. Replaced with a single prefetch
   (`_latest_card_by_line`) at the top of `_run_once` — one lake
   query, grouped in Python into a `{line_id: latest_ts}` map. New
   `_is_fresh_from_map` predicate takes the map. Fallback path
   (`_has_fresh_card`) kept for the cold-start single-fire caller.
   Saves roughly (N-1) × 100ms per visit.

3. `9ddc29c` — `_gather_pool` parallel reads. Card candidate pool
   fans in topic-tag, rss digest, browser-extension, and semantic
   search. Four sequential awaits became `asyncio.gather` with
   `return_exceptions=True`. Per-card save: ~1-2s → ~500ms.

**Audited + deferred**
- `chat_listener._tick` iterates every delta string-comparing
  timestamps. Fine at current scale; only interesting at 1000×
  volume.
- `_format_candidates` does per-delta regex + list building. Called
  on each line, <50 candidates. Negligible vs. the LLM call it feeds.
- `/v1/feed/engagement/history` scans deltas client-side. 500 rows
  max, run on a read endpoint. Not a hot path.

**Tests** — 6 new in `test_feed_loop_freshness.py` covering the
map-based is-fresh predicate: missing line, recent/stale, Z-suffix
timestamp, unparseable timestamp (safe-default: stale), exact-
boundary strictly-less-than.

---

### 2026-04-23 — Security Review / fathomdx

Five commits on `ralph`, eight new tests. One real high-severity fix,
two credential-hardening fixes, two defence-in-depth additions. CORS
wildcard audited and left as-is (bearer-token API, no credentials, no
cookies → safe).

**Findings + fixes**

| Severity | Finding | Fix (commit) |
|---|---|---|
| **HIGH** | `image_path` on POST /v1/deltas let any `lake:write` caller read arbitrary server-side files (CWE-22). `Path(image_path).read_bytes()` with no validation. | `e183bf9` — sandbox via new `settings.image_path_allowed_prefix`; feature disabled by default; `Path.resolve().relative_to(prefix)` blocks `..` traversal and symlink escapes. 6 tests. |
| MEDIUM | `tokens.json` + `pair-codes.json` written with default umask (0644) → world-readable on the host. Contents are password-equivalent material. | `d7961f3` — chmod 0600 after each write, wrapped in `contextlib.suppress(OSError)` so Windows doesn't trip. 2 POSIX-only tests. |
| LOW | `_dump_to` in delta-store used `create_subprocess_shell` with f-string-interpolated DSN. Operator-controlled (DATABASE_URL) so not an external attack surface, but shell-interpolating any string is a smell. | `5d79048` — switched to `create_subprocess_exec` with argv list, piping pg_dump → gzip via `asyncio.subprocess` pipe instead of shell. |
| DoS | No size caps on POST /v1/deltas — a `lake:write` caller could pile up 100k tags or stream 100MB base64. | `b1702a4` — `_MAX_TAGS_PER_DELTA = 64`, `_MAX_IMAGE_B64_CHARS = 35M` (~25MB decoded). Auth still gates first; these are defence-in-depth. |

**Audited + OK**
- CORS: `allow_origins=["*"]` without `allow_credentials=True`. Bearer-token API with no cookies — safe by design; browsers send the Authorization header explicitly, so cross-origin calls still need a stolen token.
- Auth middleware scope gate: `_required_scope` prefix-match is fail-closed (adds scope requirement on false-positive).
- SQL: all pg queries use positional parameters; f-string interpolation only on integer param indices. No injection.
- Token-in-log: one `log.info` near tokens, logs count + slug only, no raw tokens.
- Pair-code comparison is `==`, not `hmac.compare_digest`. Codes are 26 char base36 (~10^40 keyspace), TTL 10 min — timing-attack economics don't work.
- `?token=` query-param fallback on GET /v1/media/*: documented tradeoff in middleware comments; scoped to GET-only so a leaked URL grants read not write. Acceptable.

---

### 2026-04-23 — Test Creation / fathomdx

Three commits, 52 new tests. pytest 8 → 60 — hit 2× the PRD
§Completion target of 30 in a single iteration.

**Commits**
- `300f331` — `api/slug.py`: 9 tests covering adj-adj-animal grammar,
  slot uniqueness over 500 seeded draws, deterministic rng, and the
  `is_slug_taken` / `generate_unique_slug` helpers including the lake-
  unreachable fail-open and the collision-fallback suffix path.
- `96b47b4` — `api/reserved_tags.py`: 20 tests for the authority gate
  (strip_contact_tags, resolve, hint_for, evaluate across every gate
  branch + unknown-gate fail-closed + unauthenticated-writer reject).
  Monkeypatched synthetic gate rows for the two branches that don't
  have real tags in the current registry.
- `166962a` — `api/auth.py`: 23 tests for token CRUD, scope-matrix
  mapping, legacy-migration idempotency, contact-slug request helper,
  contact-cache invalidation. tmp_path fixture isolates the tokens
  file so tests never touch /data.

**PRD-flagged surfaces**
- [x] auth — 23 tests
- [x] tag parsing — 20 tests (reserved_tags)
- [x] slug — 9 tests
- [ ] mood-synthesis scoring — skipped, LLM-coupled

**Why skip mood scoring**: `mood.synthesize_mood` runs an LLM call to
produce a `carrier_wave` + `threads` JSON, then calls `pressure` /
`delta_client` internals. Unit-testing that requires mocking a stable
LLM response AND the pressure state — high effort for low signal.
A dedicated "regression corpus" pass with golden-file thread
signatures is a better shape for that test. Logged for Performance
or a future iteration.

---

### 2026-04-23 — Quality Scaffold / fathomdx

Two commits on `ralph`. Key win: `api/delta_client.py` now retries
idempotent reads with jittered exponential backoff, so a compose-
stack delta-store restart no longer cascades to dashboard errors.
pytest count 1 → 8. Ruff clean.

**Commits**
- `b848a5c` — `_request_with_retry` helper in `api/delta_client.py` +
  4 unit tests covering the contract (success-after-transient, exhaust,
  no-retry-on-4xx, timeout-retry). Applied to every idempotent read:
  search, query, plan, engagement_cloud, get_delta, tags, stats,
  retrievals_history, usage_history, pressure_history, pressure_volume,
  recent_deltas_timestamps, feed_stories, drift, get_contact_row,
  list_contact_rows, list_handles, resolve_handle, centroid. Writes
  (POST /deltas, upload_media, handle CRUD, backfill) do NOT retry —
  delta-store has no idempotency keys, so a retried POST can create
  duplicate deltas. 3 attempts, 0.2s base doubling with 0.5-1.5×
  jitter. Retries on httpx Transport/Timeout errors + 502/503/504.

- `1b561f5` — bound `ChatListener._session_locks` with LRU eviction at
  256 entries + 3 unit tests. Factored `_lock_for_session()` out so
  the bookkeeping is testable without touching the network. Evicts
  only inactive entries so no concurrent holder can race on a dropped
  Lock.

**Dep note**: `pytest-httpx` is in the `dev` extras already. Had to
`pip install pytest-httpx` in the local venv — the CI workflow reads
pyproject `dev` extras, so CI picks it up for free.

**Still open for Performance/Scaffold**: some `except Exception` sites
in search.py / contacts.py / usage.py could be tightened to specific
exception types (json.JSONDecodeError, httpx.HTTPStatusError). Didn't
touch this pass — low-priority, would add noise without clear wins.

---

### 2026-04-23 — Bug Hunt / fathomdx

Seven commits on `ralph`. All 8 RUF006 asyncio-dangling-task sites
fixed + a new shared `api/_bgtasks.py` helper. Ruff now clean (0
errors). Tests green throughout.

**The RUF006 class of bug in one sentence**: under Python 3.12+ the
event loop only holds a weak reference to tasks created via
`asyncio.create_task`, so a fire-and-forget caller that discards the
return value can see its coroutine silently GC'd mid-flight, AND any
exception the task raised goes unlogged because nothing ever awaits
it. Two failure modes in one idiom.

**New helper**: `api/_bgtasks.py:spawn(coro, *, name=...)` — adds the
task to a module-level set, drops it in a done_callback, AND logs any
exception on completion. Commit `7745ef3` introduced it. Accidentally
bundled with some unstaged hook.sh edits Myra had in her working
tree; Myra explicitly OK'd the bundle for this cleanup-pass iteration.

**Commits (one fix per site/cluster)**
- `7745ef3` — add `api/_bgtasks.py` helper
- `1a64d65` — `api/auto_regen.py`: bootstrap + drift regen fires
- `186104b` — `api/chat_listener.py`: on_tool_event write_chat_event
- `1ad3111` — `api/feed_loop.py`: force_fire (/v1/feed/refresh)
- `ed005b1` — `api/server.py`: lifespan contact-backfill
- `5c53539` — `delta-store/deltas/retrievals.py`: fire_and_forget record
- `84df2d1` — `source-runner/` (server + source_runner): startup run + manual_poll

**Chat-listener race-condition audit findings** (PRD flagged #3)
- `_last_seen` / `max_ts` logic: initially suspected a bug where a
  future-timestamped delta could advance `last_seen` past real-now
  messages. On closer reading, delta-store is the single clock source
  for all deltas, so there's no skew to exploit. Not a bug.
- `_session_locks`: a dict that accumulates one `asyncio.Lock` per
  session slug, never pruned. Over a long-running process with many
  chat sessions this leaks memory (tiny — ~100B per session). Real
  but low-priority; moved to the Next section above as a scaffold/
  perf item rather than a bug.
- The listener filter correctly skips Fathom's own writes via the
  `participant:fathom` tag (not the source, which is `fathom-chat`
  for both user and Fathom deltas). Ran the logic against each of
  `db.add_message`'s code paths — no double-response loop.

**Feed-loop off-by-ones / cooldown audit**
- `mark_visit` + `force_fire` + `_run_once` triple-checks the
  per-contact lock. Not a TOCTOU: none of `_lock_for(...)`,
  `lock.locked()`, or the `_pending_visits.get` path yields, so no
  other coroutine can run in between. Safe under asyncio's
  single-threaded execution.

**Ruff status**: 0 errors. Format: 46 files still unformatted (out
of scope for Bug Hunt; track for future mechanical pass).

---

### 2026-04-23 — Senior Dev Audit (server.py split — Option A) / fathomdx

Ten commits on `ralph`. api/server.py 2 417 → 688 LOC (-1 729) —
under the 800-line ceiling for the first time since the baseline.
Tests stayed green (1/1) throughout. Every URL path unchanged.

Myra picked Option A (split by resource) after the prior iteration's
write-up. Nine new router files under `api/routes/`:

| file | routes | purpose |
|---|---|---|
| `agents.py` | 2 | /v1/agents/* — presence + npm release cache |
| `auth.py` | 9 | /v1/auth/*, /v1/tokens, /v1/scopes, /v1/pair* |
| `contacts.py` | 13 | /v1/contacts/*, /v1/contact-proposals/*, /v1/me/profile |
| `feed.py` | 12 | /v1/feed/* + FeedEngagementRequest |
| `lake.py` | 8 | /v1/search, /v1/deltas*, /v1/engagement, /v1/tools + LAKE_TOOLS |
| `media.py` | 3 | /v1/media/* (proxy, upload, capture-context) |
| `routines.py` | 6 | /v1/routines/* |
| `sessions.py` | 5 | /v1/sessions/* |
| `sources.py` | 9 | /v1/sources/* (source-runner proxy) |
| `vitals.py` | 10 | /v1/moods/*, /v1/pressure/*, /v1/drift/*, /v1/usage/*, /v1/recall/*, /v1/crystal/events |

**Commits (one router per commit, each with the tests passing)**
- `2a9733b` — agents (2 routes)
- `ebc1043` — sources (9 routes)
- `37d12b2` — routines (6 routes)
- `6774f45` — move `current_contact_slug` helper into api/auth.py
- `6457b61` — sessions (5 routes)
- `0ee88c9` — media (3 routes)
- `08148c4` — auth/tokens/pair (9 routes)
- `535bf33` — contacts + proposals + /me (13 routes)
- `352a59c` — restore exec bit on server.py after awk excise
- `d043046` — vitals (10 routes)
- `68b3727` — lake (8 routes)
- `1b490a3` — feed (12 routes)

**Cross-cutting moves**
- `auth.current_contact_slug(request)` promoted from private helper in
  server.py to a public name on `api/auth.py`. 17 call sites.
- Routers live under `api/routes/`, imported at the bottom of
  server.py next to each other so new router files are grep-able in
  one place (the E402 noqa on each import is intentional — must come
  after `app = FastAPI(...)`).
- Each router commit drops its now-unused imports from server.py
  (F401 autofix caught `httpx`, `Depends`, `File/Form/UploadFile`,
  `pairing`, `pressure`, `recall`, `usage_module`, `timedelta`,
  `datetime`, etc.).

**Still in server.py** (left for a cleanup pass, not this iteration):
- `/v1/chat/completions` route + the Message/ChatRequest models
- `fathom_think` (public API, imported by chat_listener + feed_loop)
- `_resolve_tools` (~100-line tool-calling loop)
- `/v1/crystal` + `/v1/crystal/refresh` + 3 generate/validate helpers
- `_split_facets` (used only by the crystal refresh)
- `/v1/models`, `/health`, static UI mount

### 2026-04-23 — Senior Dev Audit / fathomdx

Nine commits on `ralph`. Ruff violations 51 → 8 (all remaining are
RUF006, deferred to Bug Hunt). Tests stayed green (1/1).

**Style / correctness commits (one ruff rule per commit)**
- `e935228` — SIM105: 11 `try/except/pass` → `contextlib.suppress`
  (2 asyncio-wait-for, 9 temp-file-cleanup idioms)
- `201c2c4` — B904: 6 `raise HTTPException(…)` in except blocks now
  chain with `from e` (4 source-runner endpoints, api/server.py media)
- `7abd167` — B905: 2 cosine-distance helpers use `zip(strict=True)`
- `8a2a3f7` — E402: move `log = logging.getLogger(__name__)` below
  the import block in server.py
- `f131fb7` — E701: break 5 single-line `if cond: reasons.append(…)`
  in feed_loop
- `c6cc249` — SIM102 + SIM103 + RUF005: three small idiom flattens
- `dd072e0` — ruff config: ignore RUF001/002/003 (intentional math
  notation ×, −, Σ in pressure/crystal docstrings)

**DRY consolidation**
- `a5a157b` — hoist 7 byte-identical `_now()` + 2 `_now_iso()` into
  `api/_time.py`. Seven modules now import the private names via
  alias (`from ._time import now as _now`), so call sites didn't
  change. Dropped a stray local `from datetime import datetime,
  timedelta` in feed_crystal that shadowed module imports.

**Totals**: 13 files touched, +36 / -41 in the consolidation alone;
43 line-of-code net reduction across all 9 commits. `api/_time.py` is
new (21 lines).

**Key findings**
- `cosine_distance` in `crystal_anchor.py` and `_cosine_distance` in
  `feed_crystal.py` are near-duplicate implementations. Added as a
  future consolidation target.
- `api/server.py` at 2 417 LOC is still over the 800 ceiling. Written
  up under **Needs human** above — needs a split-topology decision
  (by resource vs. by layer) before the mechanical work.
- Ruff now reports only 8 errors, all RUF006 asyncio-dangling-task.
  Bug Hunt territory.

---

### 2026-04-23 — Dead Code & Cleanup / fathomdx

Thirteen commits on `ralph`, each scoped to one rule or one dead function.
Ruff violations 121 → 51 (-70, all the autofixable rules for this
perspective). Tests stayed green (1/1) throughout.

**Autofix commits (mechanical, one rule per commit)**
- `cd66f9a` — F401: 8 genuinely unused imports
- `30c957a` — UP017: `timezone.utc` → `datetime.UTC` across 17 files
- `359f8b6` — F401 follow-up: drop 18 now-unused `timezone` imports
- `cf8ca01` — I001: sort imports across 21 files
- `8aa31fb` — UP041: `asyncio.TimeoutError` → `TimeoutError` (6 sites)
- `49ec30c` — RUF100: drop 7 unused `noqa:B008` markers
- `a273fab` — F541: drop f-prefix from 3 non-interpolating strings
- `c372e86` — UP035 + RUF022: AsyncGenerator import, sort `__all__`

**Dead-function commits (one function per commit)**
- `9b2c113` — `_md5` in `delta-store/deltas/store.py` + `hashlib` import
- `7d9ac49` — `_format_scored_row` in `delta-store/deltas/cli.py`
- `954992d` — `_now` in `api/crystal.py` + datetime/UTC imports
- `4cb7d9a` — `_stream_response` in `api/server.py` (-57 lines) +
  `AsyncGenerator` import
- `08835b8` — `_msg_dicts` in `api/server.py`

**Totals**: 25 files touched, +102 / -182 (net -80 lines). Five dead
helpers deleted. Server.py: 2 492 → 2 417 lines.

**Key findings**
- No orphaned source files — `source-runner/sources/template.py` looked
  suspicious but it's an intentional scaffold for creating new sources.
- No commented-out code blocks and no TODOs anywhere. The baseline is
  already clean for that class of rot.
- The `_now()` helper is copy-pasted across 7+ modules with identical
  implementations — flag this for Utility Consolidation (#14) later.
- Ruff reports 34 files that still need reformatting. Not a Dead Code
  concern; defer to the Senior Dev pass when we're already touching
  those files for structural work.

### 2026-04-23 — scaffolding (pre-Ralph)

Initial tooling + file setup. Not a Ralph iteration.

- Added `pyproject.toml` (ruff + pytest, py312, pragmatic ruleset)
- Added `package.json` + `eslint.config.mjs` + `.prettierrc.json` +
  `.prettierignore` at repo root for shared addon tooling
- Added `.pre-commit-config.yaml` (ruff + eslint + prettier + hygiene)
- Added `.github/workflows/ci.yml` (Python + Node jobs, site/ filtered out)
- Added `api/tests/conftest.py` + `test_health.py` (1 passing smoke test)
- Extended `.gitignore` (.pytest_cache, .ruff_cache, node_modules)
- Wrote `ralph-prd.md` + `ralph-progress.md` (originally `RALPH.md` /
  `PROGRESS.md`; renamed to match ralph-loop skill contract)

Commits: `adff068`
