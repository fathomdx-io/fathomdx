"""Mastodon source producer.

Polls home timeline and/or notifications from a Mastodon instance.
Auth: personal access token (Bearer token), no OAuth redirect flow needed.
"""

from __future__ import annotations

import logging

import httpx

from .base import ProducedDelta, RawItem, SourceProducer, convert_html, extract_images

log = logging.getLogger("source.mastodon")


def _md(html: str) -> str:
    """Convert Mastodon post HTML to markdown."""
    content, _ = convert_html(html)
    return content


class MastodonProducer(SourceProducer):
    source_type = "mastodon"
    display_name = "Mastodon"
    description = "Home timeline and notifications"
    version = "0.1.0"
    author = "fathom"
    auth_type = "api_key"
    schedule_type = "poll"
    default_interval = "15m"
    digestion = "raw"
    default_expiry_days = 30
    expiry_configurable = True

    async def poll(self, config: dict, since: float | None = None) -> list[RawItem]:
        instance = config.get("instance", "").rstrip("/")
        token = config.get("token", "")
        feeds = config.get("feeds", ["home"])

        if not instance or not token:
            return []

        headers = {"Authorization": f"Bearer {token}"}
        items: list[RawItem] = []

        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            if "home" in feeds:
                items.extend(await self._poll_timeline(client, instance))
            if "notifications" in feeds:
                items.extend(await self._poll_notifications(client, instance))

        return items

    async def _poll_timeline(self, client: httpx.AsyncClient, instance: str) -> list[RawItem]:
        items: list[RawItem] = []
        try:
            resp = await client.get(f"{instance}/api/v1/timelines/home", params={"limit": "30"})
            resp.raise_for_status()
            for post in resp.json():
                content = _md(post.get("content", ""))
                acct = post.get("account", {}).get("acct", "?")
                display = post.get("account", {}).get("display_name", acct)
                media = post.get("media_attachments", [])
                image_urls = [m["url"] for m in media if m.get("type") == "image"]

                text = f"@{acct} ({display}):\n{content}"
                if post.get("reblog"):
                    reblog = post["reblog"]
                    orig_acct = reblog.get("account", {}).get("acct", "?")
                    orig_content = _md(reblog.get("content", ""))
                    text = f"@{acct} boosted @{orig_acct}:\n{orig_content}"
                    image_urls = [
                        m["url"]
                        for m in reblog.get("media_attachments", [])
                        if m.get("type") == "image"
                    ]

                media_hash = (
                    await extract_images(image_urls, http_client=client)
                    if image_urls
                    else None
                )

                items.append(
                    RawItem(
                        id=post["id"],
                        content=text,
                        timestamp=post.get("created_at"),
                        title=f"@{acct}",
                        url=post.get("url"),
                        image_urls=image_urls,
                        meta={"media_hash": media_hash} if media_hash else {},
                    )
                )
        except Exception:
            log.warning("Failed to fetch home timeline", exc_info=True)
        return items

    async def _poll_notifications(self, client: httpx.AsyncClient, instance: str) -> list[RawItem]:
        items: list[RawItem] = []
        try:
            resp = await client.get(f"{instance}/api/v1/notifications", params={"limit": "30"})
            resp.raise_for_status()
            for notif in resp.json():
                ntype = notif["type"]
                acct = notif.get("account", {}).get("acct", "?")
                status = notif.get("status")

                if ntype == "mention" and status:
                    content = _md(status.get("content", ""))
                    text = f"@{acct} mentioned you: {content}"
                elif ntype == "favourite" and status:
                    content = _md(status.get("content", ""))[:80]
                    text = f"@{acct} favorited: {content}"
                elif ntype == "reblog" and status:
                    content = _md(status.get("content", ""))[:80]
                    text = f"@{acct} boosted: {content}"
                elif ntype == "follow":
                    text = f"@{acct} followed you"
                else:
                    text = f"@{acct}: {ntype}"

                items.append(
                    RawItem(
                        id=f"notif-{notif['id']}",
                        content=text,
                        timestamp=notif.get("created_at"),
                        title=f"@{acct}",
                        url=status.get("url") if status else None,
                    )
                )
        except Exception:
            log.warning("Failed to fetch notifications", exc_info=True)
        return items

    def digest(self, item: RawItem, config: dict | None = None) -> ProducedDelta:
        return ProducedDelta(
            content=item.content,
            tags=["mastodon", "social"],
            source="mastodon",
            timestamp=item.timestamp,
            image_urls=item.image_urls,
            media_hash=item.meta.get("media_hash"),
        )

    def default_tags(self, config: dict) -> list[str]:
        return ["mastodon", "social"]

    def validate_config(self, config: dict) -> list[str]:
        errors = []
        if not config.get("instance"):
            errors.append("'instance' is required (e.g. https://mastodon.social)")
        if not config.get("token"):
            errors.append("'token' is required (access token)")
        return errors
