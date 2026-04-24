"""Source plugin runtime — scheduling, dedup, delta writes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sources.base import ProducedDelta, SourceProducer

log = logging.getLogger("source_runner")

INTERVAL_MAP = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "6h": 360,
    "daily": 1440,
}


def _interval_to_minutes(s: str) -> int:
    return INTERVAL_MAP.get(s, 30)


def slugify(name: str) -> str:
    """Convert a name to a lowercase slug: letters, numbers, dashes."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _minutes_to_interval(m: int) -> str:
    for label, mins in INTERVAL_MAP.items():
        if mins == m:
            return label
    return f"{m}m"


# ── Persisted data structures ────────────────────────────────────────────


@dataclass
class SourceConfig:
    id: str
    source_type: str
    name: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    interval_minutes: int = 30
    expiry_days: float | None = 30
    status: str = "active"
    error_message: str = ""
    created_at: str = ""


@dataclass
class SourceState:
    last_poll_at: float = 0
    next_poll_at: float = 0
    seen_ids: list[str] = field(default_factory=list)
    error_count: int = 0
    delta_count: int = 0


# ── Source Runner ────────────────────────────────────────────────────────


class SourceRunner:
    def __init__(
        self,
        *,
        delta_url: str,
        delta_key: str = "",
        sources_path: str | Path = "/data/sources.json",
        state_dir: str | Path = "/data/source-state",
    ):
        self._delta_url = delta_url.rstrip("/")
        self._delta_key = delta_key
        self._sources_path = Path(sources_path)
        self._state_dir = Path(state_dir)
        self._running = False
        self._sources: dict[str, SourceConfig] = {}
        self._states: dict[str, SourceState] = {}
        self._producers: dict[str, SourceProducer] = {}
        self._registry: dict[str, type[SourceProducer]] = {}
        self._http: httpx.AsyncClient | None = None
        # Strong references for manual-poll tasks — without this the event
        # loop only holds a weak ref (Python 3.12+) and the poll can be
        # garbage-collected mid-flight.
        self._poll_tasks: set[asyncio.Task] = set()
        self._register_builtins()

    def _register_builtins(self) -> None:
        # HomeAssistant moved to a machine-source (fathom-agent plugin at
        # consumer-fathom/addons/agent/plugins/homeassistant.js). Most HA installs
        # live on the user's LAN and the cloud runner can't reach them —
        # the agent polls from the same network as the thing it watches.
        from sources.mastodon import MastodonProducer
        from sources.rss import RSSProducer
        from sources.vault import VaultProducer

        self._registry["rss"] = RSSProducer
        self._registry["mastodon"] = MastodonProducer
        self._registry["vault"] = VaultProducer

    def _get_producer(self, source_type: str) -> SourceProducer:
        if source_type not in self._producers:
            cls = self._registry.get(source_type)
            if cls is None:
                raise ValueError(f"Unknown source type: {source_type}")
            self._producers[source_type] = cls()
        return self._producers[source_type]

    # ── Config persistence ───────────────────────────────────────────

    def _load_sources(self) -> None:
        if not self._sources_path.exists():
            self._sources = {}
            return
        try:
            data = json.loads(self._sources_path.read_text())
            self._sources = {}
            for raw in data:
                sc = SourceConfig(
                    **{k: v for k, v in raw.items() if k in SourceConfig.__dataclass_fields__}
                )
                self._sources[sc.id] = sc
        except Exception:
            log.warning("Failed to load sources from %s", self._sources_path, exc_info=True)
            self._sources = {}

    def _save_sources(self) -> None:
        self._sources_path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(sc) for sc in self._sources.values()]
        self._sources_path.write_text(json.dumps(data, indent=2))

    def _load_state(self, source_id: str) -> SourceState:
        path = self._state_dir / f"{source_id}.json"
        if not path.exists():
            return SourceState()
        try:
            raw = json.loads(path.read_text())
            return SourceState(
                **{k: v for k, v in raw.items() if k in SourceState.__dataclass_fields__}
            )
        except Exception:
            return SourceState()

    def _save_state(self, source_id: str, state: SourceState) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_dir / f"{source_id}.json"
        path.write_text(json.dumps(asdict(state), indent=2))

    # ── Poll loop ────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._http = httpx.AsyncClient(timeout=30)
        self._load_sources()
        for sid in self._sources:
            self._states[sid] = self._load_state(sid)
        log.info("SourceRunner started with %d sources", len(self._sources))

        while self._running:
            now = time.time()
            for sid, src in list(self._sources.items()):
                if src.status != "active":
                    continue
                state = self._states.get(sid)
                if not state:
                    continue
                if now >= state.next_poll_at:
                    await self._poll_source(sid)
            await asyncio.sleep(30)

    async def _poll_source(self, source_id: str) -> None:
        src = self._sources.get(source_id)
        if not src:
            return
        state = self._states.get(source_id)
        if not state:
            state = SourceState()
            self._states[source_id] = state

        try:
            producer = self._get_producer(src.source_type)
        except ValueError as e:
            log.error("Unknown source type for %s: %s", source_id, e)
            return

        # Poll
        try:
            items = await producer.poll(src.config, since=state.last_poll_at or None)
        except Exception as e:
            state.error_count += 1
            log.warning("Poll failed for %s (attempt %d): %s", source_id, state.error_count, e)
            if state.error_count >= 5:
                src.status = "error"
                src.error_message = f"Failed {state.error_count} consecutive polls: {e}"
                self._save_sources()
            state.next_poll_at = time.time() + (src.interval_minutes * 60)
            self._save_state(source_id, state)
            return

        # Reset error count on success
        state.error_count = 0
        src.error_message = ""

        # Dedup
        seen_set = set(state.seen_ids)
        new_items = [item for item in items if item.id not in seen_set]

        # Digest + write
        written = 0
        for item in new_items:
            try:
                delta = producer.digest(item, src.config)
                # Scope the source to the configured instance. A producer that
                # returned its default source_type (e.g. "rss", "mastodon",
                # "homeassistant") gets narrowed to "<type>/<source_id>" so
                # individual feeds/accounts are independently addressable in
                # the lake. Producers that already scoped themselves (vault
                # returns "vault/<workspace>") are left alone.
                if delta.source == producer.source_type:
                    delta.source = f"{producer.source_type}/{source_id}"
                if src.expiry_days is not None:
                    delta.expires_at = self._compute_expiry(src.expiry_days)
                await self._write_delta(delta)
                seen_set.add(item.id)
                written += 1
            except Exception:
                log.warning("Failed to process item %s from %s", item.id, source_id, exc_info=True)

        # Update state
        state.last_poll_at = time.time()
        state.next_poll_at = time.time() + (src.interval_minutes * 60)
        state.delta_count += written
        state.seen_ids = list(seen_set)[-1000:]
        self._save_state(source_id, state)

        if new_items:
            log.info(
                "Source %s: polled %d items, %d new, %d written",
                source_id,
                len(items),
                len(new_items),
                written,
            )

    async def _write_delta(self, delta: ProducedDelta) -> str | None:
        if not self._http:
            return None

        # Always preserve image URLs in content — even when upload succeeded
        # and we have a media_hash. Two reasons: (1) downstream consumers
        # like the feed loop need a renderable URL handle without resolving
        # the hash, and (2) <img src="..."> tags can render external URLs
        # directly, while /v1/media/{hash} requires Authorization that
        # <img> can't pass. The hash is for archival; the URL is the
        # rendering handle.
        content = delta.content
        if delta.image_urls:
            img_lines = "\n".join(f"![image]({url})" for url in delta.image_urls)
            content = f"{content}\n\n{img_lines}"

        body: dict[str, Any] = {
            "content": content,
            "modality": "image" if delta.media_hash else delta.modality,
            "tags": delta.tags,
            "source": delta.source,
        }
        if delta.timestamp:
            body["timestamp"] = delta.timestamp
        if delta.expires_at:
            body["expires_at"] = delta.expires_at
        if delta.media_hash:
            body["media_hash"] = delta.media_hash

        headers: dict[str, str] = {}
        if self._delta_key:
            headers["X-API-Key"] = self._delta_key

        resp = await self._http.post(f"{self._delta_url}/deltas", json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data.get("id")

    def _compute_expiry(self, days: float) -> str:
        return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── CRUD ─────────────────────────────────────────────────────────

    def add_source(self, source_type: str, config: dict, **kwargs: Any) -> SourceConfig:
        producer = self._get_producer(source_type)
        errors = producer.validate_config(config)
        if errors:
            raise ValueError("; ".join(errors))

        custom_name = kwargs.get("name", "")
        if custom_name:
            source_id = slugify(custom_name)
        else:
            source_id = f"{source_type}-{uuid.uuid4().hex[:8]}"

        if source_id in self._sources:
            raise ValueError(f"Source '{source_id}' already exists")

        interval = kwargs.get("interval_minutes", _interval_to_minutes(producer.default_interval))
        expiry = kwargs.get("expiry_days", producer.default_expiry_days)

        sc = SourceConfig(
            id=source_id,
            source_type=source_type,
            name=custom_name or producer.display_name,
            config=config,
            interval_minutes=interval,
            expiry_days=expiry,
            status="active",
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._sources[source_id] = sc

        state = SourceState(next_poll_at=time.time())  # poll immediately
        self._states[source_id] = state

        self._save_sources()
        self._save_state(source_id, state)
        return sc

    def update_source(self, source_id: str, updates: dict) -> SourceConfig:
        sc = self._sources.get(source_id)
        if not sc:
            raise KeyError(f"Source not found: {source_id}")

        if "config" in updates:
            producer = self._get_producer(sc.source_type)
            errors = producer.validate_config(updates["config"])
            if errors:
                raise ValueError("; ".join(errors))
            sc.config = updates["config"]

        if "interval_minutes" in updates:
            sc.interval_minutes = updates["interval_minutes"]
        if "expiry_days" in updates:
            sc.expiry_days = updates["expiry_days"]

        self._save_sources()
        return sc

    def remove_source(self, source_id: str) -> None:
        self._sources.pop(source_id, None)
        self._states.pop(source_id, None)
        state_path = self._state_dir / f"{source_id}.json"
        if state_path.exists():
            state_path.unlink()
        self._save_sources()

    def pause_source(self, source_id: str) -> None:
        sc = self._sources.get(source_id)
        if sc:
            sc.status = "paused"
            self._save_sources()

    def resume_source(self, source_id: str) -> None:
        sc = self._sources.get(source_id)
        if sc:
            sc.status = "active"
            sc.error_message = ""
            state = self._states.get(source_id)
            if state:
                state.error_count = 0
                state.next_poll_at = time.time()
                self._save_state(source_id, state)
            self._save_sources()

    async def manual_poll(self, source_id: str) -> None:
        if source_id not in self._sources:
            raise KeyError(f"Source not found: {source_id}")
        task = asyncio.create_task(self._poll_source(source_id), name=f"manual-poll/{source_id}")
        self._poll_tasks.add(task)
        task.add_done_callback(self._poll_tasks.discard)

    # ── Query ────────────────────────────────────────────────────────

    def list_sources(self) -> list[dict[str, Any]]:
        results = []
        for sid, sc in self._sources.items():
            producer = self._get_producer(sc.source_type)
            state = self._states.get(sid, SourceState())
            results.append(self._to_api_shape(sc, state, producer))
        # Always include available types (multiple instances allowed)
        for stype in self._registry:
            p = self._get_producer(stype)
            results.append(self._available_shape(p))
        return results

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        sc = self._sources.get(source_id)
        if not sc:
            return None
        producer = self._get_producer(sc.source_type)
        state = self._states.get(source_id, SourceState())
        return self._to_api_shape(sc, state, producer)

    def list_available_types(self) -> list[dict[str, Any]]:
        return [self._available_shape(self._get_producer(st)) for st in self._registry]

    def _to_api_shape(
        self, sc: SourceConfig, state: SourceState, producer: SourceProducer
    ) -> dict[str, Any]:
        last_sync = (
            datetime.fromtimestamp(state.last_poll_at, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if state.last_poll_at
            else None
        )
        next_sync = (
            datetime.fromtimestamp(state.next_poll_at, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if state.next_poll_at and sc.status == "active"
            else None
        )
        return {
            "id": sc.id,
            "name": sc.name or producer.display_name,
            "description": producer.description,
            "version": producer.version,
            "author": producer.author,
            "source": producer.source_type,
            "source_type": sc.source_type,
            "config": sc.config,
            "status": sc.status,
            "lastSync": last_sync,
            "nextSync": next_sync,
            "deltaCount": state.delta_count,
            "pollInterval": _minutes_to_interval(sc.interval_minutes),
            "expiryDays": sc.expiry_days,
            "errorMessage": sc.error_message or None,
            "auth": {"type": producer.auth_type},
            "schedule": {
                "type": producer.schedule_type,
                "default_interval": producer.default_interval,
            },
            "digestion": producer.digestion,
            "tags": producer.default_tags(sc.config),
            "expiry": {
                "default_days": producer.default_expiry_days,
                "configurable": producer.expiry_configurable,
            },
        }

    def _available_shape(self, producer: SourceProducer) -> dict[str, Any]:
        return {
            "id": producer.source_type,
            "name": producer.display_name,
            "description": producer.description,
            "version": producer.version,
            "author": producer.author,
            "source": producer.source_type,
            "source_type": producer.source_type,
            "config": {},
            "status": "available",
            "lastSync": None,
            "nextSync": None,
            "deltaCount": None,
            "pollInterval": producer.default_interval,
            "expiryDays": producer.default_expiry_days,
            "errorMessage": None,
            "auth": {"type": producer.auth_type},
            "schedule": {
                "type": producer.schedule_type,
                "default_interval": producer.default_interval,
            },
            "digestion": producer.digestion,
            "tags": producer.default_tags({}),
            "expiry": {
                "default_days": producer.default_expiry_days,
                "configurable": producer.expiry_configurable,
            },
        }

    def stop(self) -> None:
        self._running = False
