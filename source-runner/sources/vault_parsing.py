"""Pure parsing for vault markdown notes.

Takes file content, returns structured chunks with Obsidian-aware metadata.
No I/O — file reading, image upload, HTTP are the caller's responsibility.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────

MIN_CHUNK_LENGTH = 40
MAX_CHUNK_LENGTH = 2000
MEDIUM_DOC_THRESHOLD = 2000
LARGE_DOC_THRESHOLD = 8000

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SKIP_DIRS = {".claude", ".git", ".obsidian", "data-backup", "__pycache__"}
SKIP_FILES = {"CLAUDE.md"}

# ── Regexes ──────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_FRONTMATTER_TAGS_RE = re.compile(
    r"^\s*tags:\s*\[([^\]]+)\]|^\s*tags:\s*\n((?:\s*-\s*\S+\s*(?:\n|$))+)",
    re.MULTILINE,
)
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]+)?\]\]")
_EMBED_RE = re.compile(r"!\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
_STD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
# Inline #tag — avoid matching headings (# Foo) and URLs (#fragment after /)
_HASHTAG_RE = re.compile(r"(?:^|(?<=\s))#([a-zA-Z][\w/-]*)(?=\s|$|[.,;:!?])")
_DATAVIEW_RE = re.compile(r"```dataview.*?```", re.DOTALL)
_CODEBLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


# ── Data types ───────────────────────────────────────────────────────────


@dataclass
class ImageRef:
    """An image referenced by a markdown document."""

    alt: str
    src: str  # raw src as written — wikilink target, relative path, or URL


@dataclass
class ParsedChunk:
    """One chunk of a parsed document."""

    index: int
    content: str
    heading_trail: str
    content_hash: str
    inline_tags: list[str] = field(default_factory=list)
    wikilinks: list[str] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)


@dataclass
class ParsedDoc:
    """Result of parsing a single markdown file."""

    relpath: str
    workspace: str
    doc_tag: str
    frontmatter_tags: list[str]
    chunks: list[ParsedChunk]
    all_images: list[ImageRef]  # union of every chunk's images, in source order


# ── Public parsing entrypoint ────────────────────────────────────────────


def parse_document(content: str, *, workspace: str, relpath: str) -> ParsedDoc:
    """Parse a vault markdown document into chunks + metadata.

    Pure function — no I/O. Image refs are returned as raw srcs; caller resolves.
    """
    # 1. Strip frontmatter, capture tags
    frontmatter_tags, body = _extract_frontmatter(content)

    # 2. Strip dataview blocks (generated, not sediment)
    body = _DATAVIEW_RE.sub("", body)

    # 3. Chunk via cascade
    raw_chunks = chunk_cascade(body, max_size=MAX_CHUNK_LENGTH, min_size=MIN_CHUNK_LENGTH)

    # 4. Extract per-chunk metadata
    chunks: list[ParsedChunk] = []
    all_images: list[ImageRef] = []
    heading_trail = ""

    for i, chunk_body in enumerate(raw_chunks):
        # Update heading trail if this chunk starts with a heading
        m = _HEADING_RE.search(chunk_body)
        if m and chunk_body.lstrip().startswith("#"):
            heading_trail = m.group(2).strip()

        inline_tags = _extract_hashtags(chunk_body)
        wikilinks = sorted(set(_WIKILINK_RE.findall(chunk_body)))
        images = _extract_images(chunk_body)
        all_images.extend(images)

        content_hash = hashlib.sha256(chunk_body.encode("utf-8")).hexdigest()[:8]
        chunks.append(
            ParsedChunk(
                index=i,
                content=chunk_body,
                heading_trail=heading_trail,
                content_hash=content_hash,
                inline_tags=inline_tags,
                wikilinks=wikilinks,
                images=images,
            )
        )

    return ParsedDoc(
        relpath=relpath,
        workspace=workspace,
        doc_tag=doc_tag(workspace, relpath),
        frontmatter_tags=frontmatter_tags,
        chunks=chunks,
        all_images=_dedup_image_refs(all_images),
    )


# ── Cascade chunker ──────────────────────────────────────────────────────


def chunk_cascade(
    text: str, *, max_size: int = MAX_CHUNK_LENGTH, min_size: int = MIN_CHUNK_LENGTH
) -> list[str]:
    """Split text into chunks, always under max_size. Falls through heuristics.

    Cascade (each step only applied if the chunk still exceeds max_size):
      1. H1/H2 headings
      2. H3-H6 headings + horizontal rules
      3. Double-blank paragraph boundaries
      4. Single newlines
      5. Sentence boundaries
      6. Word boundaries
      7. Character boundaries (last resort)

    Chunks below min_size are merged into the next chunk to avoid fragmentation.
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    # Protect code blocks from being split by later heuristics
    # (we chunk by boundaries that don't split across ``` fences)
    parts = _split_preserving_codeblocks(text)
    out: list[str] = []
    for part in parts:
        out.extend(_chunk_part(part, max_size=max_size))

    # Merge too-small chunks forward
    merged: list[str] = []
    for chunk in out:
        chunk = chunk.strip()
        if not chunk:
            continue
        if merged and len(chunk) < min_size and len(merged[-1]) + len(chunk) + 2 <= max_size:
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        else:
            merged.append(chunk)
    return merged


