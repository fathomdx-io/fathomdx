"""Source-runner proxy endpoints.

Thin passthrough to the source-runner container so the browser can
manage external sources (RSS, Mastodon, vault, etc.) without talking
to a second host. Admin-gated except for the read endpoints.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, delta_client
from ..settings import settings

router = APIRouter()


class SourceCreate(BaseModel):
    source_type: str
    config: dict
    name: str = ""
    interval_minutes: int = 30
    expiry_days: float | None = 30


class SourceUpdate(BaseModel):
    config: dict | None = None
    interval_minutes: int | None = None
    expiry_days: float | None = None


def _source_runner() -> httpx.AsyncClient:
    """Lazy client for source-runner API."""
    return httpx.AsyncClient(
        base_url=settings.source_runner_url.rstrip("/"),
        timeout=15,
    )


@router.get("/v1/sources")
async def list_sources():
    async with _source_runner() as c:
        r = await c.get("/api/sources")
        r.raise_for_status()
        return r.json()


@router.get("/v1/sources/types")
async def list_source_types():
    async with _source_runner() as c:
        r = await c.get("/api/sources/types")
        r.raise_for_status()
        return r.json()


@router.post("/v1/sources", dependencies=[Depends(auth.require_admin)])
async def create_source(req: SourceCreate):
    async with _source_runner() as c:
        r = await c.post("/api/sources", json=req.model_dump())
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json().get("detail", r.text))
        return r.json()


@router.put("/v1/sources/{source_id}", dependencies=[Depends(auth.require_admin)])
async def update_source(source_id: str, req: SourceUpdate):
    # Include explicitly-set fields (even if None, for "forever" expiry)
    body = dict(req.model_dump(exclude_unset=True).items())
    async with _source_runner() as c:
        r = await c.put(f"/api/sources/{source_id}", json=body)
        if r.status_code == 404:
            raise HTTPException(404, f"Source not found: {source_id}")
        r.raise_for_status()
        return r.json()


@router.post("/v1/sources/{source_id}/pause", dependencies=[Depends(auth.require_admin)])
async def pause_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/pause")
        r.raise_for_status()
        return r.json()


@router.post("/v1/sources/{source_id}/resume", dependencies=[Depends(auth.require_admin)])
async def resume_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/resume")
        r.raise_for_status()
        return r.json()


@router.post("/v1/sources/{source_id}/poll", dependencies=[Depends(auth.require_admin)])
async def poll_source(source_id: str):
    async with _source_runner() as c:
        r = await c.post(f"/api/sources/{source_id}/poll")
        if r.status_code == 404:
            raise HTTPException(404, f"Source not found: {source_id}")
        r.raise_for_status()
        return r.json()


@router.delete("/v1/sources/{source_id}", dependencies=[Depends(auth.require_admin)])
async def delete_source(source_id: str):
    async with _source_runner() as c:
        r = await c.delete(f"/api/sources/{source_id}")
        r.raise_for_status()
        return r.json()


@router.get("/v1/sources/{source_id}/detail")
async def source_detail(source_id: str):
    """Fetch source metadata + recent deltas + time-windowed counts."""
    # Get source info from source-runner
    async with _source_runner() as c:
        r = await c.get(f"/api/sources/{source_id}")
        if r.status_code == 404:
            raise HTTPException(404, f"Source not found: {source_id}")
        r.raise_for_status()
        source = r.json()

    # The source field in deltas is "{type}/{id}" for scoped sources
    source_type = source.get("source_type", "")
    delta_source = source.get("source", source_type)
    if delta_source == source_type:
        delta_source = f"{source_type}/{source_id}"

    now = datetime.now(UTC)
    t_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    t_7d = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Fetch recent deltas + counts in parallel
    recent, last_24h, last_7d = await asyncio.gather(
        delta_client.query(limit=20, source=delta_source),
        delta_client.query(limit=1000, source=delta_source, time_start=t_24h),
        delta_client.query(limit=5000, source=delta_source, time_start=t_7d),
    )

    # Slim down recent deltas for the response
    deltas = []
    for d in recent[:20]:
        deltas.append(
            {
                "id": d.get("id"),
                "content": d.get("content") or "",
                "timestamp": d.get("timestamp"),
                "tags": d.get("tags", []),
                "media_hash": d.get("media_hash"),
            }
        )

    return {
        "source": source,
        "counts": {
            "last_24h": len(last_24h),
            "last_7d": len(last_7d),
            "all_time": source.get("deltaCount") or 0,
        },
        "deltas": deltas,
    }
