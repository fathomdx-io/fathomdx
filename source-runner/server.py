"""Source Runner API — manages source plugins that feed the delta lake."""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from source_runner import SourceRunner

log = logging.getLogger("source-runner")

app = FastAPI(title="Fathom Source Runner")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
_runner: SourceRunner | None = None


@app.on_event("startup")
async def startup():
    global _runner
    _runner = SourceRunner(
        delta_url=os.environ.get("DELTA_STORE_URL", "http://localhost:4246"),
        delta_key=os.environ.get("DELTA_API_KEY", ""),
        sources_path=os.path.join(DATA_DIR, "sources.json"),
        state_dir=os.path.join(DATA_DIR, "source-state"),
    )
    asyncio.create_task(_runner.run(), name="source-runner")
    log.info("Source runner started")


# ── Models ───────────────────────────────────────────────────────────────


class CreateSourceRequest(BaseModel):
    source_type: str
    config: dict
    name: str = ""
    interval_minutes: int = 30
    expiry_days: float | None = 30


class UpdateSourceRequest(BaseModel):
    config: dict | None = None
    interval_minutes: int | None = None
    expiry_days: float | None = None


# ── Endpoints ────────────────────────────────────────────────────────────


@app.get("/api/sources")
async def list_sources():
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    return {"sources": _runner.list_sources()}


@app.get("/api/sources/types")
async def list_source_types():
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    return {"types": _runner.list_available_types()}


@app.post("/api/sources")
async def create_source(req: CreateSourceRequest):
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    try:
        sc = _runner.add_source(
            req.source_type,
            req.config,
            name=req.name,
            interval_minutes=req.interval_minutes,
            expiry_days=req.expiry_days,
        )
        return {"id": sc.id, "created": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/sources/{source_id}")
async def get_source(source_id: str):
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    src = _runner.get_source(source_id)
    if not src:
        raise HTTPException(404, f"Source not found: {source_id}")
    return src


@app.put("/api/sources/{source_id}")
async def update_source(source_id: str, req: UpdateSourceRequest):
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    updates = {}
    if req.config is not None:
        updates["config"] = req.config
    if req.interval_minutes is not None:
        updates["interval_minutes"] = req.interval_minutes
    if req.expiry_days is not None:
        updates["expiry_days"] = req.expiry_days
    try:
        _runner.update_source(source_id, updates)
        return {"updated": True}
    except KeyError as e:
        raise HTTPException(404, f"Source not found: {source_id}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/sources/{source_id}/pause")
async def pause_source(source_id: str):
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    _runner.pause_source(source_id)
    return {"paused": True}


@app.post("/api/sources/{source_id}/resume")
async def resume_source(source_id: str):
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    _runner.resume_source(source_id)
    return {"paused": False}


@app.post("/api/sources/{source_id}/poll")
async def poll_source(source_id: str):
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    try:
        await _runner.manual_poll(source_id)
        return {"triggered": True}
    except KeyError as e:
        raise HTTPException(404, f"Source not found: {source_id}") from e


@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: str):
    if _runner is None:
        raise HTTPException(503, "Source runner not initialized")
    _runner.remove_source(source_id)
    return {"deleted": True}


@app.get("/health")
async def health():
    sources = len(_runner._sources) if _runner else 0
    return {"status": "ok", "sources": sources}
