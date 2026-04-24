---
title: How to add a feed source
description: Wire an RSS or Atom feed into your lake. Items are converted to markdown deltas and polled on a schedule. About two minutes per feed.
audience: developer
quadrant: how-to
last_verified: 2026-04-24
owners: [source-runner/sources/rss.py, api/routes/sources.py]
---

# How to add a feed source

Adding an RSS or Atom feed makes everything in that feed searchable alongside the rest of your lake. New items become deltas, with the article's text converted to markdown, images extracted, and tags applied. Polling happens on a schedule (default 30 minutes); you don't refresh anything yourself.

## Prerequisites

- A running Fathom stack.
- An RSS or Atom feed URL. Most blogs, news sites, GitHub releases, and Reddit subreddits expose one.

## Step 1: Open the Sources page

In the dashboard sidebar, find **Sources**. Click **Add source**. The dialog lists the source types this Fathom instance knows about. RSS is one of them.

## Step 2: Configure the feed

Pick **RSS**. You'll be asked for:

| Field | What to put |
|---|---|
| **Name** | Any human-readable label. Shown in the dashboard. |
| **Feed URL** | The full URL to the feed. Trailing slashes don't matter. |
| **Poll interval** | How often to check for new items. `30m` is the default and a good starting point. Options: `1m`, `5m`, `15m`, `30m`, `1h`, `6h`, `daily`. |
| **Expiry days** | How long to keep feed items. `30` by default, configurable. Past that, individual items can be reaped while their summaries persist. |

Save. The source-runner picks it up on its next loop and starts polling.

## Step 3: Verify items are landing

Wait a poll cycle (about 30 minutes if you used the default), or trigger a poll manually from the source's row in the dashboard. Then:

```bash
curl 'http://localhost:8201/v1/deltas?source=rss&limit=5'
```

You should see deltas with markdown content, tags including `rss` and `feed:<url>`, and timestamps from the original feed (not the time you saved the source).

If the feed had images, they're stored as image moments referenced from the deltas. The dashboard renders them inline.

## Step 4: Use it from chat

Now ask Fathom about something the feed would have:

> Anything new about <topic the feed covers>?

Fathom searches the lake, finds matching feed items, and answers using them. Cross-source queries work the same way: tell Fathom about a project of yours and ask what's relevant from the feed.

This is the convergence pattern from [tutorial 1](../tutorials/01-the-lake-and-you.md), formalized as a how-to.

## Polling behavior

- The runner deduplicates by item ID, so re-fetching the same feed doesn't create duplicate deltas.
- Items written before the source was added will appear on first poll if they're still in the feed (the runner pulls everything the feed currently exposes; old items past the publisher's window are gone).
- A feed that returns an empty body or a non-200 status is logged and retried at the next interval.

## Other sources besides RSS

The same dashboard flow handles other source types if your Fathom build has them registered:

- **Mastodon.** Polls a Mastodon account's posts.
- **Vault.** Watches an Obsidian vault on a paired agent host. Each note change becomes a delta.
- **Home Assistant.** Bridges a Home Assistant instance. Sensor and state changes become deltas.

What's available depends on what's in `source-runner/sources/` and what's enabled. Sources requiring agent presence (vault, Home Assistant) only show up after you've paired an agent on the right machine.

## Tag conventions for filtering

Every feed delta carries:

- `source:rss` (or whatever source type).
- `feed:<url>` for the specific feed.
- `rss` or other source-name shortcut for general filtering.
- Topic tags from the publisher if available.

To search just one feed:

```bash
curl 'http://localhost:8201/v1/deltas?tags_include=feed:https://example.com/rss.xml&limit=10'
```

To search all feeds across all sources:

```bash
curl 'http://localhost:8201/v1/deltas?tags_include=feed&limit=10'
```

## Troubleshooting

- **No items after a full poll cycle.** The feed URL might be wrong, return non-200, or be empty. Check `docker compose logs source-runner --tail 50` for the poll attempt and its result.
- **Items appear with broken markdown.** The HTML-to-markdown conversion preserves most structure, but some publishers wrap content in unusual ways. Check the raw `content` field via the API; if the source is fine and the conversion is the problem, it's worth filing.
- **Images aren't rendering in the dashboard.** Image extraction happens during `digest()`. If the feed serves images behind auth or hotlink protection, they fail silently and the delta has no `image_path`. The text still lands.
- **Polling seems too aggressive.** Increase the interval. Frequent polls of a high-volume feed can fill the lake fast.
- **Polling seems too quiet.** Decrease the interval. The default 30m is conservative for casual feeds; a busy news source can warrant 5m or 15m.

## Removing a feed

In the dashboard's Sources page, the feed has a remove action. Confirming it stops future polling and tombstones the source. Past deltas from the feed stay in the lake; if you want them gone, see [delete a delta or tag](./delete-a-delta-or-tag.md).
