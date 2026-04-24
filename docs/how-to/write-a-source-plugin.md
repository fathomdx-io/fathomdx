---
title: How to write a source plugin
description: Add a new source type to fathomdx by extending SourceProducer. Implement poll() to fetch items and digest() to shape them into deltas.
audience: developer
quadrant: how-to
last_verified: 2026-04-24
owners: [source-runner/sources/, source-runner/source_runner.py]
---

# How to write a source plugin

A source plugin is a Python class that knows how to fetch items from somewhere and shape them into deltas. The source-runner handles polling, deduplication, and writing to the lake. You implement the parts specific to your data source.

This page walks through the four-step workflow the runner expects.

## Prerequisites

- A running Fathom stack.
- A clone of the fathomdx repo.
- Python 3.11+ familiarity. The runner uses async/await for I/O.

## Step 1: Copy the template

The runner ships with a working starting point at `source-runner/sources/template.py`. Copy it under a new name:

```bash
cp source-runner/sources/template.py source-runner/sources/my_source.py
```

The template has every required method, default values for every metadata field, and inline comments explaining each one. Read through it once before editing.

## Step 2: Set the metadata

At the top of the new class, fill in the identifying fields:

```python
class MySourceProducer(SourceProducer):
    source_type = "my-source"        # unique ID, lowercase, kebab-case
    display_name = "My Source"       # what shows in the dashboard
    description = "What this source pulls in"
    version = "0.1.0"
    author = "you"

    auth_type = "none"               # none | oauth2 | api_key | file
    schedule_type = "poll"           # poll | watch | import
    default_interval = "30m"         # 1m, 5m, 15m, 30m, 1h, 6h, daily
    digestion = "raw"                # raw | llm
    default_expiry_days = 30         # None to keep forever
    expiry_configurable = True
```

`source_type` is the stable ID. Once a source has been added by users, never change it. Renames are migrations.

## Step 3: Implement poll()

`poll()` is where you fetch items. It receives the user's config (from the dashboard form) and the timestamp of the last successful poll. Return a list of `RawItem`. Always return everything the source currently exposes; the runner deduplicates against previously-seen IDs.

```python
async def poll(self, config: dict, since: float | None = None) -> list[RawItem]:
    url = config["url"]
    items: list[RawItem] = []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        for entry in resp.json():
            items.append(RawItem(
                id=entry["id"],            # unique within this source
                content=entry["text"],     # raw payload
                timestamp=entry.get("date"),
                title=entry.get("title", ""),
                url=entry.get("url"),
                image_urls=entry.get("images", []),
            ))
    return items
```

Return an empty list when there's nothing new (or when the fetch fails non-fatally). Raise an exception only when something is genuinely wrong; the runner catches it and surfaces it in the dashboard.

## Step 4: Implement digest()

`digest()` shapes a `RawItem` into a `ProducedDelta`. This is where you decide tags, format the content, and pass through any image URLs.

```python
def digest(self, item: RawItem, config: dict | None = None) -> ProducedDelta:
    return ProducedDelta(
        content=f"{item.title}\n\n{item.content}" if item.title else item.content,
        tags=[self.source_type, "feed", f"feed:{config.get('url', '')}"],
        source=self.source_type,
        timestamp=item.timestamp,
        image_urls=item.image_urls,
    )
```

The default tag `[self.source_type]` is fine for simple cases. Add tags that make later filtering easy: a feed-specific tag, a topic tag, a contact tag if relevant.

If `digestion` is set to `llm`, the runner offers users an opt-in summarization step. Most plugins should leave this `raw` and let users compose summarization separately if they want it.

## Step 5: Register the plugin

Open `source-runner/source_runner.py` and find `_register_builtins()`. Add an import and a registry entry:

```python
def _register_builtins(self):
    from sources.rss import RSSProducer
    from sources.mastodon import MastodonProducer
    from sources.vault import VaultProducer
    from sources.my_source import MySourceProducer  # add this

    self._registry["rss"] = RSSProducer
    self._registry["mastodon"] = MastodonProducer
    self._registry["vault"] = VaultProducer
    self._registry["my-source"] = MySourceProducer  # and this
```

The key in `_registry` must match `source_type` exactly.

## Step 6: Rebuild and test

```bash
docker compose build source-runner
docker compose up -d source-runner
```

The new source type appears in the dashboard's "Add source" dialog. Configure one with test values and watch it poll:

```bash
docker compose logs source-runner -f
```

You'll see one log line per poll cycle, and one entry per item digested. Once items have landed:

```bash
curl 'http://localhost:8201/v1/deltas?source=my-source&limit=5'
```

If they show up with the right shape, the plugin is working.

## Optional: validate_config()

To reject bad config before it's saved, override `validate_config()`:

```python
def validate_config(self, config: dict) -> list[str]:
    errors = []
    if not config.get("url", "").startswith("https://"):
        errors.append("URL must use https.")
    return errors
```

The dashboard surfaces these errors in the form.

## Optional: extract images

If your source has image URLs, the helpers in `sources/base.py` will fetch and store them as image moments. See `RSSProducer._entry_content()` for the pattern (around `extract_images()`).

## Things to know

- **`poll()` returns everything; the runner deduplicates.** Don't try to track "last seen" yourself unless the source's API is fundamentally pull-since. The runner does it for you.
- **`source_type` is a permanent identifier.** Renames are user-data migrations. Pick a good name on day one.
- **Async all the way down.** Use `httpx.AsyncClient`, `asyncio.to_thread` for sync libs, never `requests`. The runner is a single event loop.
- **Errors during poll are recoverable by default.** Log and return `[]`. The runner retries on the next interval. Raise only for genuinely unrecoverable conditions.
- **Tags shape recall, not just display.** Be deliberate. The reserved tag conventions in [reserved-tags-spec.md](../reference/reserved-tags-spec.md) apply here.
- **Authentication.** `auth_type` is metadata only. The actual auth flow lives in your `poll()` body using `config` values the user set up (API key, OAuth token, file path).

## Sharing a plugin

If your source is generally useful (a popular service, a common log format, a frequently-imported file type), open a PR against `fathomdx-io/fathomdx` adding it under `source-runner/sources/`. Plugins that ship with the build are available to every Fathom user without extra setup.

For private sources, keep the file in your fork or in a separate sidecar repo and rebuild the source-runner image when you want to update it.
