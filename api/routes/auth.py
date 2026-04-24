"""Auth, token, and pair-code endpoints.

Three clusters live here:
- /v1/auth/* — bootstrap-status, first-run bootstrap, /me identity read
- /v1/tokens, /v1/scopes — admin token management
- /v1/pair* — admin mints pair codes; agents redeem them for tokens

All share api/auth.py (middleware, scopes) and api/pairing.py
(pair-code storage + redemption).
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth as auth_mod
from .. import contacts as contacts_mod
from .. import delta_client, pairing

log = logging.getLogger(__name__)

router = APIRouter()


# ── Bootstrap (first-run onboarding) ─────────────
#
# A fresh install has no admin. The dashboard gates on
# bootstrap-status and redirects to /ui/onboarding.html when needed.
# The POST endpoint is one-shot — it creates the first admin contact,
# writes the admin profile delta, optionally adds an email handle, and
# mints a full-scope admin token. Subsequent POSTs fail 409.


class BootstrapBody(BaseModel):
    display_name: str
    slug: str | None = None
    profile: dict | None = None
    email: str | None = None


def _slugify(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "admin"


@router.get("/v1/auth/bootstrap-status")
async def bootstrap_status():
    """Return whether this instance still needs first-run onboarding.

    True when no active admin contact exists in the registry. The UI
    uses this to decide between onboarding.html and the dashboard on
    first boot."""
    slug = await contacts_mod.first_admin_slug()
    return {"needs_bootstrap": slug is None}


@router.post("/v1/auth/bootstrap", status_code=201)
async def bootstrap(body: BootstrapBody):
    """Create the first admin contact and mint its admin token. One-shot."""
    existing = await contacts_mod.first_admin_slug()
    if existing:
        raise HTTPException(409, f"Already bootstrapped (admin: {existing})")

    display_name = (body.display_name or "").strip()
    if not display_name:
        raise HTTPException(400, "display_name is required")

    slug = _slugify(body.slug or display_name)

    # Collision check (include disabled, since re-using a tombstoned slug
    # would silently merge into a prior contact's delta stream).
    existing_row = await delta_client.get_contact_row(slug, include_disabled=True)
    if existing_row:
        raise HTTPException(409, f"Slug '{slug}' already exists")

    initial_profile: dict = {"role": "admin", "display_name": display_name}
    if body.profile:
        for key in ("pronouns", "timezone", "language", "bio", "aliases", "avatar"):
            v = body.profile.get(key)
            if v is not None:
                initial_profile[key] = v

    contact = await contacts_mod.create(slug, initial_profile=initial_profile, actor_slug=None)

    if body.email:
        email = body.email.strip()
        if email:
            try:
                await delta_client.add_handle(slug, "email", email)
            except Exception:
                log.exception("bootstrap: add_handle(email) failed for %s", slug)

    token_result = auth_mod.create_token(
        name="Admin (bootstrap)",
        scopes=list(auth_mod.ALL_SCOPES.keys()),
        contact_slug=slug,
    )

    # Prime the first-admin cache so subsequent unauthed reads see the
    # new admin without waiting on cache expiry / module reload.
    contacts_mod.invalidate_first_admin_cache()
    auth_mod.invalidate_contact_cache(slug)

    # Re-fetch so handles (and any other derived fields) land on the
    # returned contact.
    hydrated = await contacts_mod.get(slug) or contact

    return {"token": token_result["token"], "contact": hydrated}


@router.get("/v1/auth/me")
async def auth_me(request: Request):
    """Return the current caller's contact + token shape.

    Used by the dashboard shell to know who's logged in and which role
    gates to apply. Never returns the raw token — that's one-time at
    mint.

    `auth_required` reports whether the server currently enforces auth
    (i.e. at least one token has been minted). The login page uses it
    to distinguish "first-run, sign-in optional" from "server is
    locked, redirect to login."
    """
    contact = getattr(request.state, "contact", None)
    token = getattr(request.state, "token", None)
    return {
        "authenticated": (contact is not None)
        and (token is not None or not auth_mod.auth_required()),
        "auth_required": auth_mod.auth_required(),
        "contact": contact,
        "token": {
            "id": (token or {}).get("id"),
            "name": (token or {}).get("name"),
            "scopes": (token or {}).get("scopes"),
        }
        if token
        else None,
    }


# ── Token management ─────────────────────────────


class TokenCreate(BaseModel):
    name: str = ""
    scopes: list[str] | None = None
    contact_slug: str | None = None


@router.post("/v1/tokens", dependencies=[Depends(auth_mod.require_admin)])
async def create_token(req: TokenCreate, request: Request):
    # Default to the caller's own contact. Admins can mint for others by
    # passing contact_slug explicitly. require_admin guarantees a caller
    # contact, so the fallback is purely defensive.
    caller = getattr(request.state, "contact", None)
    default_slug = (caller or {}).get("slug", "")
    slug = req.contact_slug or default_slug
    if not slug:
        raise HTTPException(400, "contact_slug required")
    return auth_mod.create_token(req.name, req.scopes, contact_slug=slug)


@router.get("/v1/scopes")
async def list_scopes():
    return auth_mod.get_scopes()


@router.get("/v1/tokens", dependencies=[Depends(auth_mod.require_admin)])
async def list_tokens():
    return auth_mod.list_tokens()


@router.delete("/v1/tokens/{token_id}", dependencies=[Depends(auth_mod.require_admin)])
async def delete_token(token_id: str):
    deleted = auth_mod.delete_token(token_id)
    if not deleted:
        raise HTTPException(404, "Token not found")
    return {"deleted": True}


# ── Pair-code onboarding ─────────────────────────
#
# POST /v1/pair         → mint a short-lived single-use admission code
# GET  /v1/pair         → list currently-active (unredeemed, unexpired) codes
# POST /v1/pair/redeem  → exchange a code for a real API token (public)
#
# See api/pairing.py for the flow and rationale. The redeem endpoint is in
# PUBLIC_PATHS because the agent has no token yet when it calls it.


class PairCreate(BaseModel):
    note: str = ""
    ttl_seconds: int = 600
    contact_slug: str | None = None


@router.post("/v1/pair", dependencies=[Depends(auth_mod.require_admin)])
async def pair_create(body: PairCreate, request: Request):
    caller = getattr(request.state, "contact", None)
    default_slug = (caller or {}).get("slug", "")
    slug = body.contact_slug or default_slug
    if not slug:
        raise HTTPException(400, "contact_slug required")
    return pairing.create_pair_code(
        ttl_seconds=body.ttl_seconds,
        note=body.note,
        contact_slug=slug,
    )


@router.get("/v1/pair", dependencies=[Depends(auth_mod.require_admin)])
async def pair_list():
    return {"codes": pairing.list_active_codes()}


class PairRedeem(BaseModel):
    code: str
    host: str = ""


@router.post("/v1/pair/redeem")
async def pair_redeem(body: PairRedeem):
    try:
        return pairing.redeem_pair_code(body.code, body.host)
    except ValueError as e:
        reason = str(e)
        # Map reason → HTTP status so the agent can show a useful message
        status = {
            "unknown_code": 404,
            "already_redeemed": 410,
            "expired": 410,
        }.get(reason, 400)
        raise HTTPException(status, detail=reason) from e
