#!/usr/bin/env bash
# install — one-shot Fathom installer.
#
# Usage:
#   curl -fsSL https://fathomdx.io/install.sh | bash
#
# Or, equivalently:
#   wget -qO- https://fathomdx.io/install.sh | bash
#
# (Use `bash`, not `sh` — on Debian/Ubuntu `sh` is dash, which doesn't
# support `pipefail` or `[[`. The guard below catches this and prints a
# friendly message rather than the cryptic `Illegal option -o pipefail`.)
#
# Clones fathomdx into ~/.fathom/src (or wherever you tell it), runs preflight,
# and optionally starts the stack. Idempotent — re-running updates an
# existing install via `git pull` and re-runs preflight.
#
# Environment overrides (set before piping):
#   FATHOM_DIR    install location (default: $HOME/.fathom/src)
#   FATHOM_REPO   git URL (default: https://github.com/fathomdx-io/fathomdx.git)
#   FATHOM_REF    branch/tag/sha (default: main)
#   NONINTERACTIVE=1   skip all prompts, accept all defaults
#   FATHOM_AUTOSTART=1 in non-interactive mode, also start the stack
#                      (interactive runs ask before starting)

# POSIX-only prelude: if we're not in bash, print a clear hint and bail.
# Everything BELOW this guard is allowed to use bash features.
# Keep this block POSIX-compatible — no [[, no ${var:-default} chains, etc.
if [ -z "${BASH_VERSION:-}" ]; then
  printf '%s\n' \
    'Fathom installer requires bash, not POSIX sh.' \
    '' \
    'On Debian/Ubuntu, /bin/sh is dash and does not support `pipefail`.' \
    'Re-run the install with bash:' \
    '' \
    '    curl -fsSL https://fathomdx.io/install.sh | bash' \
    '' >&2
  exit 1
fi

set -euo pipefail

# Anchor CWD to a real directory before doing anything filesystem-y.
# When `curl … | bash` inherits a working directory that no longer
# resolves (deleted dir, broken automount, stale tmux pane), git
# refuses to clone with `fatal: Unable to read current working
# directory`. Re-anchor to $HOME (or /tmp, or /) so downstream ops
# always have a stable parent.
if ! cd "${HOME}" 2>/dev/null; then
  cd /tmp 2>/dev/null || cd /
fi

# ── output helpers ───────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
  C_BLU=$'\033[34m'; C_DIM=$'\033[2m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_DIM=""; C_BLD=""; C_RST=""
fi

