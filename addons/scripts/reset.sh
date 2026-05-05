#!/usr/bin/env bash
# reset — wipe the lake and start fresh.
#
# Destroys:
#   - The postgres volume (${COMPOSE_PROJECT_NAME:-fathom}-pg)
#   - All files under ${LAKE_DIR}/{api,deltas,backups,source-runner}
#
# Keeps:
#   - .env  (your API keys survive)
#   - The checkout itself
#   - Any backup you choose to save (written to ~/fathom-backups/)
#
# Usage:
#   ./addons/scripts/reset.sh          # interactive, confirms before destroying
#   ./addons/scripts/reset.sh --yes    # skip confirmation (CI / scripted)
#   ./addons/scripts/reset.sh --backup # force backup without prompting
#   ./addons/scripts/reset.sh --no-backup  # skip backup offer

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_DIR}"

# ── output helpers ───────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
  C_BLU=$'\033[34m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_DIM=""; C_RST=""
fi

ok()   { printf "  %s✓%s %s\n" "${C_GRN}" "${C_RST}" "$*"; }
info() { printf "  %s•%s %s\n" "${C_BLU}" "${C_RST}" "$*"; }
warn() { printf "  %s!%s %s\n" "${C_YLW}" "${C_RST}" "$*"; }
fail() { printf "  %s✗%s %s\n" "${C_RED}" "${C_RST}" "$*" >&2; }
step() { printf "\n%s%s%s\n" "${C_BLU}" "$*" "${C_RST}"; }

die() {
  fail "$1"
  [[ $# -ge 2 ]] && printf "    %s%s%s\n" "${C_DIM}" "$2" "${C_RST}" >&2
  exit 1
}

# ── .env helpers ─────────────────────────────────────────────────────
get_env() {
  local key="$1"
  [[ -f .env ]] || { printf ""; return; }
  grep -E "^${key}=" .env | head -1 \
    | sed -E "s/^${key}=//" \
    | sed -E 's/^"(.*)"$/\1/' \
    | sed -E "s/^'(.*)'\$/\1/"
}

# ── compose helper ───────────────────────────────────────────────────
compose_cmd() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    podman compose "$@"
  else
    die "Neither 'docker compose' nor 'podman compose' found."
  fi
}

volume_exists() {
  local vol="$1"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker volume ls --format '{{.Name}}' 2>/dev/null | grep -qx "${vol}"
  else
    podman volume ls --format '{{.Name}}' 2>/dev/null | grep -qx "${vol}"
  fi
}

# ── args ─────────────────────────────────────────────────────────────
YES=0
BACKUP_FLAG=""   # "" = ask interactively, "yes" = force, "no" = skip
for arg in "$@"; do
  case "${arg}" in
    --yes|-y)        YES=1 ;;
    --backup)        BACKUP_FLAG="yes" ;;
    --no-backup)     BACKUP_FLAG="no" ;;
    --help|-h)
      printf "Usage: %s [--yes] [--backup|--no-backup]\n" "$(basename "$0")"
      exit 0 ;;
    *) die "Unknown argument: ${arg}" "Usage: $(basename "$0") [--yes] [--backup|--no-backup]" ;;
  esac
done

# ── resolve lake dir and project name ────────────────────────────────
lake="$(get_env LAKE_DIR)"
lake="${lake:-${HOME}/.fathom/mind}"
lake="${lake/#\~/${HOME}}"

project="$(get_env COMPOSE_PROJECT_NAME)"
project="${project:-fathom}"
volume="${project}-pg"

# ── show what will be destroyed ──────────────────────────────────────
printf "%sFathom reset%s — %s\n" "${C_RED}" "${C_RST}" "${REPO_DIR}"
printf "\n  This will permanently destroy:\n\n"
printf "    %s  postgres volume%s  %s%s%s\n" "${C_RED}" "${C_RST}" "${C_DIM}" "${volume}" "${C_RST}"
for sub in api deltas backups source-runner; do
  printf "    %s  lake dir%s         %s%s/%s%s\n" \
    "${C_RED}" "${C_RST}" "${C_DIM}" "${lake}" "${sub}" "${C_RST}"
