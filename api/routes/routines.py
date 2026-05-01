"""Routine CRUD endpoints.

Routines are spec deltas in the lake with YAML frontmatter + prompt
body. Tagged `[spec, routine, routine-id:<id>]`. CRUD operations here
write new spec deltas with the same routine-id; scheduler + dashboard
take latest. See docs/routine-spec.md for the canonical field reference.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, delta_client
from .. import routines as routines_mod

router = APIRouter()


_REWRITE_PROMPT = """You are rewriting a Fathom routine prompt into the standard four-section schema.

The schema is:

  # Purpose
  [One sentence — what should Fathom accomplish on this routine?]

  # Needs
  [What this needs to actually run — claude-code on a host name (e.g.
  "claude-code on myras-fedora-laptop"), a specific tool, or "substrate
  only" if the lake already has the data.]

  # Steps
  [The instructions — what to look for, what to filter, what to compare.
  Numbered list or short prose. Written as a request to Fathom, NOT as
  instructions for claude-code (no "you are claude-code; run curl X").]

  # Ending
  [How the user wants to be notified. Plain language. The witness reads
  this as a route directive: "send me a card" → feed-card; "DM me" →
  chat-reply; "stay silent unless X" → silent then alert when X. Read
  the original prompt for clues — does it mention thresholds, alerts,
  daily summaries? Translate those into Ending prose.]

Original prompt body:

---
{original}
---

Routine context:
  · name: {name}
  · host pin: {host}
  · schedule: {schedule}

Rewrite the prompt body using the four sections. Keep the user's intent
intact — don't add or remove tasks. Translate any "synthesize / write /
report" instructions into the # Ending section. If the original prompt
already follows the schema, return it unchanged.

Return ONLY the rewritten prompt body. No commentary, no markdown
fences. Start with "# Purpose".
"""


@router.post(
    "/v1/routines/{routine_id}/rewrite-to-schema",
    dependencies=[Depends(auth.require_admin)],
)
async def rewrite_to_schema(routine_id: str):
    """Use an LLM to rewrite this routine's prompt into the four-section
    schema, then emit a `tool:routines` proposal card so the user can
    review (Edit/Deny/Approve) before any change lands.

    No-op if the routine already has all four headers. The proposal-
    card flow is deliberate — the rewrite is best-effort, the user
    should always see what's about to change before it ships.
    """
    spec = await routines_mod.get_latest_spec(routine_id)
    if not spec or spec["meta"].get("deleted"):
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")

    meta = spec["meta"]
    original = (spec["body"] or "").strip()
    name = meta.get("name") or routine_id
    host = meta.get("host") or "(fleet-wide)"
    schedule = meta.get("schedule") or "(none)"

    has_all_sections = all(
        s in original
        for s in ("# Purpose", "# Needs", "# Steps", "# Ending")
    )
    if has_all_sections:
        return {
            "skipped": True,
            "reason": "already-in-schema",
            "routine_id": routine_id,
        }

    from ..loop.llm import loop_generate

    prompt = _REWRITE_PROMPT.format(
        original=original or "(empty)",
        name=name,
        host=host,
        schedule=schedule,
    )
    try:
        rewritten = await loop_generate(
            prompt=prompt,
            tier="medium",
            max_tokens=2048,
            temperature=0.4,
        )
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"LLM rewrite failed: {e}"
        ) from e

    rewritten = (rewritten or "").strip()
    if not rewritten or "# Purpose" not in rewritten:
        raise HTTPException(
            status_code=502,
            detail="LLM rewrite did not produce a valid four-section body",
        )

    # Build the proposal card. Args mirror the OpenAI tool-schema for
    # routines.update so /v1/proposals/<id>/approve calls the same path
    # routines_mod.update would. Only `id` and `prompt` change; everything
    # else stays whatever the user already had.
    proposal_args = {
        "action": "update",
        "id": routine_id,
        "prompt": rewritten,
    }
    payload = {
        "kicker": "Rewrite",
        "title": f"Rewrite {name} to schema",
        "body": (
            f"Drafted a four-section version of this routine's prompt. Edit "
            f"any field, then approve to save it back. Original prompt is "
            f"preserved in lake history."
        ),
        "tail": "",
        "body_image": "",
        "link": "",
        "links": [],
        "route": "tool:routines",
        "axes": {},
        "tool": "routines",
        "tool_args": proposal_args,
    }
    tags = [
        "feed-card",
        "kind:proposal",
        "proposal-status:pending",
        "tool:routines",
        "action:update",
        "route:tool:routines",
        f"routine-id:{routine_id}",
        "rewrite-to-schema",
    ]
    written = await delta_client.write(
        content=json.dumps(payload, ensure_ascii=False),
        tags=tags,
        source="routines-migration",
    )
    return {
        "proposed": True,
        "routine_id": routine_id,
        "proposal_delta_id": written.get("id") if isinstance(written, dict) else None,
    }


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
    """Fire a routine on demand.

    Default: fires INTO the River — writes a `routine-due` intent into
    the puddle alongside a `routine-tick` marker in the lake. The witness
    deliberates and routes (claude-code dispatch, feed-card, alert,
    chat-reply, silent). Same shape as a cron-driven fire; just earlier.

    Pass `{"via": "direct"}` to force the legacy direct-to-kitty path —
    writes a `routine-fire` delta the kitty plugin consumes immediately,
    bypassing the witness. Useful for "run this RIGHT NOW with no River
    deliberation" semantics, but the default behavior matches what cron
    would do at the routine's next-fire time.
    """
    body = body or {}
    via = (body.get("via") or "river").strip()
    override = body.get("prompt")

    if via == "direct":
        try:
            return await routines_mod.fire(routine_id, prompt_override=override)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    # River path — mirror what routine_scheduler does on cron tick.
    spec = await routines_mod.get_latest_spec(routine_id)
    if not spec or spec["meta"].get("deleted"):
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")
    body_text = override if override is not None else spec["body"]

    from .. import routine_scheduler

    await routine_scheduler._fire_into_river(
        routine_id, spec["meta"], body_text or ""
    )
    return {
        "fired": True,
        "via": "river",
        "routine_id": routine_id,
    }


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
