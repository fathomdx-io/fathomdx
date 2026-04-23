# Ralph progress — fathomdx

`ralph-prd.md` is the contract. This file is the log + coverage matrix.
`ralph-findings.json` is the metrics tracker.

## Next

**Perspective:** Bug Hunt
**Repo:** fathomdx
**Why next:** Senior Dev Audit is DONE. The 8 remaining ruff errors
are all RUF006 (asyncio-dangling-task) — each one is a potential
silent failure where an `asyncio.create_task(...)` return value is
discarded and the task can be GC'd mid-flight. Those are bugs, not
style, so they belong in Bug Hunt. Read each site line-by-line before
the mechanical "store a reference" fix — some call sites may actually
want `asyncio.ensure_future` + a task-set, or fire-and-forget is
correct (and the right fix is a `# noqa: RUF006` with a why).

Also open: look for race conditions in `chat_listener` (the PRD called
this out at priority #3) and check the feed-loop's cooldown logic for
off-by-ones.

**Pending cleanup** from the server.py split (this iteration partial,
not Bug Hunt): `/v1/chat/completions` + `fathom_think` +
`_resolve_tools` (~250 lines) still live in server.py, along with the
`/v1/crystal/*` cluster and its generate/validate helpers. Pull those
into `api/routes/chat.py` and `api/routes/crystal.py` in a cleanup
pass before/after Bug Hunt — fathom_think is imported by
chat_listener and feed_loop, so moving it updates those lazy-imports.

## Coverage matrix

Single repo (`fathomdx`) so the "matrix" is a column. `-` = not started,
`IP` = in progress, `DONE` = complete, `N/A` = out of scope for this repo.

| # | Perspective                      | fathomdx |
|---|----------------------------------|----------|
| 1 | Dead Code & Cleanup              | DONE     |
| 2 | Senior Dev Audit                 | DONE     |
| 3 | Bug Hunt                         | -        |
| 4 | Quality Scaffold                 | -        |
| 5 | Test Creation                    | -        |
| 6 | Security Review                  | -        |
| 7 | Performance                      | -        |
| 8 | Dependency Audit                 | -        |
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