done
printf "\n  .env is kept — your API keys survive.\n\n"

if [[ ${YES} -eq 0 ]]; then
  printf "  %sType 'reset' to confirm, anything else to abort:%s " "${C_YLW}" "${C_RST}"
  read -r answer
  if [[ "${answer}" != "reset" ]]; then
    printf "\n  Aborted.\n\n"
    exit 0
  fi
fi

# ── offer backup if the pg volume exists ─────────────────────────────
if volume_exists "${volume}"; then
  do_backup=0

  if [[ "${BACKUP_FLAG}" == "yes" ]]; then
    do_backup=1
  elif [[ "${BACKUP_FLAG}" == "no" ]]; then
    do_backup=0
  else
    printf "\n  %sYour lake has data (volume: %s).%s\n" "${C_YLW}" "${volume}" "${C_RST}"
    printf "  Back it up before wiping? [Y/n] "
    read -r bk_answer
    bk_answer="${bk_answer:-y}"
    [[ "${bk_answer}" =~ ^[Yy] ]] && do_backup=1
  fi

  if [[ ${do_backup} -eq 1 ]]; then
    step "Backing up lake"

    backup_dir="${HOME}/fathom-backups"
    mkdir -p "${backup_dir}"
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_file="${backup_dir}/${ts}-${project}-pre-reset.sql.gz"

    # Bring postgres up if it isn't already running.
    postgres_was_stopped=0
    if ! compose_cmd ps postgres 2>/dev/null | grep -q "Up\|running"; then
      info "Starting postgres for backup..."
      compose_cmd up -d postgres >/dev/null 2>&1

      # Wait up to 30 s for postgres to be healthy.
      for i in $(seq 1 30); do
        if compose_cmd exec -T postgres pg_isready -U fathom -d deltas >/dev/null 2>&1; then
          break
        fi
        sleep 1
      done
      postgres_was_stopped=1
    fi

    info "Dumping to ${backup_file} ..."
    compose_cmd exec -T postgres \
      pg_dump -U fathom -d deltas --no-password \
      | gzip > "${backup_file}"

    ok "Backup saved: ${backup_file}"

    # Also archive the media files if they exist.
    media_dir="${lake}/deltas/media"
    if [[ -d "${media_dir}" ]] && [[ -n "$(ls -A "${media_dir}" 2>/dev/null)" ]]; then
      media_archive="${backup_dir}/${ts}-${project}-media.tar.gz"
      tar -czf "${media_archive}" -C "${lake}/deltas" media
      ok "Media archived: ${media_archive}"
    fi

    [[ ${postgres_was_stopped} -eq 1 ]] && compose_cmd stop postgres >/dev/null 2>&1 || true
  fi
fi

# ── stop stack and remove volumes ────────────────────────────────────
step "Stopping stack and removing postgres volume"
compose_cmd down -v 2>/dev/null || warn "compose down -v returned non-zero — volume may not have existed, continuing"
ok "Stack stopped, volume removed"

# ── wipe lake dir subdirectories ─────────────────────────────────────
step "Wiping lake directory"
if [[ ! -d "${lake}" ]]; then
  info "Lake dir ${lake} does not exist — nothing to wipe"
else
  for sub in api deltas backups source-runner; do
    target="${lake}/${sub}"
    if [[ -d "${target}" ]]; then
      rm -rf "${target}"
      mkdir -p "${target}"
      ok "Cleared ${target}"
    else
      info "Skipping ${target} (not found)"
    fi
  done
fi

# ── done ─────────────────────────────────────────────────────────────
printf "\n%sDone.%s Fresh lake ready. Next:\n\n    docker compose up -d\n    open http://localhost:8201\n\n" \
  "${C_GRN}" "${C_RST}"
