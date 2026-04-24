#!/usr/bin/env bash
# preflight — validate and repair the local install before `docker compose up`.
#
# Catches every silent first-run failure we've seen so far:
#   - .env missing
#   - LAKE_DIR unset, blank, relative, or still the CHANGE-ME placeholder
#   - LAKE_DIR's required subdirectories missing (rootless podman won't auto-create)
#   - LLM_API_KEY blank for a provider that needs one
#
# Exits 0 when the install is ready to `docker compose up`. Exits non-zero with
# a human-readable explanation when something needs your attention.
#
# Safe to re-run. Modifies .env in place when it can fix things automatically;
# prompts when a value can't be guessed (paths and API keys).

set -euo pipefail

# ── locate repo root ─────────────────────────────────────────────────
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

ok()    { printf "  %s✓%s %s\n" "${C_GRN}" "${C_RST}" "$*"; }
info()  { printf "  %s•%s %s\n" "${C_BLU}" "${C_RST}" "$*"; }
warn()  { printf "  %s!%s %s\n" "${C_YLW}" "${C_RST}" "$*"; }
fail()  { printf "  %s✗%s %s\n" "${C_RED}" "${C_RST}" "$*" >&2; }
step()  { printf "\n%s%s%s\n" "${C_BLU}" "$*" "${C_RST}"; }

