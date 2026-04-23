"""Template for a new source producer.

Copy this file, rename the class, and implement poll() and digest().
Then register it in source_runner.py's _register_builtins().

Usage:
    1. cp sources/template.py sources/my_source.py
    2. Implement MySourceProducer (see below)
    3. In source_runner.py, add to _register_builtins():
           from sources.my_source import MySourceProducer
           self._registry["my-source"] = MySourceProducer
    4. Rebuild the container
    5. The new source type appears in the dashboard
"""

from __future__ import annotations

import logging

import httpx

from .base import ProducedDelta, RawItem, SourceProducer

log = logging.getLogger("source.template")


class TemplateProducer(SourceProducer):
    # ── Metadata ─────────────────────────────────────────────────────
    # These show up in the dashboard and determine default behavior.

    source_type = "template"              # unique ID, lowercase
    display_name = "Template"             # shown in the UI
    description = "Description of what this source does"
    version = "0.1.0"
    author = "you"

    auth_type = "none"                    # none | oauth2 | api_key | file
    schedule_type = "poll"                # poll | watch | import
    default_interval = "30m"              # 1m, 5m, 15m, 30m, 1h, 6h, daily
    digestion = "raw"                     # raw = structure output in digest(), llm = opt-in LLM summary
    default_expiry_days = 30              # None = keep forever
    expiry_configurable = True

    # ── Required: poll() ─────────────────────────────────────────────
    # Fetch items from the source. Return ALL items on every call.
    # The runner handles deduplication via seen IDs.

    async def poll(self, config: dict, since: float | None = None) -> list[RawItem]:
        """Fetch items from the source.

        Args:
            config: user-provided config dict (e.g. {"feed": "https://..."})
            since: timestamp of last successful poll, or None on first run

        Returns:
            List of RawItem. Return everything; the runner deduplicates.
        """
        url = config.get("url", "")
        if not url:
            return []

        items: list[RawItem] = []
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()  # or parse XML, HTML, etc.

                for entry in data:
                    items.append(
                        RawItem(
                            id=entry["id"],              # unique within this source
                            content=entry["text"],        # raw content to digest
                            timestamp=entry.get("date"),  # ISO 8601 or None
                            title=entry.get("title", ""),
                            url=entry.get("url"),
                            image_urls=[],                # optional image URLs
                        )
                    )
            except Exception:
                log.warning("Failed to fetch %s", url, exc_info=True)

        return items

    # ── Optional: digest() ───────────────────────────────────────────
    # Shape the raw item into a delta. Structure content programmatically.
    # Override to customize tags, content format, or image handling.

    def digest(self, item: RawItem, config: dict | None = None) -> ProducedDelta:
        return ProducedDelta(
            content=f"{item.title}\n\n{item.content}" if item.title else item.content,
            tags=[self.source_type],
            source=self.source_type,
            timestamp=item.timestamp,
            image_urls=item.image_urls,
        )

    # ── Optional: validate_config() ──────────────────────────────────
    # Validate user config before saving. Return error messages.

    def validate_config(self, config: dict) -> list[str]:
        if not config.get("url"):
            return ["'url' is required"]
        return []
