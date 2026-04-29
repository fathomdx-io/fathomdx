#!/usr/bin/env bash
# mint-key — recover or create a Fathom API key from the host.
#
# For the "I've lost my only admin key" case. Runs inside the api
# container via `docker compose exec`, so it bypasses the web auth
# gate — the trust boundary is host-level access to this directory.
#
# Usage:
#   addons/scripts/mint-key.sh                    # interactive: pick a contact
#   addons/scripts/mint-key.sh --contact admin    # non-interactive
#   addons/scripts/mint-key.sh list-contacts      # just show who's here
#   addons/scripts/mint-key.sh list-keys          # existing tokens (no raw values)
#
# The raw token prints on stdout; metadata (id, scopes) prints on
# stderr. That lets you pipe:
#
#     KEY=$(addons/scripts/mint-key.sh --contact admin 2>/dev/null)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "${REPO_DIR}"

# Pick whichever compose driver the operator actually has. Both expose
# the same `compose` subcommand surface this script needs, but a
# Fedora install often ships podman without docker, and a Mac install
# usually has docker without podman. Letting the script work with
# either keeps "lost my admin key" recovery from being blocked by a
# tooling preference.
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
  COMPOSE=(podman compose)
else
  echo "error: neither 'docker compose' nor 'podman compose' is available on PATH." >&2
  echo "Install one and try again." >&2
  exit 1
fi

# Sanity: the api service has to be running for `exec` to reach it.
# A stopped stack gives a cryptic "service not running" — upgrade that
# into a friendly nudge.
#
# `compose ps --status --services` shape differs between docker compose
# and podman-compose, so check at the engine level: list running
# containers and look for one whose name matches the compose-generated
# api container (`<project>_api_1` for podman, `<project>-api-1` for
# docker). Either separator counts.
ENGINE="${COMPOSE[0]}"
if ! "${ENGINE}" ps --filter status=running --format '{{.Names}}' 2>/dev/null | grep -qE '(_|-)api(_|-)1$'; then
  echo "error: the api service isn't running. Start the stack first:" >&2
  echo "    ${COMPOSE[*]} up -d" >&2
  exit 1
fi

# Subcommand convenience: allow `mint-key.sh list-contacts` / `list-keys`
# without the operator having to type `python -m api.cli` incantations.
# Default to `mint-key` when the first arg doesn't look like a known
# subcommand.
case "${1:-}" in
  list-contacts|list-keys|mint-key)
    CMD="$1"
    shift
    ;;
  -*|"")
    # No subcommand, or a flag — assume mint-key and pass everything through.
    CMD="mint-key"
    ;;
  *)
    CMD="mint-key"
    ;;
esac

# -T disables pseudo-tty allocation so the script works under CI /
# non-interactive shells. We pass -i (stdin) so the interactive prompt
# in cmd_mint_key works when called from a real terminal.
if [ -t 0 ]; then
  exec "${COMPOSE[@]}" exec api python -m api.cli "${CMD}" "$@"
else
  exec "${COMPOSE[@]}" exec -T api python -m api.cli "${CMD}" "$@"
fi
