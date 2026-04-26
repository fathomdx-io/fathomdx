"""Local-admin CLI for the api container.

When the web UI is out of reach — usually "I've lost my only admin
key" — an operator can shell into the api container and call this
module to list contacts, list tokens, or mint a fresh key bound to
whichever contact they name. It bypasses the web auth gate because
the operator already has host-level access to the container, which
is the same trust boundary that protects tokens.json.

    python -m api.cli list-contacts
    python -m api.cli list-keys
    python -m api.cli mint-key                     # interactive: pick from list
    python -m api.cli mint-key --contact myra       # non-interactive
    python -m api.cli mint-key --contact myra --name "Recovery key"

Meant to be invoked via `addons/scripts/mint-key.sh` on the host,
which wraps `docker compose exec api python -m api.cli …`. The
wrapper is the operator-facing entrypoint; this module is just the
implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import auth as auth_mod
from . import contacts as contacts_mod


def _fail(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


async def _all_contacts(include_disabled: bool) -> list[dict]:
    return await contacts_mod.list_all(include_disabled=include_disabled)


async def cmd_list_contacts(args: argparse.Namespace) -> int:
    rows = await _all_contacts(include_disabled=args.include_disabled)
    if not rows:
        print("(no contacts — run /v1/auth/bootstrap or the onboarding page first)")
        return 0
    # Tab-separated so the output pipes cleanly into awk/cut.
    print("slug\trole\tdisplay_name")
    for row in rows:
        slug = row.get("slug", "")
        role = row.get("role", "")
        name = row.get("display_name", "")
        print(f"{slug}\t{role}\t{name}")
    return 0


async def cmd_list_keys(args: argparse.Namespace) -> int:
    tokens = auth_mod.list_tokens()
    if not tokens:
        print("(no tokens in /data/tokens.json)")
        return 0
    print("id\tcontact_slug\tname\tscopes")
    for t in tokens:
        print(
            f"{t.get('id', '')}\t{t.get('contact_slug', '')}\t"
            f"{t.get('name', '')}\t{','.join(t.get('scopes') or [])}"
        )
    return 0


async def _pick_contact_interactively(rows: list[dict]) -> str:
    """Print a numbered menu and read a selection on stdin."""
    print("Contacts on this instance:\n")
    for i, row in enumerate(rows, 1):
        slug = row.get("slug", "")
        role = row.get("role", "")
        name = row.get("display_name", "")
        print(f"  {i:>2}. {slug:<20} [{role}]  {name}")
    print()
    while True:
        raw = input("Mint for which contact (number or slug)? ").strip()
        if not raw:
            continue
        # Number?
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(rows):
                return rows[idx - 1]["slug"]
            print(f"  out of range — pick 1..{len(rows)}")
            continue
        # Slug?
        for row in rows:
            if row.get("slug") == raw:
                return raw
        print(f"  no contact with slug '{raw}' — try again")


async def cmd_mint_key(args: argparse.Namespace) -> int:
    rows = await _all_contacts(include_disabled=False)
    if not rows:
        _fail(
            "No active contacts. Run bootstrap first (POST /v1/auth/bootstrap "
            "or open /ui/onboarding.html)."
        )

    slug = args.contact or ""
    if not slug:
        if not sys.stdin.isatty():
            _fail(
                "stdin isn't a tty — pass --contact <slug> explicitly. "
                "Available slugs: " + ", ".join(r.get("slug", "") for r in rows)
            )
        slug = await _pick_contact_interactively(rows)

    if not any(row.get("slug") == slug for row in rows):
        _fail(
            f"No active contact with slug '{slug}'. Available: "
            + ", ".join(r.get("slug", "") for r in rows)
        )

    # Figure out a sensible default token name based on the contact's
    # role — admin keys and member keys get different labels so list-keys
    # stays readable.
    contact = next(row for row in rows if row.get("slug") == slug)
    role = contact.get("role", "member")
    default_name = (
        "Admin (recovery)" if role == "admin" else f"Member ({contact.get('display_name', slug)})"
    )
    name = args.name or default_name

    # Scope picking: admin contacts get full scopes; members get the
    # same subset the web register flow mints. Operator can override
    # with --scopes but that's a power-user knob.
    if args.scopes:
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    else:
        scopes = (
            list(auth_mod.ALL_SCOPES.keys())
            if role == "admin"
            else ["lake:read", "lake:write", "chat"]
        )

    result = auth_mod.create_token(name=name, scopes=scopes, contact_slug=slug)

    # Raw token prints on its own line so it's copy-paste friendly. The
    # trailing summary goes to stderr so `mint-key | read TOKEN` works.
    print(result["token"])
    print(
        f"\nMinted for contact '{slug}' (role: {role}).",
        f"Token id: {result['id']} · scopes: {', '.join(result['scopes'])}",
        file=sys.stderr,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m api.cli",
        description="Fathom api local-admin commands (mint keys, list contacts/tokens).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list-contacts", help="List active contacts on this instance.")
    sp.add_argument("--include-disabled", action="store_true", help="Include tombstoned contacts.")
    sp.set_defaults(func=cmd_list_contacts)

    sp = sub.add_parser("list-keys", help="List token metadata (no raw tokens).")
    sp.set_defaults(func=cmd_list_keys)

    sp = sub.add_parser("mint-key", help="Mint a new API key bound to a contact.")
    sp.add_argument(
        "--contact", help="Slug of the contact to bind the token to. Interactive if omitted."
    )
    sp.add_argument(
        "--name", help="Human-readable name for the token (shown in Settings → API Keys)."
    )
    sp.add_argument(
        "--scopes",
        help="Comma-separated scope list. Defaults to full for admin, limited for member.",
    )
    sp.set_defaults(func=cmd_mint_key)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
