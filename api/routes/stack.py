"""Stack version endpoint.

The dashboard ships inside the api container, so the api already knows what
version it is — there's no heartbeat half like the agent flow. We just need
to compare the local checkout's HEAD SHA to the upstream main on GitHub and
report "you're N commits behind."

The container has no `git` binary; instead, docker-compose mounts the host's
.git read-only at /repo/.git. We parse the plumbing files directly (HEAD →
refs/heads/<branch> → SHA, with packed-refs as fallback) and derive the
GitHub owner/repo from the `origin` remote URL in .git/config. That keeps
forks working without an env var.
"""

from __future__ import annotations

import re
import time as _time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter

router = APIRouter()

_GIT_DIR = Path("/repo/.git")

# One-hour cache. Same TTL as the agent endpoint — a self-host operator
# isn't going to push and rebuild faster than that, and we don't want every
# dashboard refresh to spend a GitHub API call.
_CACHE: dict = {"payload": None, "checked_at": None}
_CACHE_TTL_SECONDS = 3600

_REMOTE_URL_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
    re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
)


def _read_head() -> tuple[str | None, str | None]:
    """Return (branch, sha). branch is None for detached HEAD."""
    head = _GIT_DIR / "HEAD"
    if not head.exists():
        return None, None
    raw = head.read_text().strip()
    if raw.startswith("ref: "):
        ref = raw[5:].strip()  # e.g. "refs/heads/main"
        branch = ref.split("/", 2)[-1] if ref.startswith("refs/heads/") else None
        sha = _resolve_ref(ref)
        return branch, sha
    # Detached HEAD: HEAD contains the SHA directly.
    return None, raw or None


def _resolve_ref(ref: str) -> str | None:
    """Look up a ref's SHA, checking loose refs then packed-refs."""
    loose = _GIT_DIR / ref
    if loose.exists():
        return loose.read_text().strip() or None
    packed = _GIT_DIR / "packed-refs"
    if packed.exists():
        for line in packed.read_text().splitlines():
            if line.startswith("#") or line.startswith("^"):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def _read_origin_remote() -> tuple[str | None, str | None]:
    """Parse .git/config for [remote "origin"] url, return (owner, repo)."""
    cfg = _GIT_DIR / "config"
    if not cfg.exists():
        return None, None
    in_origin = False
    url = None
    for line in cfg.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped == '[remote "origin"]'
            continue
        if in_origin and stripped.startswith("url"):
            _, _, val = stripped.partition("=")
            url = val.strip()
            break
    if not url:
        return None, None
    for pat in _REMOTE_URL_PATTERNS:
        m = pat.match(url)
        if m:
            return m.group("owner"), m.group("repo")
    return None, None


async def _gather_version() -> dict:
    if not _GIT_DIR.exists():
        return {"error": "git_unavailable"}

    branch, local_sha = _read_head()
    owner, repo = _read_origin_remote()

    payload: dict = {
        "local_sha": local_sha,
        "local_short": local_sha[:7] if local_sha else None,
        "branch": branch,
        "repo": f"{owner}/{repo}" if owner and repo else None,
    }

    if not local_sha:
        payload["error"] = "head_unreadable"
        return payload
    if not owner or not repo:
        payload["error"] = "remote_not_github"
        return payload

    # Always compare against upstream main — the dashboard cares about
    # "you're behind the canonical release", regardless of which branch
    # the local checkout happens to be on.
    upstream_branch = "main"
    base = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "fathomdx-stack-version"}

    try:
        async with httpx.AsyncClient(timeout=8) as c:
            br = await c.get(f"{base}/branches/{upstream_branch}", headers=headers)
            br.raise_for_status()
            upstream_sha = br.json().get("commit", {}).get("sha")
            payload["upstream_sha"] = upstream_sha
            payload["upstream_short"] = upstream_sha[:7] if upstream_sha else None
            payload["upstream_branch"] = upstream_branch

            if upstream_sha and upstream_sha != local_sha:
                cmp_r = await c.get(
                    f"{base}/compare/{local_sha}...{upstream_sha}", headers=headers
                )
                if cmp_r.status_code == 200:
                    cmp = cmp_r.json()
                    payload["behind_by"] = cmp.get("behind_by", 0)
                    payload["ahead_by"] = cmp.get("ahead_by", 0)
                else:
                    # Local SHA isn't on GitHub (private commit, force-pushed
                    # over, etc) — we can still say "out of sync" without an
                    # exact count.
                    payload["behind_by"] = None
                    payload["ahead_by"] = None
                    payload["compare_unavailable"] = True
            else:
                payload["behind_by"] = 0
                payload["ahead_by"] = 0
    except Exception as e:
        payload["error"] = "github_unreachable"
        payload["error_detail"] = str(e)[:200]

    return payload


@router.get("/v1/stack/version")
async def stack_version():
    """Return current stack SHA + how many commits behind origin/main it is."""
    now = _time.time()
    checked = _CACHE.get("checked_at")
    if (
        checked
        and (now - checked) < _CACHE_TTL_SECONDS
        and _CACHE.get("payload")
    ):
        return {
            **_CACHE["payload"],
            "checked_at": datetime.fromtimestamp(checked, UTC).isoformat(),
            "cached": True,
        }

    payload = await _gather_version()
    # Only cache when the GitHub round trip succeeded; otherwise we'd pin a
    # transient failure for an hour.
    if not payload.get("error") or payload.get("error") == "git_unavailable":
        _CACHE.update({"payload": payload, "checked_at": now})

    return {
        **payload,
        "checked_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "cached": False,
    }
