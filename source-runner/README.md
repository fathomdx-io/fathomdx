# Source Runner

Lightweight service that polls external data sources and writes deltas to the delta lake. Each source type is a Python class that implements `SourceProducer`. The runner handles scheduling, deduplication, and delta writes. All content structuring is programmatic (no LLM dependency).

## Architecture

```
source-runner (this container)
  |-- polls sources on interval
  |-- deduplicates via seen ID set
  |-- digest() structures content programmatically
  |-- writes deltas to delta-store
  `-- exposes API for dashboard management
```

Separate from recall-loop (sessions/chat) and delta-store (storage). Produces deltas, nothing else.

## Running

Part of the docker-compose stack:

```bash
podman compose up source-runner
```

Or standalone for development:

```bash
DELTA_STORE_URL=http://localhost:4246 \
PYTHONPATH=source-runner \
python3 -m uvicorn server:app --port 4260
```

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `DELTA_STORE_URL` | `http://localhost:4246` | Delta store HTTP endpoint |
| `DELTA_API_KEY` | | Delta store auth key |
| `DATA_DIR` | `/data` | Where sources.json and state files live |

## API

All endpoints prefixed with `/api/sources`. Dashboard proxies through `/sources/`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/sources` | List all sources (configured + available types) |
| GET | `/api/sources/types` | List available source types |
| POST | `/api/sources` | Create a source `{source_type, config, name?, interval_minutes?, expiry_days?}` |
| GET | `/api/sources/{id}` | Get single source detail |
| PUT | `/api/sources/{id}` | Update config, interval, or expiry |
| POST | `/api/sources/{id}/pause` | Pause polling |
| POST | `/api/sources/{id}/resume` | Resume polling |
| POST | `/api/sources/{id}/poll` | Trigger immediate poll |
| DELETE | `/api/sources/{id}` | Remove source (deltas stay in lake) |
| GET | `/health` | Health check |

## Data

Persisted in `DATA_DIR` (mounted as `data/source-runner/`):

- `sources.json` -- configured sources with their settings
- `source-state/{id}.json` -- per-source runtime state (last poll, seen IDs, delta count)

## Built-in sources

| Type | Description | Config |
|------|-------------|--------|
| `rss` | RSS/Atom feeds converted to markdown, images preserved | `{"feed": "https://..."}` |
| `mastodon` | Home timeline and notifications | `{"instance": "https://...", "token": "..."}` |
| `vault` | Obsidian vault watcher with chunking and diffs | `{"path": "/path/to/vault"}` |

> Home Assistant used to live here. It moved to a machine-source
> (fathom-agent plugin in consumer-fathom) because most HA installs
> are LAN-local and the cloud runner can't see them.

## Adding a new source type

1. Copy `sources/template.py` to `sources/my_source.py`
2. Set the class metadata (`source_type`, `display_name`, etc.)
3. Implement `poll()` -- fetch items, return `RawItem` list
4. Override `digest()` -- structure content as markdown with proper tags
5. Optionally override `validate_config()` -- validate user input
6. Register in `source_runner.py`:
   ```python
   def _register_builtins(self):
       from sources.my_source import MySourceProducer
       self._registry["my-source"] = MySourceProducer
   ```
7. Rebuild the container

The dashboard will automatically show the new source type with an "add feed" form.

## How it works

1. **Poll loop** runs every 30s, checks which sources are due
2. **poll()** fetches all items from the source (full list, every time)
3. **Dedup** filters out items already in the `seen_ids` set (capped at 1000)
4. **digest()** structures each new item into a `ProducedDelta` (programmatic, no LLM)
5. **Delta write** POSTs to delta-store with tags, source, timestamp, images
6. **State save** updates last_poll_at, next_poll_at, seen_ids, delta_count