def _split_preserving_codeblocks(text: str) -> list[str]:
    """Split text into segments, keeping code fences intact as single segments."""
    segments: list[str] = []
    last = 0
    for m in _CODEBLOCK_RE.finditer(text):
        if m.start() > last:
            segments.append(text[last : m.start()])
        segments.append(m.group(0))
        last = m.end()
    if last < len(text):
        segments.append(text[last:])
    return [s for s in segments if s.strip()]


def _chunk_part(text: str, *, max_size: int) -> list[str]:
    """Apply the cascade to one segment of text. Code-block segments pass through whole."""
    if len(text) <= max_size:
        return [text]
    # If this is a pure code block we don't split — the model can read a single oversized
    # chunk better than a garbled one. Truncation is not our job here; surface it.
    if text.lstrip().startswith("```"):
        return [text[:max_size]]  # code blocks are rare + bounded; hard cut is ok

    # 1. Split on H1/H2
    chunks = _split_on_regex(text, re.compile(r"\n(?=#{1,2}\s)"))
    if all(len(c) <= max_size for c in chunks):
        return chunks
    chunks = _descend(chunks, max_size, _split_on_h3_plus_hr)
    if all(len(c) <= max_size for c in chunks):
        return chunks
    chunks = _descend(chunks, max_size, _split_on_double_newline)
    if all(len(c) <= max_size for c in chunks):
        return chunks
    chunks = _descend(chunks, max_size, _split_on_single_newline)
    if all(len(c) <= max_size for c in chunks):
        return chunks
    chunks = _descend(chunks, max_size, _split_on_sentence)
    if all(len(c) <= max_size for c in chunks):
        return chunks
    chunks = _descend(chunks, max_size, _split_on_word)
    if all(len(c) <= max_size for c in chunks):
        return chunks
    # Last resort: character split
    chunks = _descend(chunks, max_size, lambda t: _split_on_chars(t, max_size))
    return chunks


def _descend(chunks: list[str], max_size: int, splitter) -> list[str]:
    """Apply splitter to any chunk still over max_size; leave others alone."""
    out: list[str] = []
    for c in chunks:
        if len(c) <= max_size:
            out.append(c)
        else:
            out.extend(splitter(c))
    return out


def _split_on_regex(text: str, pattern: re.Pattern) -> list[str]:
    return [s for s in pattern.split(text) if s.strip()]


def _split_on_h3_plus_hr(text: str) -> list[str]:
    return _split_on_regex(text, re.compile(r"\n(?=#{3,6}\s)|\n---+\n"))


def _split_on_double_newline(text: str) -> list[str]:
    return _split_on_regex(text, re.compile(r"\n{2,}"))


def _split_on_single_newline(text: str) -> list[str]:
    return _split_on_regex(text, re.compile(r"\n"))


def _split_on_sentence(text: str) -> list[str]:
    # Sentence boundary: [.!?] followed by whitespace and a capital or newline
    return _split_on_regex(text, re.compile(r"(?<=[.!?])\s+(?=[A-Z\n])"))


def _split_on_word(text: str) -> list[str]:
    return _split_on_regex(text, re.compile(r"\s+"))


def _split_on_chars(text: str, max_size: int) -> list[str]:
    return [text[i : i + max_size] for i in range(0, len(text), max_size)]


# ── Frontmatter ──────────────────────────────────────────────────────────


