"""Media proxy endpoints.

Upload/fetch images through the consumer API so the UI has one host.
Also writes the companion "context" delta for browser captures, so an
image in the lake is always accompanied by text explaining what it is.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from .. import db, delta_client
from .._tags import has_any_tag_with_prefix

router = APIRouter()


@router.get("/v1/media/{media_hash}")
async def proxy_media(media_hash: str):
    """Proxy image from delta store so the consumer UI has one API surface."""
    c = await delta_client._get()
    r = await c.get(f"/media/{media_hash}", timeout=15)
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail="media not found")
    return Response(content=r.content, media_type="image/webp")


@router.post("/v1/media/upload")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(""),
    content: str = Form(""),
    expires_at: str = Form(""),
    tags: str = Form(""),
    source: str = Form(""),
    write_delta: str = Form("auto"),
):
    """Upload an image. Returns ``{id, media_hash}`` (id may be empty).

    Two modes, picked via ``write_delta``:

    * ``"auto"`` (default) — write a companion lake delta only when the
      caller passed ``content`` or explicit ``tags``/``source``. The
      composer-image flow (no caption text, no extra tags) gets bytes
      only; ``post_seed`` then attaches the returned ``media_hash`` to
      its own user-seed delta. Without this, every image upload spawned
      a duplicate "fathom-chat / participant:user / image" delta that
      surfaced alongside the real seed and rendered the image twice.
    * ``"true"``/``"1"`` — always write the delta (legacy chat UI,
      browser-capture flows that genuinely want a standalone image
      record).
    * ``"false"``/``"0"`` — never write the delta; just return the hash.
    """
    file_bytes = await file.read()
    mode = (write_delta or "auto").strip().lower()

    if mode in ("false", "0", "no"):
        wants_delta = False
    elif mode in ("true", "1", "yes"):
        wants_delta = True
    else:
        # Auto: write only when the caller declared intent via content
        # or explicit tags/source. Composer's "just upload bytes" call
        # passes none of these and gets the bytes-only path.
        wants_delta = bool(content or tags or source)

    if not wants_delta:
        return await delta_client.upload_media_bytes(
            file_bytes=file_bytes,
            filename=file.filename or "upload.jpg",
        )

    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        tag_list = [db.LAKE_CHAT_TAG, "user", "participant:user", "image"]

    if session_id:
        tag_list.append(f"chat:{session_id}")

    contact = getattr(request.state, "contact", None)
    contact_slug = (contact or {}).get("slug")
    if contact_slug and not has_any_tag_with_prefix(tag_list, "contact:"):
        tag_list.append(f"contact:{contact_slug}")

    return await delta_client.upload_media(
        file_bytes=file_bytes,
        filename=file.filename or "upload.jpg",
        content=content,
        tags=tag_list,
        source=source or db.LAKE_CHAT_SOURCE,
        expires_at=expires_at or None,
    )


class CaptureContext(BaseModel):
    media_hash: str
    content: str = ""
    tags: list[str] = []
    source: str = "browser-capture"
    expires_at: str | None = None


@router.post("/v1/media/capture-context")
async def capture_context(req: CaptureContext):
    """Write a context delta for a browser-captured image.

    The image is already in delta-store (uploaded via /v1/media/upload).
    This writes a companion text delta linking the media_hash to the
    story content so the lake knows what the image means.

    ``expires_at`` (optional ISO timestamp) makes the context delta
    short-lived alongside the image — both should expire together so
    browse captures don't leave dangling context in the lake.
    """
    c = await delta_client._get()
    body: dict = {
        "content": req.content or f"[captured image:{req.media_hash}]",
        "tags": req.tags or ["browser-capture"],
        "source": req.source,
        "media_hash": req.media_hash,
        "modality": "image",
    }
    if req.expires_at:
        body["expires_at"] = req.expires_at
    r = await c.post("/deltas", json=body)
    r.raise_for_status()
    return r.json()
