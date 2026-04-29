"""Channels — how Fathom routes outputs to and from external surfaces.

A channel is just a tag pair: `channel:<name>` on inbound intents,
`to:<name>:<correlation>` on the witness output that addresses them.
The correlation is whatever the channel uses to identify a conversation
(an OpenAI session id, a Telegram chat id, a feed lane name).

There is no "channel daemon" abstraction here. Inbound writes go through
`write_intent` directly; outbound delivery is the channel's own
responsibility — the OpenAI endpoint polls the lake itself, a future
Telegram bridge would have its own watcher.

What this module IS: the canonical naming + helpers + per-channel
renderer that converts a witness card payload into the body shape the
channel's consumer wants. Centralizing it here so adding a new channel
means one entry, not a tag-format-string scattered across four files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# ── Tag conventions ────────────────────────────────────────────────────


def channel_tag(name: str) -> str:
    """`channel:<name>` — stamped on inbound intents to declare origin."""
    return f"channel:{name}"


def correlation_tag(channel: str, correlation: str) -> str:
    """`<channel>-session:<id>` — the channel-specific conversation key.

    Kept channel-prefixed (rather than a generic `correlation:`) so
    queries like "all openai sessions" stay one tag-prefix scan and
    don't collide across channels.
    """
    return f"{channel}-session:{correlation}"


def address_tag(channel: str, correlation: str) -> str:
    """`to:<channel>:<correlation>` — stamped on witness outputs to mark
    where they should be delivered. The channel's consumer polls on this."""
    return f"to:{channel}:{correlation}"


def extract_channel(tags: list[str]) -> tuple[str, str]:
    """Pull (channel, correlation) off a delta's tags. Returns ("", "")
    for ambient (channel-less) deltas."""
    channel = ""
    for t in tags:
        if t.startswith("channel:"):
            channel = t.split(":", 1)[1]
            break
    if not channel:
        return "", ""
    prefix = f"{channel}-session:"
    for t in tags:
        if t.startswith(prefix):
            return channel, t[len(prefix):]
    return channel, ""


# ── Renderers ──────────────────────────────────────────────────────────


def _render_openai(payload: dict) -> str:
    """Witness card → assistant message body for OpenAI clients.

    The OpenAI surface is text-only — the kicker/title/tail/links/image
    fields the witness produces for feed cards are dropped. If the body
    is empty we return empty rather than synthesizing one from the
    title; an empty body is a meaningful signal (the loop chose silence)
    and the endpoint surfaces it as a stop-with-empty-content.
    """
    return (payload.get("body") or "").strip()


def _render_feed(payload: dict) -> str:
    """Feed renderer — the feed reads the JSON payload directly, so this
    is identity. Present for symmetry; the dashboard doesn't call it."""
    import json
    return json.dumps(payload, ensure_ascii=False)


def _render_claude_code(payload: dict) -> str:
    """Witness card → prompt body for an agent-side claude-code consumer.

    Same shape as openai — text-only, body-or-empty. The channel-level
    contract is just "the task body Fathom wants run." The agent-side
    consumer (kitty plugin today) is responsible for wrapping with a
    correlation marker and appending a closure-instruction footer at
    injection time, since closure mechanics differ between agents
    (kitty injects + waits; a hypothetical headless runner would do
    something else).
    """
    return (payload.get("body") or "").strip()


@dataclass(frozen=True)
class Channel:
    name: str
    render: Callable[[dict], str]


REGISTRY: dict[str, Channel] = {
    "openai":      Channel(name="openai",      render=_render_openai),
    "feed":        Channel(name="feed",        render=_render_feed),
    "claude-code": Channel(name="claude-code", render=_render_claude_code),
}


def get(name: str) -> Channel | None:
    return REGISTRY.get(name)