die() {
  fail "$1"
  [[ $# -ge 2 ]] && printf "    %s%s%s\n" "${C_DIM}" "$2" "${C_RST}" >&2
  exit 1
}

# ── .env helpers ─────────────────────────────────────────────────────
# Read a value from .env. Strips surrounding quotes. Empty string if unset.
get_env() {
  local key="$1"
  [[ -f .env ]] || { printf ""; return; }
  grep -E "^${key}=" .env | head -1 \
    | sed -E "s/^${key}=//" \
    | sed -E 's/^"(.*)"$/\1/' \
    | sed -E "s/^'(.*)'\$/\1/"
}

# Set or replace a key=value line in .env. Uses | as the sed delimiter so
# paths with / don't need escaping.
set_env() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" .env; then
    sed -i.bak "s|^${key}=.*|${key}=${val}|" .env && rm -f .env.bak
  else
    printf "%s=%s\n" "${key}" "${val}" >> .env
  fi
}

# ── checks ───────────────────────────────────────────────────────────

check_env_file() {
  step "Checking .env"
  if [[ ! -f .env ]]; then
    if [[ ! -f .env.example ]]; then
      die ".env.example is missing." \
          "Are you in a fathomdx checkout? Try: cd path/to/fathomdx"
    fi
    cp .env.example .env
    ok "Created .env from .env.example"
  else
    ok ".env exists"
  fi
}

check_lake_dir() {
  step "Lake location"

  # Default is intentionally NOT derived from COMPOSE_PROJECT_NAME — that var
  # scopes container/volume names; the lake path is decoupled so the canonical
  # single-instance install gets a clean ~/.fathom/mind layout instead of the
  # awkward ~/.fathom/fathom doubling. Multi-instance users set LAKE_DIR
  # explicitly (e.g. ~/.fathom/mind-dev) alongside their COMPOSE_PROJECT_NAME.
  local default="${HOME}/.fathom/mind"

  local lake="$(get_env LAKE_DIR)"
  local needs_fix=0 reason=""

  if [[ -z "${lake}" ]]; then
    needs_fix=1; reason="not set"
  elif [[ "${lake}" == *CHANGE-ME* ]]; then
    needs_fix=1; reason="placeholder value"
  elif [[ "${lake:0:1}" != "/" ]]; then
    needs_fix=1; reason="must be an absolute path (got '${lake}')"
  fi

  if [[ ${needs_fix} -eq 1 ]]; then
    if [[ -t 0 ]]; then
      # Interactive: just ask. The "why" is internal noise on first-run.
      printf "    Where should your lake live? [%s] " "${default}"
      read -r answer
      lake="${answer:-${default}}"
    else
      # Non-interactive (CI, scripted): log the reason so the trail explains
      # what was assumed and why.
      info "LAKE_DIR ${reason} — defaulting to ${default}"
      lake="${default}"
    fi
    # Expand a leading ~ if the user typed one
    lake="${lake/#\~/${HOME}}"
    if [[ "${lake:0:1}" != "/" ]]; then
      die "LAKE_DIR must be an absolute path. Got: ${lake}"
    fi
    set_env LAKE_DIR "${lake}"
    ok "Set LAKE_DIR=${lake}"
  else
    ok "LAKE_DIR=${lake}"
  fi

  # Create the four subdirectories docker-compose binds. Rootless podman
  # won't auto-create these and fails with a confusing "no such file" error.
  local sub
  for sub in deltas backups source-runner api; do
    if [[ ! -d "${lake}/${sub}" ]]; then
      mkdir -p "${lake}/${sub}" \
        || die "Could not create ${lake}/${sub}" \
               "Check permissions on the parent path."
      ok "Created ${lake}/${sub}"
    fi
  done

  # Drop the README marker so users wandering into ${lake} know what it is.
  local readme_src="${REPO_DIR}/addons/scripts/lake-dir-README.md"
  if [[ -f "${readme_src}" && ! -f "${lake}/README.md" ]]; then
    cp "${readme_src}" "${lake}/README.md"
    ok "Wrote ${lake}/README.md"
  fi
}

check_llm_key() {
  step "LLM provider"

  # Env vars override .env values, so users can do
  # `LLM_API_KEY=… curl … | bash` and CI can set LLM_PROVIDER=ollama
  # without touching files. Persists the override into .env.
  if [[ -n "${LLM_PROVIDER:-}" ]]; then
    set_env LLM_PROVIDER "${LLM_PROVIDER}"
    info "LLM_PROVIDER=${LLM_PROVIDER} (from environment)"
  fi
  if [[ -n "${LLM_API_KEY:-}" ]]; then
    set_env LLM_API_KEY "${LLM_API_KEY}"
    info "LLM_API_KEY set from environment"
  fi

  local provider="$(get_env LLM_PROVIDER)"
  provider="${provider:-gemini}"
  local key="$(get_env LLM_API_KEY)"

  # ollama is a local model server — no key needed, regardless of who set it.
  if [[ "${provider}" == "ollama" ]]; then
    ok "LLM_PROVIDER=ollama (no API key needed)"
    return 0
  fi

  # Already configured for a hosted provider with a key — done.
  if [[ -n "${key}" ]]; then
    ok "LLM_PROVIDER=${provider} (key present)"
    return 0
  fi

  # No key. Interactive: ask which provider + paste the key right here.
  # Non-interactive: log what's missing and exit non-zero.
  if [[ ! -t 0 ]]; then
    warn "LLM_API_KEY is blank for provider '${provider}'"
    case "${provider}" in
      gemini) info "Get one at https://aistudio.google.com/apikey" ;;
      openai) info "Get one at https://platform.openai.com/api-keys" ;;
      *)      warn "Unknown LLM_PROVIDER='${provider}' (expected: gemini, openai, ollama)" ;;
    esac
    info "Open .env and set LLM_API_KEY=... before continuing."
    return 1
  fi

  # Interactive provider pick.
  echo "    Which LLM provider does this instance talk to?"
  echo "      gemini  — Google AI Studio (free tier available)"
  echo "      openai  — OpenAI (gpt-4o by default)"
  echo "      ollama  — local model server, no key needed"
  printf "    Provider [%s]: " "${provider}"
  local answer=""
  read -r answer
  answer="${answer:-${provider}}"

  case "${answer}" in
    gemini|openai|ollama)
      provider="${answer}"
      set_env LLM_PROVIDER "${provider}"
      ;;
    *)
      warn "Unknown provider '${answer}' — keeping ${provider}"
      ;;
  esac

  if [[ "${provider}" == "ollama" ]]; then
    ok "LLM_PROVIDER=ollama (no API key needed)"
    return 0
  fi

  # Hosted provider — prompt for key, with a skip option.
  case "${provider}" in
    gemini) info "Get a key at https://aistudio.google.com/apikey" ;;
    openai) info "Get a key at https://platform.openai.com/api-keys" ;;
  esac
  printf "    Paste your %s API key (or press enter to set later): " "${provider}"
  # Visible echo on purpose: silent input makes paste impossible to verify
  # mid-install, which generates more "did it work?" anxiety than the
  # marginal scrollback risk avoids. Users running an install command are
  # already trusting their terminal; if the threat model demands more,
  # they can pre-set LLM_API_KEY in the environment and skip this prompt.
  local entered=""
  read -r entered

  if [[ -n "${entered}" ]]; then
    set_env LLM_API_KEY "${entered}"
    ok "LLM_PROVIDER=${provider} (key set, ${#entered} chars)"
    return 0
  fi

  # User skipped — leave .env untouched, surface clearly what to do.
  warn "No key entered."
  info "Open ${REPO_DIR}/.env, set LLM_API_KEY=…, then re-run preflight."
  return 1
}

check_compose() {
  step "Checking docker / podman compose"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "docker compose available ($(docker compose version --short 2>/dev/null || echo 'present'))"
  elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    ok "podman compose available"
  else
    fail "Neither 'docker compose' nor 'podman compose' is available on PATH"
    info "Install Docker Desktop, Docker Engine + compose plugin, or Podman + podman-compose"
    return 1
  fi
}

# ── main ─────────────────────────────────────────────────────────────
printf "%sFathom preflight%s — %s\n" "${C_BLU}" "${C_RST}" "${REPO_DIR}"

failures=0
check_env_file
check_lake_dir
check_llm_key   || failures=$((failures + 1))
check_compose   || failures=$((failures + 1))

echo
if [[ ${failures} -eq 0 ]]; then
  printf "%sReady.%s Next:\n\n    docker compose up -d\n    open http://localhost:8201\n\n" \
    "${C_GRN}" "${C_RST}"
  exit 0
else
  printf "%s%d issue(s) above need attention.%s Re-run preflight after fixing.\n\n" \
    "${C_YLW}" "${failures}" "${C_RST}"
  exit 1
fi
