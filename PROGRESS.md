# RALPH progress log

Each iteration: append one entry. Read the last ~20 entries before picking
the next perspective so you don't repeat yourself.

Format:
```
## <YYYY-MM-DD HH:MM> — <perspective> — <short title>
- What changed: …
- Files touched: …
- Commit: <sha> or "no-op (nothing found)"
- Follow-ups: …
```

---

## 2026-04-23 — scaffolding — initial setup (pre-ralph)

- `pyproject.toml` with ruff + pytest config (py312, pragmatic ruleset)
- `package.json` + `eslint.config.mjs` + `.prettierrc.json` at root for
  shared addon tooling
- `.pre-commit-config.yaml` with ruff + eslint + prettier + hygiene hooks
- `.github/workflows/ci.yml` — Python + Node jobs, site/ path-filtered out
- `api/tests/conftest.py` + `test_health.py` as the test harness seed
- `RALPH.md` (this file's sibling) defines scope, perspectives, commands

Baseline `ruff check .` count at handoff: **121 errors, 36 files need format**.
Baseline `pytest`: **1 test, passing**.

Follow-ups for RALPH:
- First perspective to pick: **dead code** (ruff autofix handles much of it)
- Second: **tests** (huge coverage gap, clear value)
- Rotate through the other eight in order.
