"""Contacts registry and self-profile endpoints.

Three clusters live here:
- /v1/contacts/* — admin CRUD over the contacts registry (incl. handles)
- /v1/contact-proposals/* — propose-then-confirm flow for new contacts
- /v1/me/profile — the caller's own soft-field edits

Thin HTTP layer over api/contacts.py. That module owns the merge
between the delta-store registry row and the latest profile delta,
and is the only writer of profile deltas. Per docs/contact-spec.md.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth
from .. import contacts as contacts_mod
from .. import delta_client

router = APIRouter()


class ContactCreate(BaseModel):
    slug: str
    display_name: str | None = None
    role: str = "member"
    pronouns: str | None = None
    timezone: str | None = None
    language: str | None = None
    bio: str | None = None
    avatar: str | None = None
    aliases: list[str] | None = None


class ContactUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    pronouns: str | None = None
    timezone: str | None = None
    language: str | None = None
    bio: str | None = None
    avatar: str | None = None
    aliases: list[str] | None = None


class SelfProfileUpdate(BaseModel):
    """Soft fields a contact can edit about themselves. Deliberately
    does NOT include `role` — FastAPI would drop unknown fields from the
    body, and even if the client tries to include role it never reaches
    the update call."""
    display_name: str | None = None
    pronouns: str | None = None
    timezone: str | None = None
    language: str | None = None
    bio: str | None = None
    avatar: str | None = None
    aliases: list[str] | None = None


class HandleBody(BaseModel):
    channel: str
    identifier: str


class ProposeContactIn(BaseModel):
    candidate_slug: str | None = None
    display_name: str
    rationale: str
    source_context: dict | None = None


class AcceptProposalIn(BaseModel):
    slug: str
    display_name: str
    role: str = "member"
    pronouns: str | None = None
    timezone: str | None = None
    language: str | None = None
    bio: str | None = None
    aliases: list[str] | None = None


class RejectProposalIn(BaseModel):
    note: str = ""


def _caller_slug(request: Request) -> str | None:
    contact = getattr(request.state, "contact", None)
    return (contact or {}).get("slug")


@router.get("/v1/contacts", dependencies=[Depends(auth.require_admin)])
async def list_contacts(include_disabled: bool = False):
    return await contacts_mod.list_all(include_disabled=include_disabled)


@router.post("/v1/contacts", dependencies=[Depends(auth.require_admin)])
async def create_contact(req: ContactCreate, request: Request):
    actor = _caller_slug(request)
    payload = req.model_dump(exclude_unset=True)
    slug = payload.pop("slug")
    try:
        created = await contacts_mod.create(
            slug=slug, initial_profile=payload, actor_slug=actor
        )
    except httpx.HTTPStatusError as e:
        detail = "Contact already exists" if e.response.status_code == 409 else str(e)
        raise HTTPException(status_code=e.response.status_code, detail=detail) from e
    auth.invalidate_contact_cache(slug)
    return created


@router.get("/v1/contacts/{slug}", dependencies=[Depends(auth.require_admin)])
async def get_contact(slug: str):
    contact = await contacts_mod.get(slug)
    if not contact:
        raise HTTPException(404, "Contact not found")
    return contact


@router.patch("/v1/contacts/{slug}", dependencies=[Depends(auth.require_admin)])
async def update_contact(slug: str, req: ContactUpdate, request: Request):
    actor = _caller_slug(request)
    fields = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if not fields:
        existing = await contacts_mod.get(slug)
        if not existing:
            raise HTTPException(404, "Contact not found")
        return existing
    try:
        updated = await contacts_mod.update_profile(
            slug, fields, actor_slug=actor, event="updated"
        )
    except contacts_mod.LastAdminError as e:
        raise HTTPException(409, detail=str(e)) from e
    if not updated:
        raise HTTPException(404, "Contact not found")
    auth.invalidate_contact_cache(slug)
    return updated


@router.delete("/v1/contacts/{slug}", dependencies=[Depends(auth.require_admin)])
async def delete_contact(slug: str, request: Request):
    actor = _caller_slug(request)
    try:
        ok = await contacts_mod.disable(slug, actor_slug=actor)
    except contacts_mod.LastAdminError as e:
        raise HTTPException(409, detail=str(e)) from e
    if not ok:
        raise HTTPException(404, "Contact not found")
    auth.invalidate_contact_cache(slug)
    return {"disabled": slug}


@router.get("/v1/contacts/{slug}/handles", dependencies=[Depends(auth.require_admin)])
async def list_contact_handles(slug: str):
    c = await delta_client._get()
    r = await c.get(f"/contacts/{slug}/handles")
    if r.status_code == 404:
        raise HTTPException(404, "Contact not found")
    r.raise_for_status()
    return r.json()


@router.post("/v1/contacts/{slug}/handles", dependencies=[Depends(auth.require_admin)])
async def add_contact_handle(slug: str, body: HandleBody):
    try:
        handle = await delta_client.add_handle(slug, body.channel, body.identifier)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=e.response.json().get("detail", str(e)),
        ) from e
    auth.invalidate_contact_cache(slug)
    return handle


@router.delete("/v1/contacts/{slug}/handles", dependencies=[Depends(auth.require_admin)])
async def remove_contact_handle(slug: str, body: HandleBody):
    try:
        await delta_client.remove_handle(slug, body.channel, body.identifier)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=e.response.json().get("detail", str(e)),
        ) from e
    auth.invalidate_contact_cache(slug)
    return {"deleted": {"channel": body.channel, "identifier": body.identifier}}


# ── Contact proposals (propose-then-confirm) ──────
#
# Any authenticated caller can propose — Fathom in its chat loop,
# a plugin, a bridge, a teammate. Admin accepts or rejects. The
# proposal is sediment either way.


@router.get("/v1/contact-proposals")
async def list_contact_proposals(limit: int = 50):
    """Open (unresolved) proposals — visible to any authed caller so
    Fathom can avoid proposing the same person twice."""
    return await contacts_mod.list_proposals(limit=limit)


@router.post("/v1/contact-proposals")
async def propose_contact(body: ProposeContactIn, request: Request):
    """Low-privilege propose endpoint. The proposer's contact slug is
    stamped on the delta so admins can see who noticed."""
    proposer = _caller_slug(request)
    return await contacts_mod.propose(
        candidate_slug=body.candidate_slug,
        display_name=body.display_name,
        rationale=body.rationale,
        source_context=body.source_context,
        proposer_slug=proposer,
    )


@router.post(
    "/v1/contact-proposals/{proposal_id}/accept",
    dependencies=[Depends(auth.require_admin)],
)
async def accept_contact_proposal(
    proposal_id: str, body: AcceptProposalIn, request: Request
):
    actor = _caller_slug(request)
    extras = {
        k: v for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None and k not in ("slug", "display_name", "role")
    }
    try:
        created = await contacts_mod.accept_proposal(
            proposal_id=proposal_id,
            slug=body.slug,
            display_name=body.display_name,
            role=body.role,
            extra_fields=extras,
            actor_slug=actor,
        )
    except httpx.HTTPStatusError as e:
        detail = "Slug already exists" if e.response.status_code == 409 else str(e)
        raise HTTPException(status_code=e.response.status_code, detail=detail) from e
    auth.invalidate_contact_cache(body.slug)
    return created


@router.post(
    "/v1/contact-proposals/{proposal_id}/reject",
    dependencies=[Depends(auth.require_admin)],
)
async def reject_contact_proposal(
    proposal_id: str, body: RejectProposalIn, request: Request
):
    await contacts_mod.reject_proposal(
        proposal_id,
        actor_slug=_caller_slug(request),
        note=body.note,
    )
    return {"rejected": proposal_id}


# ── Self profile (named endpoint for self-edits) ───


@router.get("/v1/me/profile")
async def get_my_profile(request: Request):
    contact = getattr(request.state, "contact", None)
    if not contact:
        raise HTTPException(401, "Authentication required")
    slug = contact.get("slug")
    fresh = await contacts_mod.get(slug)
    if not fresh:
        raise HTTPException(404, "Profile not found")
    return fresh


@router.patch("/v1/me/profile")
async def update_my_profile(req: SelfProfileUpdate, request: Request):
    """Self-edit for soft profile fields. Role stays admin-only by
    virtue of not being on the Pydantic model."""
    contact = getattr(request.state, "contact", None)
    if not contact:
        raise HTTPException(401, "Authentication required")
    slug = contact.get("slug")

    fields = {
        k: v
        for k, v in req.model_dump(exclude_unset=True).items()
        if v is not None
    }
    if not fields:
        existing = await contacts_mod.get(slug)
        if not existing:
            raise HTTPException(404, "Profile not found")
        return existing

    updated = await contacts_mod.update_profile(
        slug, fields, actor_slug=slug, event="self-edited"
    )
    if not updated:
        raise HTTPException(404, "Profile not found")
    auth.invalidate_contact_cache(slug)
    return updated
