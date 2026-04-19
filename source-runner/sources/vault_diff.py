"""Diff computation for vault note modifications.

Compute a unified-style diff between the last-seen and current file content,
render it as a readable diff-delta body. Large diffs are summarized.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

MAX_DIFF_DELTA_LENGTH = 2000
MAX_HUNK_LENGTH = 800


@dataclass
class DiffHunk:
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.removed and not self.added

    def render(self) -> str:
        parts: list[str] = []
        if self.removed:
            body = "\n".join(self.removed)
            if len(body) > MAX_HUNK_LENGTH:
                body = body[:MAX_HUNK_LENGTH] + "…"
            parts.append(f"--- removed ---\n{body}")
        if self.added:
            body = "\n".join(self.added)
            if len(body) > MAX_HUNK_LENGTH:
                body = body[:MAX_HUNK_LENGTH] + "…"
            parts.append(f"+++ added +++\n{body}")
        return "\n\n".join(parts)


@dataclass
class DiffSummary:
    relpath: str
    hunks: list[DiffHunk]
    lines_added: int
    lines_removed: int
    identical: bool

    @property
    def is_noop(self) -> bool:
        return self.identical or not self.hunks


def compute_diff(old: str, new: str, *, relpath: str) -> DiffSummary:
    """Compare old vs new text, return structured hunks."""
    if old == new:
        return DiffSummary(
            relpath=relpath, hunks=[], lines_added=0, lines_removed=0, identical=True
        )

    old_lines = old.splitlines()
    new_lines = new.splitlines()

    diff_iter = difflib.unified_diff(old_lines, new_lines, n=0, lineterm="")
    hunks: list[DiffHunk] = []
    current = DiffHunk()
    added = removed = 0

    for line in diff_iter:
        if line.startswith(("---", "+++", "@@")):
            # hunk boundary — flush current if it has content
            if not current.is_empty:
                hunks.append(current)
                current = DiffHunk()
            continue
        if line.startswith("+"):
            current.added.append(line[1:])
            added += 1
        elif line.startswith("-"):
            current.removed.append(line[1:])
            removed += 1

    if not current.is_empty:
        hunks.append(current)

    return DiffSummary(
        relpath=relpath,
        hunks=hunks,
        lines_added=added,
        lines_removed=removed,
        identical=False,
    )


def render_diff_delta(summary: DiffSummary) -> str:
    """Render a DiffSummary as the content body of a diff-delta.

    If the full rendering would exceed MAX_DIFF_DELTA_LENGTH, emit a summarized
    form instead: counts + first hunk only.
    """
    if summary.is_noop:
        return ""

    header = f"vault-diff: {summary.relpath}"
    counts = f"+{summary.lines_added} lines added, -{summary.lines_removed} lines removed"

    body_parts = [h.render() for h in summary.hunks if not h.is_empty]
    full = f"{header}\n\n{counts}\n\n" + "\n\n".join(body_parts)

    if len(full) <= MAX_DIFF_DELTA_LENGTH:
        return full

    # Truncated form: counts + first hunk + "... and N more hunks"
    first = body_parts[0] if body_parts else ""
    rest_count = max(len(body_parts) - 1, 0)
    tail = f"\n\n… and {rest_count} more hunk(s)" if rest_count else ""
    truncated = f"{header}\n\n{counts}\n\n{first}{tail}"
    if len(truncated) > MAX_DIFF_DELTA_LENGTH:
        # First hunk itself too long — hard cap
        truncated = truncated[: MAX_DIFF_DELTA_LENGTH - 1] + "…"
    return truncated


def render_tombstone(relpath: str, last_content: str) -> str:
    """Content body for a deletion tombstone delta."""
    snippet = last_content.strip().replace("\n", " ")[:240]
    suffix = "…" if len(last_content) > 240 else ""
    return (
        f"Vault note deleted: {relpath}\n\nLast-known snippet: {snippet}{suffix}"
        if snippet
        else f"Vault note deleted: {relpath}"
    )
