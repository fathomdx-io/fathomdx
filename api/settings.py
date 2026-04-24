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
    "ollama": {
        "base_url": "http://localhost:11434/v1/",
        "medium": "llama3.1:8b",
        "hard": "qwen2.5:32b",  # ~20GB VRAM; drop to :14b on a 16GB box
    },
}


class Settings(BaseSettings):
    # LLM provider — these use the LLM_ prefix (not FATHOM_) to keep them
    # distinct from Fathom's own bearer tokens (ftm_…), which the client
    # tools (mcp, cli, hooks) read from FATHOM_API_KEY.
    provider: str = Field("gemini", validation_alias="LLM_PROVIDER")
    api_key: str = Field("", validation_alias="LLM_API_KEY")
    base_url: str = Field("", validation_alias="LLM_BASE_URL")  # overrides provider default
    # Per-tier overrides. LLM_MODEL is the pre-tier name; it still works
    # and maps to the hard tier so existing .env files keep running.
    model: str = Field("", validation_alias="LLM_MODEL")
    model_hard: str = Field("", validation_alias="LLM_MODEL_HARD")
    model_medium: str = Field("", validation_alias="LLM_MODEL_MEDIUM")

    # Delta store
    delta_store_url: str = "http://localhost:8100"
    delta_api_key: str = ""

    # Source runner
    source_runner_url: str = "http://localhost:4260"

    # Paths (container defaults). Crystal is lake-backed — no file path.
    feed_directive_path: str = "/data/feed-directive.txt"
    tokens_path: str = "/data/tokens.json"
    mood_state_path: str = "/data/mood-state.json"
    pair_codes_path: str = "/data/pair-codes.json"

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
    feed_loop_visit_debounce_seconds: int = 600  # 10 min

    # Server
    host: str = "0.0.0.0"
    port: int = 8200

    model_config = {"env_prefix": "FATHOM_"}

    @property
    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        return PROVIDER_DEFAULTS.get(self.provider, {}).get("base_url", "")

    @property
    def resolved_model_hard(self) -> str:
        """Model for the chat loop and identity-crystal regen."""
        if self.model_hard:
            return self.model_hard
        if self.model:  # back-compat: bare LLM_MODEL means "the chat model"
            return self.model
        return PROVIDER_DEFAULTS.get(self.provider, {}).get("hard", "")

    @property
    def resolved_model_medium(self) -> str:
        """Model for search planning, mood, feed-crystal."""
        if self.model_medium:
            return self.model_medium
        return PROVIDER_DEFAULTS.get(self.provider, {}).get("medium", "")

    # Backwards-compatible alias for call-sites that only ever wanted
    # "the chat model" — keep pointing them at the hard tier.
    @property
    def resolved_model(self) -> str:
        return self.resolved_model_hard


settings = Settings()
