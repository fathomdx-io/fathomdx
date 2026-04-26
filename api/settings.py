"""Configuration from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

# Per-provider model picks, grouped by difficulty tier. Picks are
# opinionated choices that balance capability against cost; they're not
# authoritative — verify against the provider's current lineup when
# model families age out. Users override via LLM_MODEL_HARD /
# LLM_MODEL_MEDIUM in .env (with LLM_MODEL as a back-compat alias for
# hard). Two tiers today:
#   hard   — chat loop, identity-crystal regen. Needs tool use,
#            structured output, voice. Don't under-spec this.
#   medium — search planning, mood synth, feed-crystal, in-turn prose.
#            Fine to run on a cheaper model; under-speccing shows up as
#            weaker memory routing, not broken conversation.
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "medium": "gemini-2.5-flash",
        "hard": "gemini-2.5-pro",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1/",
        "medium": "gpt-4o-mini",
        "hard": "gpt-4o",
    },
    "anthropic": {
        # Anthropic publishes an OpenAI-compatible endpoint that speaks
        # the same tool-call protocol — same client works.
        "base_url": "https://api.anthropic.com/v1/",
        "medium": "claude-haiku-4-5",
        "hard": "claude-sonnet-4-6",
    },
    "local": {
        # Any local OpenAI-compat server (ollama, LM Studio, vLLM,
        # llama.cpp server, …). Base URL is user-supplied because the
        # default depends on where the server runs relative to the api
        # container (host.docker.internal vs a LAN IP).
        "base_url": "",
        "medium": "llama3.1:8b",
        "hard": "qwen2.5:32b",  # ~20GB VRAM; drop to :14b on a 16GB box
    },
}


class Settings(BaseSettings):
    # Legacy single-provider config — still supported. If set, LLM_API_KEY
    # populates the credentials for whichever provider LLM_PROVIDER names,
    # so existing installs keep working without touching .env. Keep the
    # LLM_ prefix (not FATHOM_) to stay distinct from Fathom's own bearer
    # tokens (ftm_…), which the client tools read from FATHOM_API_KEY.
    provider: str = Field("gemini", validation_alias="LLM_PROVIDER")
    api_key: str = Field("", validation_alias="LLM_API_KEY")
    base_url: str = Field("", validation_alias="LLM_BASE_URL")  # overrides provider default
    # Per-tier overrides. LLM_MODEL is the pre-tier name; it still works
    # and maps to the hard tier so existing .env files keep running.
    model: str = Field("", validation_alias="LLM_MODEL")
    model_hard: str = Field("", validation_alias="LLM_MODEL_HARD")
    model_medium: str = Field("", validation_alias="LLM_MODEL_MEDIUM")

    # Per-provider credentials. Any subset may be set; a provider without
    # credentials is hidden from the Models tab and can't be chosen for
    # a tier. `local` (any OpenAI-compat server running on your machine
    # or LAN — ollama, LM Studio, vLLM, …) needs no API key; presence
    # of LOCAL_BASE_URL is the "configured" signal. OLLAMA_BASE_URL is
    # kept as a legacy alias so upgrade-in-place works.
    gemini_api_key: str = Field("", validation_alias="GEMINI_API_KEY")
    openai_api_key: str = Field("", validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    local_base_url: str = Field("", validation_alias="LOCAL_BASE_URL")
    ollama_base_url: str = Field("", validation_alias="OLLAMA_BASE_URL")

    # Delta store
    delta_store_url: str = "http://localhost:8100"
    delta_api_key: str = ""

    # Source runner
    source_runner_url: str = "http://localhost:4260"

    # Paths (container defaults). Crystal is lake-backed — no file path.
    tokens_path: str = "/data/tokens.json"
    mood_state_path: str = "/data/mood-state.json"
    pair_codes_path: str = "/data/pair-codes.json"

    # Absolute host path where this checkout lives — the directory the
    # operator ran `docker compose up` from. Wired through compose as
    # FATHOM_HOST_REPO_DIR=${PWD}. Used by UI surfaces that need to
    # show a real shell command (e.g. the "Forgot my key" disclosure on
    # the login page, which renders
    # <host_repo_dir>/addons/scripts/mint-key.sh). Empty when a caller
    # runs the api outside compose; the UI falls back to a relative
    # path in that case.
    host_repo_dir: str = Field("", validation_alias="FATHOM_HOST_REPO_DIR")

    # Self-signup gate. When on, POST /v1/auth/register is open: a new
    # person can create their own member-scoped contact + token via the
    # onboarding page without an admin present. Off means only
    # admin-minted pair codes can onboard new contacts. Default on
    # because self-hosted single-box installs typically run on a LAN
    # where open signup is the frictionless default; operators running
    # a public instance should flip this.
    signup_enabled: bool = Field(True, validation_alias="FATHOM_SIGNUP_ENABLED")

    # Allowlist directory for `image_path` on POST /v1/deltas and the
    # `write` tool. When a caller hands the api a local filesystem path
    # instead of base64, the api reads that path — a bare acceptance
    # would be arbitrary-file-read for anyone with a lake:write token.
    # Any path not resolving inside this prefix is rejected. Empty
    # string (default) disables the feature entirely — callers must use
    # image_b64. Set this only when a dedicated staging volume is
    # mounted into the api container.
    image_path_allowed_prefix: str = ""

    # Mood layer (carrier wave) — pressure thresholds
    # Threshold tuned against a real lake. With ~50 deltas/hour, pressure
    # builds to ~30 within a few hours; 25 fires roughly every 2-3 hours
    # of sustained activity unless a contrast-wake intervenes.
    mood_pressure_threshold: float = 25.0
    mood_decay_half_life_seconds: int = 14400  # 4 hours
    mood_contrast_wake_seconds: int = 21600  # 6 hours

    # Feed-layer pressure — same primitive as mood, tuned for content
    # synthesis. See api/feed_pressure.py for weights and rationale.
    # Starting threshold is slightly above mood's because feed weights
    # tilt heavier on content surfaces (RSS doubled, engagement at 1.5);
    # the same lake-flow produces a higher feed-volume than mood-volume.
    # Tune after a few days of real data.
    feed_pressure_threshold: float = 30.0
    feed_pressure_decay_half_life_seconds: int = 10800  # 3 hours
    feed_pressure_contrast_wake_seconds: int = 28800  # 8 hours
    feed_pressure_state_path: str = "/data/feed-pressure-state.json"

    # Crystal auto-regeneration.
    # Auto-regen fires when (drift / threshold) >= red_ratio AND the last
    # regen was at least cooldown_seconds ago (guard against runaway).
    # Cooldown is deliberately long — a crystal is a durable self-description,
    # not an hourly event; multiple regens per day always indicates either
    # instability or a broken gate.
    crystal_auto_regen: bool = True
    # Anchor-based drift starts at 0 after every accepted regen (see
    # crystal_anchor.py), so centroid drift grows slowly and monotonically
    # in a lake that's being fed. 0.15 fires the red zone at ~0.135 —
    # tight enough to catch real topic drift, loose enough that routine
    # ingestion alone won't burn cooldown cycles.
    crystal_drift_threshold: float = 0.15
    crystal_drift_red_ratio: float = 0.9
    crystal_drift_poll_seconds: int = 60
    crystal_regen_cooldown_seconds: int = 259200  # 3 days

    # Feed-orient crystal (mood-shape regen, not identity-shape).
    # See docs/feed-spec.md. The min-signal guard is the cold-start
    # fail-open lesson from the 2026-04-19 auto-regen runaway.
    feed_crystal_cooldown_seconds: int = 21600  # 6 hours
    feed_drift_threshold: float = 0.35
    feed_confidence_floor: float = 0.55
    feed_min_signal_engagements: int = 10
    # Engagement-confidence recency decay. A week-old hit on last week's
    # crystal says less about today's taste than a hit from yesterday.
    # Half-life of 3 days: 1d ≈ 0.79 weight, 3d = 0.5, 7d ≈ 0.2.
    feed_engagement_half_life_seconds: int = 259200  # 3 days

    # Feed loop — per-directive-line budgets. Without a budget,
    # "until satisfied" is a runaway-cost grenade.
    feed_loop_budget_tool_calls: int = 8
    feed_loop_budget_seconds: int = 90

    # Drift pass — the free-association card slot. Gets a bigger tool-call
    # ceiling than per-line because its whole shape is "follow the thread"
    # (remember/recall outward from a scatter item). Wall-clock stays the
    # same; drift shouldn't be allowed to monopolize a fire.
    feed_drift_budget_tool_calls: int = 16

    # Volunteered noticing — the present-salience off-crystal slot. Reuses
    # per-line budgets (feed_loop_budget_tool_calls + feed_loop_budget_seconds).
    # Its shape is closer to per-line than drift: "scan, pick, compose."

    # Synthesis pass budgets — max items per cycle, per pass kind. These
    # cap output independently of the LLM's own scoring; below the axis
    # floor, items are dropped *before* hitting the budget. The budget is
    # the upper end (no quota pressure) — a quiet pass producing zero is
    # a healthy outcome, not a failure.
    feed_pass_budget_alert: int = 5  # piercing tier; rare, uncapped on emergencies
    feed_pass_budget_reflection: int = 2  # dense outputs; over-frequent reflection is noise
    feed_pass_budget_bridging: int = 2  # cross-workspace; quality over quantity
    feed_pass_budget_discrepancy: int = 1  # uncomfortable — don't pile on

    # Drop floors — if an item's axis score falls below these, the router
    # drops it entirely (throwaway as first-class destination). Items
    # above the floor go to the level computation.
    feed_axis_floor_salience: float = 0.20
    feed_axis_floor_confidence: float = 0.30  # below this, the judge thinks it might be confabulated

    # Level promotion thresholds. Composed by _feed_router.route() into
    # ALERT > NOTICE > INFO > DEBUG > TRACE bands. ALERT auto-promotes
    # when salience is very high AND comfort is low (the uncomfortable-
    # truth gate) OR when the pass kind is "alert" itself (piercing).
    feed_level_alert_salience: float = 0.92
    feed_level_alert_comfort_max: float = 0.30
    feed_level_notice_salience: float = 0.55
    feed_level_notice_resonance: float = 0.50
    feed_level_info_salience: float = 0.35

    # Default level the dashboard shows when the user hasn't picked one
    # in the verbosity dropdown. NOTICE means alerts pierce, the
    # curated mid-tier is visible, and reflection/bridging/discrepancy
    # stay in the lake until dialed up.
    feed_default_visible_level: str = "NOTICE"

    # Server
    host: str = "0.0.0.0"
    port: int = 8200

    model_config = {"env_prefix": "FATHOM_"}

    @property
    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        return PROVIDER_DEFAULTS.get(self.effective_provider, {}).get("base_url", "")

    @property
    def resolved_model_hard(self) -> str:
        """Model for the chat loop and identity-crystal regen."""
        if self.model_hard:
            return self.model_hard
        if self.model:  # back-compat: bare LLM_MODEL means "the chat model"
            return self.model
        return PROVIDER_DEFAULTS.get(self.effective_provider, {}).get("hard", "")

    @property
    def resolved_model_medium(self) -> str:
        """Model for search planning, mood, feed-crystal."""
        if self.model_medium:
            return self.model_medium
        return PROVIDER_DEFAULTS.get(self.effective_provider, {}).get("medium", "")

    # Backwards-compatible alias for call-sites that only ever wanted
    # "the chat model" — keep pointing them at the hard tier.
    @property
    def resolved_model(self) -> str:
        return self.resolved_model_hard

    @property
    def effective_provider(self) -> str:
        """Normalizes legacy LLM_PROVIDER=ollama to the new 'local' name
        so the rest of the config ladder lines up without a special case
        at every site."""
        if self.provider == "ollama":
            return "local"
        return self.provider

    def provider_credentials(self, provider: str) -> tuple[str, str]:
        """(api_key, base_url) for a provider. Falls back to the legacy
        single-provider fields when LLM_PROVIDER matches the requested
        provider — that's the upgrade path for existing installs."""
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        base_url = defaults.get("base_url", "")
        api_key = ""
        if provider == "gemini":
            api_key = self.gemini_api_key
        elif provider == "openai":
            api_key = self.openai_api_key
        elif provider == "anthropic":
            api_key = self.anthropic_api_key
        elif provider == "local":
            # Local OpenAI-compat servers don't need a real key. The SDK
            # requires a non-empty string though, so pass a placeholder.
            api_key = "local"
            # LOCAL_BASE_URL is preferred; OLLAMA_BASE_URL is a
            # back-compat alias honored only when LOCAL_ isn't set.
            if self.local_base_url:
                base_url = self.local_base_url
            elif self.ollama_base_url:
                base_url = self.ollama_base_url
        # Legacy fallback: if LLM_PROVIDER names this provider (via its
        # current or legacy alias — ollama → local) and a per-provider
        # key wasn't set, populate from LLM_API_KEY.
        if not api_key and self.effective_provider == provider and self.api_key:
            api_key = self.api_key
        # Legacy override for base_url too.
        if self.effective_provider == provider and self.base_url:
            base_url = self.base_url
        return api_key, base_url

    def configured_providers(self) -> list[str]:
        """Providers with enough credentials to make a request. Order
        follows PROVIDER_DEFAULTS for stable UI listing."""
        out: list[str] = []
        for provider in PROVIDER_DEFAULTS:
            api_key, base_url = self.provider_credentials(provider)
            if provider == "local":
                # `local` is configured when its base URL is set — the
                # placeholder api_key is always non-empty, so base_url
                # is the real signal.
                if base_url:
                    out.append(provider)
            else:
                if api_key:
                    out.append(provider)
        return out


settings = Settings()
