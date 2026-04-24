---
title: Why a session is a tag, not a table
description: Chat sessions in Fathom aren't first-class rows with memberships and permissions. They're just tags on deltas. Here's why that choice pays off.
audience: developer
quadrant: explanation
last_verified: 2026-04-24
owners: [api/chat_listener.py, ../CLAUDE.md]
---

# Why a session is a tag, not a table

In most chat systems, a session (or conversation, or thread) is a concrete object. There's a `sessions` table. It has an ID, a title, a creator, a created_at, maybe a participant list. Messages belong to a session by way of a foreign key. Joining a session means adding a row to `session_members`. Leaving means deleting that row.

Fathom has none of that. A session is a tag.

Specifically: a chat session is the tag `chat:<slug>`, and the session itself is the timestream of every delta that carries that tag. Anyone who writes a delta tagged `chat:fluid-ideas` is, by that act, participating in the `fluid-ideas` session. There is no session row. There is no membership table. There is no central coordinator deciding who's allowed in.

This looks like a shortcut until you see what it buys.

## What the tag-session approach gives you

**No central authority.** A routine running on your laptop can write into a session without asking a server for permission. A Claude Code hook can, too. So can a browser extension, a voice assistant, a sensor. Each writer authenticates once against the lake, and participation in a session is as simple as including the right tag on the delta they write. Federated writers with no coordination overhead.

**Extensibility without schema migration.** When a new kind of writer joins, nothing in the schema changes. A new plugin that wants to participate in `chat:home-automation` just adds the tag. The session absorbs it automatically. No new `plugin_session_access` table, no new foreign key, no migration.

**Sessions compose with everything else.** Because a session is just a tag filter, any other filter composes with it. "Everything in `chat:fluid-ideas` from this week" is a tag filter plus a time filter. "Everything in `chat:fluid-ideas` written by Myra" is a tag filter plus a contact filter. "Everything in `chat:fluid-ideas` semantically similar to 'singularities'" is a tag filter plus a semantic query. No API surface has to exist for each combination; they all fall out of the same recall primitives.

**History is the session.** There is no separate log to keep in sync. The session and the list of deltas tagged `chat:<slug>` are literally the same thing. You can't have a "session exists but messages got purged" inconsistency, because the session's existence is the existence of its deltas.

## The trade-offs

Two things get harder.

**Hard delete is not a thing.** You cannot make a session go away. You can write a `chat-deleted` tombstone delta, and clients can filter it out, but the underlying deltas still exist unless you explicitly delete them by ID. This is a feature for a memory system: accidental deletion shouldn't be easy. For a chat product it would be a bug.

**You don't "own" a session.** There is no creator field. The first person to write into `chat:fluid-ideas` doesn't own it any more than the second. For Fathom this is fine because there's one operator per lake. For a multi-tenant chat product this model would need a layer on top.

Both trade-offs are acceptable because Fathom's use case is *personal memory augmented by agents,* not group chat.

## What the tag vocabulary looks like in practice

A chat turn written by a user lands in the lake with tags like:

```
["chat", "fathom-chat", "chat:jovial-exhausted-rabbit",
 "participant:user", "contact:myra"]
```

An assistant reply from the same session:

```
["chat", "fathom-chat", "chat:jovial-exhausted-rabbit",
 "participant:fathom", "assistant"]
```

A rename event:

```
["chat-name", "chat:jovial-exhausted-rabbit"]
```

Retrieving "the session" is `SELECT * FROM deltas WHERE 'chat:jovial-exhausted-rabbit' = ANY(tags) ORDER BY created_at`. That's it. Members fall out of the deltas' `participant:*` tags. The session title is the most recent `chat-name` delta's content.

## Why this pattern generalizes

"It's a tag, not a table" applies beyond chat.

- A routine isn't a scheduled job with a row in a `routines` table. It's a tag (`routine-id:<id>`), and the routine-and-its-history is every delta carrying that tag.
- A contact isn't always a row in a contacts table (though one can exist for display metadata). It's primarily a tag (`contact:<handle>`).
- A source isn't a record in a sources table. It's a tag convention plus a plugin that writes deltas with that tag.

Every one of these would have been an obvious table in a traditional design. Making them tags keeps the lake's shape uniform and its query surface composable. The trade-off is the same trade-off every time: you give up enforced structure and gain free composition.

## Membership, implicitly

The question "who's in this session?" has a simple answer under the tag model: whoever has ever written a delta tagged with the session. Once you've participated, you're implicitly a member forever, and there are no membership tombstones. This sounds like a bug until you remember that Fathom's chat is more like a persistent thread you can return to than a room you can be kicked from. The deltas are the thread. If you've ever spoken into it, you've marked yourself as a party to it.

If a feature ever needs explicit "left the session" semantics, it would be built as another tag convention, not a schema change. That is the pattern: when you need more structure, you add a tag, not a table.
