"""Routine CRUD endpoints.

Routines are spec deltas in the lake with YAML frontmatter + prompt
body. Tagged `[spec, routine, routine-id:<id>]`. CRUD operations here
write new spec deltas with the same routine-id; scheduler + dashboard
take latest. See docs/routine-spec.md for the canonical field reference.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import auth
from .. import routines as routines_mod

router = APIRouter()


@router.get("/v1/routines")
async def list_routines_endpoint():
    return {"routines": await routines_mod.list_routines()}


@router.post("/v1/routines", dependencies=[Depends(auth.require_admin)])
async def create_routine_endpoint(body: dict):
    try:
        return await routines_mod.create(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.put("/v1/routines/{routine_id}", dependencies=[Depends(auth.require_admin)])
async def update_routine_endpoint(routine_id: str, body: dict):
    try:
        return await routines_mod.update(routine_id, body)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/v1/routines/{routine_id}", dependencies=[Depends(auth.require_admin)])
async def delete_routine_endpoint(routine_id: str):
    try:
        return await routines_mod.soft_delete(routine_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/v1/routines/{routine_id}/fire", dependencies=[Depends(auth.require_admin)])
async def fire_routine_endpoint(routine_id: str, body: dict | None = None):
    override = (body or {}).get("prompt") if body else None
    try:
        return await routines_mod.fire(routine_id, prompt_override=override)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/v1/routines/preview-schedule")
async def preview_schedule_endpoint(body: dict):
    schedule = (body.get("schedule") or "").strip()
    count = int(body.get("count") or 5)
    if not schedule:
        return {"fires": [], "error": "schedule required"}
    fires = routines_mod.preview_fires(schedule, count=count)
    if not fires:
        return {"fires": [], "error": "invalid cron"}
    return {"fires": fires}