def _extract_frontmatter(content: str) -> tuple[list[str], str]:
    """Return (tags_from_frontmatter, body_without_frontmatter)."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return [], content
    block = m.group(1)
    body = content[m.end() :]
    tags: list[str] = []
    tm = _FRONTMATTER_TAGS_RE.search(block)
    if tm:
        inline_list, multiline = tm.groups()
        if inline_list:
            tags = [t.strip().strip("\"'") for t in inline_list.split(",") if t.strip()]
        elif multiline:
            tags = [
                line.strip().lstrip("-").strip().strip("\"'")
                for line in multiline.splitlines()
                if line.strip().startswith("-")
            ]
    # Normalize tags: lowercase, hyphenate spaces
    tags = [t.lower().replace(" ", "-") for t in tags if t]
    return tags, body


def _extract_hashtags(text: str) -> list[str]:
    """Extract inline #tags from markdown body. Excludes headings and URL fragments."""
    # Remove code blocks and heading lines to avoid false positives
    body = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    body = re.sub(r"`[^`]+`", "", body)
    # A heading is "# " / "## " etc — hash(es) followed by whitespace
    body = "\n".join(line for line in body.splitlines() if not _HEADING_LINE_RE.match(line))
    tags = _HASHTAG_RE.findall(body)
    # Normalize + dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        norm = t.lower()
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _extract_images(text: str) -> list[ImageRef]:
    """Extract both Obsidian embeds ![[foo.png]] and standard ![alt](path)."""
    refs: list[ImageRef] = []
    for m in _EMBED_RE.finditer(text):
        src = m.group(1).strip()
        if _looks_like_image(src):
            refs.append(ImageRef(alt=Path(src).stem, src=src))
    for m in _STD_IMG_RE.finditer(text):
        refs.append(ImageRef(alt=m.group(1), src=m.group(2).strip()))
    return refs


def _looks_like_image(src: str) -> bool:
    lower = src.lower()
    return any(lower.endswith(ext) for ext in IMAGE_EXTENSIONS)


def _dedup_image_refs(refs: list[ImageRef]) -> list[ImageRef]:
    seen: set[str] = set()
    out: list[ImageRef] = []
    for r in refs:
        if r.src not in seen:
            seen.add(r.src)
            out.append(r)
    return out


# ── Identifier helpers ───────────────────────────────────────────────────


def doc_tag(workspace: str, relpath: str) -> str:
    """Stable document identifier tag. Used for idempotent re-imports."""
    slug = relpath.replace("/", "-").replace("\\", "-")
    if slug.endswith(".md"):
        slug = slug[:-3]
    return f"doc:{workspace}/{slug}"


def chunk_raw_item_id(doc_tag_str: str, index: int, content_hash: str) -> str:
    """Stable RawItem ID for a chunk. Used by runner seen_ids dedup."""
    return f"{doc_tag_str}:chunk:{index:03d}:{content_hash}"


def diff_raw_item_id(doc_tag_str: str, mtime_ns: int) -> str:
    return f"{doc_tag_str}:diff:{mtime_ns}"


def tombstone_raw_item_id(doc_tag_str: str, when_ns: int) -> str:
    return f"{doc_tag_str}:tombstone:{when_ns}"


def subfolder_tag(relpath: str) -> str | None:
    parts = Path(relpath).parts
    if len(parts) > 1:
        return f"vault:{parts[0]}"
    return None


def dedup_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ── File discovery (filesystem helpers, but no network I/O) ──────────────


def find_vault_files(vault_path: Path) -> list[Path]:
    """All .md files in a vault, skipping dotfiles and known junk."""
    files: list[Path] = []
    for p in sorted(vault_path.rglob("*.md")):
        parts = p.relative_to(vault_path).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in parts):
            continue
        if p.name in SKIP_FILES:
            continue
        files.append(p)
    return files


def find_vault_images(vault_path: Path) -> list[Path]:
    """All image files in a vault."""
    images: list[Path] = []
    for ext in IMAGE_EXTENSIONS:
        for p in sorted(vault_path.rglob(f"*{ext}")):
            parts = p.relative_to(vault_path).parts
            if any(part in SKIP_DIRS or part.startswith(".") for part in parts):
                continue
            images.append(p)
    return images


def resolve_image_src(src: str, md_file: Path, vault_path: Path) -> tuple[str, Path | None]:
    """Resolve an image src to (type, path_or_None).

    Returns ("local", Path) for local files, ("url", None) for URLs,
    ("missing", None) when we can't locate it.
    """
    if src.startswith(("http://", "https://")):
        return "url", None

    # Try: relative to markdown file
    candidate = (md_file.parent / src).resolve()
    if candidate.exists() and candidate.suffix.lower() in IMAGE_EXTENSIONS:
        return "local", candidate

    # Try: relative to vault root (Obsidian embed syntax often uses bare filenames)
    candidate = (vault_path / src).resolve()
    if candidate.exists() and candidate.suffix.lower() in IMAGE_EXTENSIONS:
        return "local", candidate

    # Try: search vault by filename (Obsidian resolves embeds by name)
    name = Path(src).name
    for img in find_vault_images(vault_path):
        if img.name == name:
            return "local", img

    return "missing", None
