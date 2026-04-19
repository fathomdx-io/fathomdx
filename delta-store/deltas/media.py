"""Media storage for image deltas.

Content-addressable flat directory. Images are preprocessed on ingest:
resized to max 1920px on longest edge, converted to WebP, targeting ~1MB.
Filenames are SHA-256 hashes (first 16 hex chars) + .webp extension.
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger("delta-store.media")

MAX_EDGE = 1920
WEBP_QUALITY = 82  # good balance for ~1MB at 1920px


def _ensure_dir(media_dir: Path) -> Path:
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def preprocess(data: bytes) -> bytes:
    """Resize and convert image bytes to WebP. Returns processed bytes."""
    img = Image.open(io.BytesIO(data))

    # Convert palette/RGBA modes as needed for WebP
    if img.mode in ("P", "PA"):
        img = img.convert("RGBA")
    if img.mode == "RGBA":
        # Keep alpha for WebP (it supports it)
        pass
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Resize if larger than MAX_EDGE
    w, h = img.size
    if max(w, h) > MAX_EDGE:
        scale = MAX_EDGE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY)
    return buf.getvalue()


def content_hash(data: bytes) -> str:
    """SHA-256 hash, first 16 hex chars."""
    return hashlib.sha256(data).hexdigest()[:16]


def ingest(media_dir: Path, data: bytes) -> str:
    """Preprocess and store image. Returns the media hash (filename stem).

    If the hash already exists, skips writing (content-addressable dedup).
    """
    processed = preprocess(data)
    h = content_hash(processed)
    filename = f"{h}.webp"
    dest = _ensure_dir(media_dir) / filename

    if not dest.exists():
        dest.write_bytes(processed)
        log.info("Stored media %s (%d bytes)", filename, len(processed))
    else:
        log.debug("Media %s already exists, skipping", filename)

    return h


def resolve(media_dir: Path, media_hash: str) -> Path | None:
    """Resolve a media hash to a file path. Returns None if not found."""
    path = media_dir / f"{media_hash}.webp"
    return path if path.exists() else None


def delete(media_dir: Path, media_hash: str) -> bool:
    """Delete a media file by hash. Returns True if deleted."""
    path = media_dir / f"{media_hash}.webp"
    if path.exists():
        path.unlink()
        return True
    return False
