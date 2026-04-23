# Ralph progress — fathomdx

`ralph-prd.md` is the contract. This file is the log + coverage matrix.
`ralph-findings.json` is the metrics tracker.

## Next

**Perspective:** Dead Code & Cleanup
**Repo:** fathomdx
**Why first:** ruff already reports 121 lint errors + 36 unformatted files
as the baseline — most are autofixable. Low-risk warmup, immediate
measurable delta, and it gets the codebase to a state where subsequent
perspectives aren't drowning in noise.

## Coverage matrix

Single repo (`fathomdx`) so the "matrix" is a column. `-` = not started,
`IP` = in progress, `DONE` = complete, `N/A` = out of scope for this repo.

| # | Perspective                      | fathomdx |
|---|----------------------------------|----------|
| 1 | Dead Code & Cleanup              | -        |
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
