# Deploying everything Fathom

A runbook for shipping the full stack: code pushes, npm publishes,
Cloudflare Pages deploys. Run from this directory (`Fathom/fathomdx/`).

## Surface map

| Surface | Source | Git remote | Cloudflare project |
|---|---|---|---|
| Code | `fathomdx/` (here) | `fathomdx-io/fathomdx` | — |
| `fathomdx.io` | `../fathomdx-site/` | `fathomdx-io/fathomdx.io-site` | `fathomdx-site` |
| `hifathom.com` | `../web/hifathom/` | `fathomdx-io/hifathom.com-site` | `fathoms-log` |
| `design.fathomdx.io` | `../web/design/` | `fathomdx-io/fathomdx-ds` | `fathom-design` |

| npm package | Source dir |
|---|---|
| `fathom-agent` | `addons/agent/` |
| `fathom-delta-cli` | `addons/cli/` |
| `fathom-mcp` | `addons/mcp-node/` |
| `fathom-connect` | `addons/connect/` |

`web/hifathom-analytics/` and `core/` are out of scope for this runbook.

## Prerequisites

- `gh auth status` shows logged in as `fathomdx-io`. If not:
  `gh auth login` (device flow), pick `HTTPS`, sign in as fathomdx-io.
  After first auth, `gh auth refresh -h github.com -s workflow` to allow
  pushes that touch `.github/workflows/*`.
- `npm whoami` works. Publishing requires the npm account that owns
  the four packages.
- `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` exported, or
  passed inline to `wrangler`.
- All four sibling repos exist at the paths above with `origin` set.

## Order

Bumps land before docs rebuild so the rebuilt site footers reference
the latest fathomdx commit. Pushing to GitHub is independent of the
Cloudflare deploy — wrangler uploads `dist/` directly.

1. **Sweep trash** (this repo)
2. **Bump + publish each changed addon**
3. **Push fathomdx**
4. **Rebuild + push + deploy fathomdx.io**
5. **Push + deploy hifathom.com**
6. **Push + deploy design.fathomdx.io**
7. **Verify**

---

## 1. Sweep trash

```bash
# Stale Dropbox conflict copies of git index (.git internal)
rm -f .git/index.sync-conflict-*

# Python editable-install metadata, gitignored but stale
rm -rf fathomdx.egg-info

# Old playwright session logs
find .playwright-cli -name "console-*.log" -mtime +14 -delete
```

Repeat the same `*.sync-conflict*` sweep in any sibling repo before
its build step (Astro and vite both choke on them).

## 2. Bump + publish each changed addon

For each addon, check whether anything in its published `files:` list
has changed since the last release commit:

```bash
# Replace <name> with: agent | cli | mcp-node | connect
LAST=$(git log -1 --format=%H --grep="chore(release): fathom-.*" -- addons/<name>/package.json)
git log --oneline $LAST..HEAD -- addons/<name>/
```

If the result is empty, skip — no bump needed.

If there are commits, bump the version:

```bash
cd addons/<name>
# Edit package.json "version"; pick patch (0.x.Y+1) for fixes/internal
# refactors, minor (0.X+1.0) for new behavior consumers can use.
npm install --package-lock-only          # refresh package-lock
cd ../..
git add addons/<name>/package.json addons/<name>/package-lock.json
git commit -m "chore(release): fathom-<pkg>@<ver>"
```

Then publish (after step 3 pushes the commit):

```bash
cd addons/<name> && npm publish && cd ../..
```

Verify with `npm view <pkg> version`.

## 3. Push fathomdx

```bash
git push origin main
```

## 4. fathomdx.io

```bash
cd ../fathomdx-site

# Pull the latest install.sh from the fathomdx repo
npm run sync:install
# If sync:install produced a commit, this will be staged automatically.
# Otherwise nothing to do here.

# Rebuild docs (pulls from ../fathomdx/docs, embeds current HEAD hash
# in the page footer) and the rest of the site
npm run build

# Commit rebuilt docs if anything changed
git add public/docs
git diff --cached --quiet || git commit -m "docs: rebuild from fathomdx @ $(git -C ../fathomdx rev-parse --short HEAD)"

git push origin main

# Cloudflare Pages deploy (production alias follows --branch main)
npx wrangler pages deploy dist --project-name fathomdx-site --branch main
```

## 5. hifathom.com

```bash
cd ../web/hifathom

find . -name "*.sync-conflict*" -delete
npm run build

git push origin main          # if there are commits

npx wrangler pages deploy dist --project-name fathoms-log --branch main
```

## 6. design.fathomdx.io

```bash
cd ../web/design

git push origin main          # if there are commits
npm run deploy                # wraps wrangler pages deploy public ...
```

The versioned URL contract is `https://design.fathomdx.io/v1/tokens.css`.
Breaking changes cut `v2/` — never edit `v1/` in place.

## 7. Verify

```bash
curl -sI https://fathomdx.io           | head -1
curl -s  https://fathomdx.io/install.sh| head -3
curl -sI https://hifathom.com          | head -1
curl -sI https://design.fathomdx.io/v1/tokens.css | head -1

npm view fathom-agent version
npm view fathom-delta-cli version
npm view fathom-mcp version
npm view fathom-connect version
```

Each curl should return `HTTP/2 200`. The npm versions should match
the `addons/*/package.json` values you just shipped.
