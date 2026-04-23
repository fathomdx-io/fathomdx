# Ralph PRD — fathomdx cleanup loop

Read this every iteration. It is the contract for what Ralph is allowed to
touch, which perspectives to rotate through, and how to commit.
`ralph-progress.md` tracks the coverage matrix + iteration log.
`ralph-findings.json` tracks cumulative metrics.

## Repos

Only one repo is in scope for this Ralph run: **`fathomdx`** (this one).

The sibling repos (`../core`, `../web/hifathom`, `../web/design`) have their
own lifecycles and CLAUDE.md files — do not cross the boundary.

## Paths

**In scope:**
- `api/` — FastAPI consumer API (~11k LOC Python, the main body of work)
- `delta-store/deltas/` — HTTP API over the lake (Python)
- `source-runner/` — external source pollers (Python)
- `addons/` — Node CLI, agent, connect, mcp-node, browser-extension

**Out of scope — do NOT touch:**
- `site/` — being Ralph'd in its own repo, don't double-edit
- `ui/`, `assets/` — vendored / generated
- `docs/` — prose, not code
- `data/` — runtime state (gitignored)

## Perspectives

Use the skill's 25-perspective taxonomy. Not all apply here; the matrix in
`ralph-progress.md` marks in-scope cells. The perspectives with frontend
focus (Accessibility, Mobile-First, Visual Language, most of Phase 6 UX)
are mostly N/A because `site/` and `ui/` are excluded. Keep them in the
matrix marked `N/A` rather than deleting them, so the scope decision is
visible.

**Priority order for this repo** (suggested "Next" progression):
1. Dead Code & Cleanup (ruff's 121 errors — low-risk warmup)
2. Senior Dev Audit (server.py at 1500 lines is a consolidation target)
3. Bug Hunt (async/await slippage, race conditions in chat_listener)
4. Quality Scaffold (error handling around delta_client retries)
5. Test Creation (huge gap — target auth, tag parsing, slug, scoring)
6. Security Review (auth middleware, file upload paths, pg_dump shellout)
7. Performance (feed loop allocations, N+1 on lake queries)
8. Dependency Audit (requirements.txt has no pins; package.json is bare)
9. Cross-Repo Coherence (api ↔ addons contract check)
10. API Consistency (endpoint naming, error-response shape)
11. Docker & DevOps (Dockerfile layer caching, compose healthchecks)
12. Error Boundary Audit (map every try/except, find swallowed errors)
13. Utility Consolidation (tag-parsing, delta-write boilerplate)

UX perspectives (16–25) are mostly N/A here. Chat & Conversation UX (#17)
partially applies to chat_listener behavior — defer until #1–13 are done.

## Commands Ralph must run

From `fathomdx/` root:

```bash
# Python
ruff check .                    # track violation count down in findings.json
ruff format --check .
ruff check --fix .              # safe autofixes are fair game
pytest                          # must stay green

# Node (once addon node_modules are installed)
npm run lint
npm run format:check
```

CI (`.github/workflows/ci.yml`) runs all of these on push — Ralph must
leave main green.

## Commit discipline (non-negotiable)

One fix per commit. Match the existing convention from `git log`:

```
<type>(<scope>): <imperative summary, ≤72 chars, no period>
```

Types in history: `feat`, `fix`, `chore`, `copy`, `refactor`.
Scopes in history: `api`, `feed`, `tools`, `delta-store`, `agent`,
`cli`, `mcp`, `site`, `write`.

Body (wrapped ~72) explains *why* when non-obvious. Skip for trivial.
Include `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
on each commit.

Never skip hooks (`--no-verify`). If pre-commit fails, fix the issue and
make a new commit — don't amend.

## Voice (when editing strings, copy, error messages)

- **Internal / code / dev-facing comments:** em dashes are fine. Fathom's
  own voice uses them.
- **User-facing / public strings:** NO em dashes. No staccato fragments.
  No mic-drop closers. No double-framing. This applies to CLI help text,
  README/QUICKSTART prose, public API error messages, marketing copy.
  Write like a real person, not an announcer.

## Hard rules

- Do NOT modify files under `site/`, `ui/`, `assets/`, `docs/`, `data/`.
- Do NOT rewrite `docker-compose.yml` without explicit user approval —
  it's wired to the running lake.
- Do NOT change API contracts (paths, response shapes) under `/v1/*`.
  The agent, CLI, MCP, and browser extension all consume them.
- Do NOT add new top-level files or directories without a clear home.
- If a fix would take more than ~15 minutes or requires an architectural
  decision, write it up in `ralph-progress.md` under "Needs human" and
  move on to something else.

## Memory (Fathom MCP)

This project has a memory lake. Before each perspective, call
`mcp__fathom__recall` with tag `ralph` and the perspective name to pull
accumulated context from prior iterations. After each iteration, call
`mcp__fathom__write` with tag `ralph` and content formatted as
instructions to your future self (not logs). See the skill's
"Knowledge Accumulation" section.

## Reporting (no external channels)

There is no `#general` room for this project. The iteration summary goes
in `ralph-progress.md` (append-only) AND as a Fathom write with tag
`ralph`. Do not post to external services.

## Completion

The Ralph run for fathomdx is complete when:

1. `ruff check .` reports zero errors on in-scope paths.
2. `ruff format --check .` reports clean.
3. `pytest` passes with ≥30 tests across api/, delta-store/, source-runner/.
4. `npm run lint` and `npm run format:check` pass.
5. No file in `api/` exceeds 800 lines (forces meaningful splitting).
6. Coverage matrix in `ralph-progress.md` has every in-scope cell marked
   DONE (N/A cells don't need work).

Output the literal string `RALPH COMPLETE` only when all six are true.
