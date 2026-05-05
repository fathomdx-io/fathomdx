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

EXISTING_INSTALL=0
if [[ -d "${FATHOM_DIR}/.git" ]]; then
  EXISTING_INSTALL=1
  step "Updating existing install"
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
  info "Existing install — rebuilding and restarting automatically."
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
