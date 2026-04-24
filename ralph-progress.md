# Ralph progress — fathomdx

`ralph-prd.md` is the contract. This file is the log + coverage matrix.
`ralph-findings.json` is the metrics tracker.

## Next

**Perspective:** Senior Dev Audit
**Repo:** fathomdx
**Why next:** With Dead Code & Cleanup done the remaining 51 ruff errors
are style/correctness (SIM105 × 11, E402 × 5, E701 × 5, RUF002 × 8,
B904 × 6, SIM102/103, RUF001/003/005, B905 × 2) — exactly the "what
would a senior reviewer flag in PR review" bucket. Also worth looking
at `api/server.py` which is 2 417 lines after this iteration's -75:
still well over the 800-line ceiling from PRD §Completion, so splitting
it is part of this perspective.

Hold RUF006 × 8 (asyncio-dangling-task) for Bug Hunt, not Senior Dev.

## Coverage matrix

Single repo (`fathomdx`) so the "matrix" is a column. `-` = not started,
`IP` = in progress, `DONE` = complete, `N/A` = out of scope for this repo.

| # | Perspective                      | fathomdx |
|---|----------------------------------|----------|
| 1 | Dead Code & Cleanup              | DONE     |
| 2 | Senior Dev Audit                 | -        |
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

_None yet. Use this section when a fix requires an architectural decision
or would take > ~15 min._

## Iteration log

Format:
```
### YYYY-MM-DD HH:MM TZ — [Perspective] / fathomdx
- What changed (files, lines ±, bugs fixed)
- Key findings or decisions
- Commits: <sha> <sha>
```

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
