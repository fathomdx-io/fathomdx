# RALPH — fathomdx cleanup loop

Read this file every iteration. It is the contract for what RALPH is allowed to
touch, which perspectives to rotate through, and how to commit. `PROGRESS.md`
(sibling file) tracks what's been done so iterations don't cover the same
ground.

## Scope

**In scope (this repo only):**
- `api/` — FastAPI consumer API (~11k LOC Python)
- `delta-store/deltas/` — HTTP API over the lake (Python)
- `source-runner/` — external source pollers (Python)
- `addons/` — Node CLI, agent, connect, mcp-node, browser-extension

**Out of scope — do NOT touch:**
- `site/` — being ralphed separately, don't double-edit
- `ui/`, `assets/` — vendored/generated
- `docs/` — prose, not code
- Sibling repos (`../core`, `../web/hifathom`, `../web/design`) — separate
  projects with their own CLAUDE.md; do not cross the boundary

## Perspectives (rotate one per iteration)

1. **Dead code** — unused imports/functions/modules, commented-out blocks,
   TODOs older than the git log can justify, orphan files not referenced by
   any importer.
2. **Bugs** — off-by-one, missing awaits, unhandled exceptions, race
   conditions in async code, incorrect Pydantic field types vs what's actually
   written into the lake.
3. **Security** — SQLi/command-injection surfaces (we shell out to pg_dump,
   we handle file uploads), auth bypass paths, secret handling, CORS
   `allow_origins=["*"]` review, path traversal in media endpoints.
4. **Performance** — N+1 queries against the lake, sync I/O in async paths,
   missing `asyncio.gather` opportunities, unbounded in-memory accumulation
   (the feed loop is a prime suspect), hot-path regex compilation.
5. **DRY / structure** — duplicated tag-parsing logic, copy-pasted delta
   write patterns, near-duplicate endpoints, overly long files that are doing
   too many jobs (`api/server.py` at ~1500 lines is a candidate).
6. **Tests** — write pytest tests against the patterns laid down in
   `api/tests/conftest.py`. Prioritize: auth middleware, tag parsing,
   slug generation, engagement scoring math, routine fire/summary pairing.
7. **Types** — add type hints where missing on public functions; don't
   boil the ocean on internals. `mypy` is not wired yet; ruff catches the
   obvious stuff.
8. **Docstrings & comments** — remove obsolete or misleading comments,
   add a one-liner to any public function that doesn't have one. Follow
   the codebase voice (terse, not ceremonial). NO em dashes in anything
   user-facing (see Voice below); internal code comments can use them.
9. **Config & deps** — pin or bump versions in `requirements.txt` and
   addon `package.json`s, remove unused deps, consolidate near-duplicates.
10. **Error messages & UX** — API error responses, CLI output clarity,
    agent log verbosity.

## Commands RALPH should run

From `fathomdx/` root:

```bash
# Python
ruff check .                    # expect violations to trend down over time
ruff format --check .
ruff check --fix .              # apply safe autofixes
pytest                          # must stay green

# Node (once addon node_modules are installed)
npm run lint
npm run format:check
```

CI (`.github/workflows/ci.yml`) runs these on every push — RALPH must
leave main green.

## Commit style

One fix per commit. Match the existing convention from `git log`:

```
<type>(<scope>): <imperative summary>
```

Types seen in history: `feat`, `fix`, `chore`, `copy`, `refactor`.
Examples of good scopes: `api`, `feed`, `tools`, `delta-store`, `agent`,
`cli`, `mcp`. Keep summaries under 72 chars.

## Voice (when editing strings, copy, error messages)

- **Internal / code:** em dashes are fine. Fathom's own voice uses them.
- **User-facing / public:** NO em dashes. No staccato fragments. No
  mic-drop closers. No double-framing. This applies to CLI help text,
  README/QUICKSTART prose, public API error messages, marketing copy.
  (Full guide: `../core/CLAUDE.md` if it exists in this checkout, else
  just: write like a real person, not an announcer.)

## Hard rules

- Do NOT delete or modify files under `site/`, `ui/`, `assets/`, `docs/`.
- Do NOT rewrite `docker-compose.yml` without explicit user approval —
  it's wired to the running lake.
- Do NOT change API contracts (paths, response shapes) under `/v1/*`.
  The agent, CLI, MCP, and browser extension all consume them.
- Do NOT add new top-level files or directories without a clear home.
- Do NOT skip hooks (`--no-verify`) on commits.
- If a fix would take more than ~15 minutes or requires an architectural
  decision, write it up in `PROGRESS.md` under "Needs human" and move on.

## Completion promise

The loop is done when:
1. `ruff check .` reports zero errors on the in-scope paths.
2. `ruff format --check .` reports clean.
3. `pytest` passes with ≥30 tests across api/, delta-store/, source-runner/.
4. `npm run lint` and `npm run format:check` pass.
5. No file in `api/` exceeds 800 lines (forces meaningful splitting).
6. `PROGRESS.md` has an entry for every perspective, at least one pass each.

Output the literal string `RALPH COMPLETE` only when all six are true.