ok()    { printf "  %s✓%s %s\n" "${C_GRN}" "${C_RST}" "$*"; }
info()  { printf "  %s•%s %s\n" "${C_BLU}" "${C_RST}" "$*"; }
warn()  { printf "  %s!%s %s\n" "${C_YLW}" "${C_RST}" "$*"; }
fail()  { printf "  %s✗%s %s\n" "${C_RED}" "${C_RST}" "$*" >&2; }
step()  { printf "\n%s%s%s\n" "${C_BLU}" "$*" "${C_RST}"; }
die()   { fail "$1"; [[ $# -ge 2 ]] && printf "    %s%s%s\n" "${C_DIM}" "$2" "${C_RST}" >&2; exit 1; }

# When run as `curl ... | sh`, stdin is the script body — not a TTY.
# Read user input from /dev/tty instead, when one exists.
INTERACTIVE=0
if [[ -z "${NONINTERACTIVE:-}" && -r /dev/tty ]]; then
  INTERACTIVE=1
fi

ask() {
  # ask <prompt> <default> → echoes the answer
  local prompt="$1" default="$2" answer=""
  if [[ ${INTERACTIVE} -eq 1 ]]; then
    printf "    %s [%s] " "${prompt}" "${default}" >/dev/tty
    read -r answer </dev/tty || answer=""
  fi
  printf "%s" "${answer:-${default}}"
}

confirm() {
  # confirm <prompt> <default-yes-or-no> → exits 0 for yes, 1 for no
  local prompt="$1" default="${2:-y}" hint answer
  hint=$([[ "${default}" == "y" ]] && echo "Y/n" || echo "y/N")
  if [[ ${INTERACTIVE} -eq 0 ]]; then
    [[ "${default}" == "y" ]]
    return
  fi
  printf "    %s [%s] " "${prompt}" "${hint}" >/dev/tty
  read -r answer </dev/tty || answer=""
  answer="${answer:-${default}}"
  [[ "${answer}" =~ ^[Yy] ]]
}

# ── banner ───────────────────────────────────────────────────────────
printf "%s┌─ Fathom installer ─%s\n" "${C_BLD}" "${C_RST}"
printf "%s│%s  Self-hosted memory lake. About five minutes.\n" "${C_BLD}" "${C_RST}"
printf "%s└─%s\n" "${C_BLD}" "${C_RST}"

# ── prereq check ─────────────────────────────────────────────────────
step "Checking prerequisites"

missing=()

if command -v git >/dev/null 2>&1; then
  ok "git ($(git --version | awk '{print $3}'))"
else
  fail "git is not installed"
  missing+=("git")
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "docker compose"
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
  ok "podman compose"
else
  fail "no container runtime found"
  info "Install one of:"
  info "  Docker Desktop:  https://docs.docker.com/desktop/"
  info "  Docker Engine:   https://docs.docker.com/engine/install/"
  info "  Podman:          https://podman.io/docs/installation"
  missing+=("container runtime")
fi

if [[ ${#missing[@]} -gt 0 ]]; then
  echo
  die "Install the missing prerequisite(s) above and re-run." \
      "Everything else this installer does happens after these are present."
fi

# ── target dir ───────────────────────────────────────────────────────
step "Where to install"

FATHOM_DIR="${FATHOM_DIR:-${HOME}/.fathom/src}"
FATHOM_DIR="$(ask "Install location" "${FATHOM_DIR}")"
FATHOM_DIR="${FATHOM_DIR/#\~/${HOME}}"  # expand leading ~

if [[ "${FATHOM_DIR:0:1}" != "/" ]]; then
  die "Install location must be an absolute path. Got: ${FATHOM_DIR}"
fi

ok "Target: ${FATHOM_DIR}"

# ── clone or update ──────────────────────────────────────────────────
FATHOM_REPO="${FATHOM_REPO:-https://github.com/fathomdx-io/fathomdx.git}"
FATHOM_REF="${FATHOM_REF:-main}"

compose_cmd() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    podman compose "$@"
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

get_env_from_dir() {
  local dir="$1" key="$2"
  [[ -f "${dir}/.env" ]] || { printf ""; return; }
  grep -E "^${key}=" "${dir}/.env" | head -1 \
    | sed -E "s/^${key}=//" \
    | sed -E 's/^"(.*)"$/\1/' \
    | sed -E "s/^'(.*)'\$/\1/"
}

do_lake_backup() {
  local dir="$1"   # repo dir (compose context)
  local lake="$2"
  local volume="$3"
  local project="$4"

  local backup_dir="${HOME}/fathom-backups"
  mkdir -p "${backup_dir}"
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local backup_file="${backup_dir}/${ts}-${project}-pre-reset.sql.gz"

  info "Starting postgres for backup (if not already running)..."
  (cd "${dir}" && compose_cmd up -d postgres >/dev/null 2>&1) || true

  local waited=0
  while [[ ${waited} -lt 30 ]]; do
    if (cd "${dir}" && compose_cmd exec -T postgres pg_isready -U fathom -d deltas >/dev/null 2>&1); then
      break
    fi
    sleep 1; waited=$((waited + 1))
  done

  info "Dumping to ${backup_file} ..."
  (cd "${dir}" && compose_cmd exec -T postgres \
    pg_dump -U fathom -d deltas --no-password) \
    | gzip > "${backup_file}"
  ok "Backup saved: ${backup_file}"

  local media_dir="${lake}/deltas/media"
  if [[ -d "${media_dir}" ]] && [[ -n "$(ls -A "${media_dir}" 2>/dev/null)" ]]; then
    local media_archive="${backup_dir}/${ts}-${project}-media.tar.gz"
    tar -czf "${media_archive}" -C "${lake}/deltas" media
    ok "Media archived: ${media_archive}"
  fi
}

do_wipe_lake() {
  local lake="$1"
  if [[ ! -d "${lake}" ]]; then
    info "Lake dir ${lake} does not exist — nothing to wipe"
    return
  fi
  for sub in api deltas backups source-runner; do
    local target="${lake}/${sub}"
    if [[ -d "${target}" ]]; then
      rm -rf "${target}"; mkdir -p "${target}"
      ok "Cleared ${target}"
    fi
  done
}

EXISTING_INSTALL=0
if [[ -d "${FATHOM_DIR}/.git" ]]; then
  EXISTING_INSTALL=1

  # Resolve lake + project from the existing install's .env
  ex_project="$(get_env_from_dir "${FATHOM_DIR}" COMPOSE_PROJECT_NAME)"
  ex_project="${ex_project:-fathom}"
  ex_lake="$(get_env_from_dir "${FATHOM_DIR}" LAKE_DIR)"
  ex_lake="${ex_lake:-${HOME}/.fathom/mind}"
  ex_lake="${ex_lake/#\~/${HOME}}"
  ex_volume="${ex_project}-pg"

  step "Existing install found at ${FATHOM_DIR}"
  info "Lake: ${ex_lake}   Volume: ${ex_volume}"

  install_choice="update"
  if [[ ${INTERACTIVE} -eq 1 ]]; then
    printf "\n    What do you want to do?\n"
    printf "      u  Update in place             — git pull + rebuild\n"
    printf "      r  Backup mind, wipe, start clean\n"
    printf "      q  Quit\n"
    printf "\n    Choice [u/r/q]: " >/dev/tty
    read -r install_choice </dev/tty || install_choice="u"
    install_choice="${install_choice:-u}"
  fi

  case "${install_choice}" in
    r|R|reinstall|fresh)
      step "Reinstalling fresh"

      # Offer backup if the pg volume has data.
      if volume_exists "${ex_volume}"; then
        printf "\n  %sYour lake has data (volume: %s).%s\n" "${C_YLW}" "${ex_volume}" "${C_RST}"
        if confirm "Back it up before wiping?" "y"; then
          do_lake_backup "${FATHOM_DIR}" "${ex_lake}" "${ex_volume}" "${ex_project}"
        fi
      fi

      step "Wiping existing lake"
      (cd "${FATHOM_DIR}" && compose_cmd down -v 2>/dev/null) \
        || warn "compose down -v returned non-zero — continuing"
      ok "Stack stopped, volume removed"
      do_wipe_lake "${ex_lake}"
      ;;
    q|Q|quit)
      printf "\n  Aborted.\n\n"
      exit 0
      ;;
    *)
      # update — fall through to git pull below
      ;;
  esac

  cd "${FATHOM_DIR}"
  if ! confirm "Pull latest from ${FATHOM_REF}?" "y"; then
    info "Skipped git pull (using whatever's currently checked out)."
  else
    # Stash uncommitted local changes so pull can fast-forward.
    if [[ -n "$(git status --porcelain)" ]]; then
      warn "You have uncommitted changes — stashing them so pull can run."
      git stash push -u -m "install.sh autosave $(date -u +%Y%m%dT%H%M%SZ)"
      info "Restore later with: git stash pop"
    fi
    git fetch origin "${FATHOM_REF}" --quiet
    git checkout "${FATHOM_REF}" --quiet
    git pull --ff-only origin "${FATHOM_REF}" --quiet
    ok "Updated to $(git rev-parse --short HEAD)"
  fi
elif [[ -e "${FATHOM_DIR}" ]]; then
  die "${FATHOM_DIR} exists but isn't a Fathom checkout." \
      "Move/remove it, or set FATHOM_DIR to a different path and re-run."
else
  # ── orphan volume check ──────────────────────────────────────────
  # Even on a truly fresh machine, a leftover postgres volume from a
  # prior install will silently re-attach and skip onboarding.
  # Check before cloning so users aren't surprised after setup.
  orphan_volume="fathom-pg"   # the default; custom project names are rare here
  if volume_exists "${orphan_volume}"; then
    printf "\n  %sFound a leftover lake volume (%s) from a prior install.%s\n" \
      "${C_YLW}" "${orphan_volume}" "${C_RST}"
    printf "  If you continue without wiping it, you'll be logged in as the\n"
    printf "  previous user instead of going through setup.\n\n"
    if confirm "Wipe it and start clean? (recommended)" "y"; then
      orphan_lake="${HOME}/.fathom/mind"
      if volume_exists "${orphan_volume}"; then
        # Spin up a temporary postgres just long enough to dump it.
        if confirm "Back up the lake first?" "y"; then
          do_lake_backup "${FATHOM_DIR}" "${orphan_lake}" "${orphan_volume}" "fathom" 2>/dev/null \
            || warn "Backup failed — continuing with wipe anyway."
        fi
        if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
          docker volume rm "${orphan_volume}" >/dev/null 2>&1 && ok "Removed volume ${orphan_volume}" \
            || warn "Could not remove ${orphan_volume} — it may still be attached to a running container. Run 'docker compose down' first."
        else
          podman volume rm "${orphan_volume}" >/dev/null 2>&1 && ok "Removed volume ${orphan_volume}" \
            || warn "Could not remove ${orphan_volume} — it may still be attached to a running container. Run 'podman compose down' first."
        fi
        do_wipe_lake "${orphan_lake}"
      fi
    else
      info "Keeping existing volume — you may be auto-logged in as a prior user."
    fi
  fi

  step "Cloning repository"
  info "${FATHOM_REPO} → ${FATHOM_DIR}"
  # `git clone --branch` only accepts branch/tag names, not commit SHAs.
  # CI's install-smoke workflow passes ${{ github.sha }} so it tests THIS
  # exact commit; for that path we clone bare then fetch the SHA directly.
  if [[ "${FATHOM_REF}" =~ ^[0-9a-f]{7,40}$ ]]; then
    git clone --filter=blob:none --no-checkout "${FATHOM_REPO}" "${FATHOM_DIR}" --quiet
    cd "${FATHOM_DIR}"
    git fetch --depth 1 origin "${FATHOM_REF}" --quiet
    git checkout --quiet FETCH_HEAD
  else
    git clone --depth 1 --branch "${FATHOM_REF}" "${FATHOM_REPO}" "${FATHOM_DIR}" --quiet
    cd "${FATHOM_DIR}"
  fi
  ok "Cloned at $(git rev-parse --short HEAD)"
fi

# ── preflight ────────────────────────────────────────────────────────
step "Running preflight"

PREFLIGHT="${FATHOM_DIR}/addons/scripts/preflight.sh"
if [[ ! -x "${PREFLIGHT}" ]]; then
  die "preflight.sh missing or not executable at ${PREFLIGHT}" \
      "This usually means a partial clone — try removing ${FATHOM_DIR} and re-running."
fi

# Preflight needs /dev/tty for its own prompts. When interactive, hand it
# stdin from /dev/tty directly so it can read user answers. Capture exit code.
preflight_ec=0
if [[ ${INTERACTIVE} -eq 1 ]]; then
  "${PREFLIGHT}" </dev/tty || preflight_ec=$?
else
  NONINTERACTIVE=1 "${PREFLIGHT}" </dev/null || preflight_ec=$?
fi

if [[ ${preflight_ec} -ne 0 ]]; then
  echo
  warn "Preflight reported issues above (most commonly: LLM_API_KEY needs to be set)."
  info "Fix what it flagged, then run:"
  printf "\n      cd %s\n      ./addons/scripts/preflight.sh\n      docker compose up -d --build\n\n" "${FATHOM_DIR}"
  exit ${preflight_ec}
fi

# ── start the stack ──────────────────────────────────────────────────
step "Starting the stack"

# Decision: should we launch (build + up -d) right now?
#  · Re-running on an existing install: yes, automatically — the user
#    asked for the installer to "pull and rebuild" without re-prompting.
#  · First install, interactive: ask (default yes).
#  · First install, non-interactive: only when FATHOM_AUTOSTART=1 — CI
#    and scripted runs should prepare without launching.
should_start=0
if [[ ${EXISTING_INSTALL} -eq 1 ]]; then
  should_start=1
  info "Rebuilding and restarting."
elif [[ ${INTERACTIVE} -eq 1 ]]; then
  confirm "Build and start the stack now ('docker compose up -d --build')?" "y" && should_start=1
elif [[ "${FATHOM_AUTOSTART:-0}" == "1" ]]; then
  should_start=1
  info "FATHOM_AUTOSTART=1 — building and starting the stack."
fi

if [[ ${should_start} -eq 1 ]]; then
  # --build picks up local source / Dockerfile changes; --force-recreate
  # ensures containers swap out even when compose decides the image
  # hash hasn't changed (a known podman compose quirk where a freshly
  # built image still runs the cached one).
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose up -d --build --force-recreate
  else
    podman compose up -d --build --force-recreate
  fi
  ok "Stack built + started"
  echo
  printf "%sFathom is up.%s Open: %shttp://localhost:8201%s\n\n" \
    "${C_GRN}" "${C_RST}" "${C_BLD}" "${C_RST}"
  info "Repo: ${FATHOM_DIR}"
  info "Logs: docker compose logs -f api"
  info "Stop: docker compose down"
else
  echo
  printf "%sReady when you are.%s Next:\n\n      cd %s\n      docker compose up -d --build\n      open http://localhost:8201\n\n" \
    "${C_GRN}" "${C_RST}" "${FATHOM_DIR}"
fi
