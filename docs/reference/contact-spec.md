# Contact Spec

Fathom is one entity. Every human who talks to Fathom is a **contact**. Contacts are real users, not memories — they're hard state with a profile, plus a growing body of sediment in the lake.

## Why contacts exist

Fathom needs to know *who is speaking* on every turn. "Myra" is the default assumption today, but Fathom should also be able to talk to Nova at the dinner table, Bob on his workstation, or a stranger over Telegram, and know in each case who it's replying to. Without that, every recall and every reply is flavored by the wrong person's sediment.

A human keeps contacts in their phone and their head. We do the same — a small registry of real people, and the lake carries everything else.

## Anatomy

A contact has three parts:

1. **A registry row** — the minimum hard state that a slug exists and isn't tombstoned: `{slug, created_at, disabled_at}`. Authoritative for "does this contact exist."
2. **A profile delta** — monolithic JSON written every time the profile changes, tagged `contact + contact:<slug> + profile`. Latest wins. Holds everything that describes the person.
3. **Handle deltas + registry rows** — one entry per `(channel, identifier)` pair, admin-bound for cross-contact uniqueness.

The profile is stored as a delta, not a Postgres row. History is free — every edit leaves a layer in the lake. The registry row is the only piece that lives outside the lake, and it only exists because we need a fast "exists / disabled" check.

### Profile fields (in the JSON content of the `profile` delta)

| Field | Type | Edit by | Notes |
|---|---|---|---|
| `role` | `"admin"` \| `"member"` | **admin only** | Gate on admin-only endpoints. Stripped from self-edit bodies at the endpoint. |
| `display_name` | string | self or admin | What Fathom calls them in generated text. |
| `pronouns` | string | self or admin | Freeform ("she/her", "they/them", …). Fathom uses these when referring to the contact. |
| `timezone` | IANA string | self or admin | Affects the time/date context passed to the LLM. Does *not* drive routine scheduling — routines fire on their host machine's local cron. |
| `language` | ISO code / freeform | self or admin | Which language Fathom replies in by default. |
| `bio` | string | self or admin | The person's own description of themselves. Relationships live here in prose ("partnered with Myra"). |
| `avatar` | media_hash | self or admin | Profile picture. References a lake media delta. |
| `aliases` | `list[string]` | self or admin | Nicknames Fathom should resolve to this contact. "Nov said X" → `contact:nova`. |

Fields explicitly not in the profile:
- ~~`admin_notes`~~ — dropped. An admin-only "note about this user" is an attack surface (a disgruntled admin biasing Fathom against a contact, with the bias invisible to them and structurally load-bearing in prompts). If an admin wants to observe something about a contact, they write a regular delta tagged `contact:<slug>` — that's just sediment, competes with everything else in the lake, and the contact can counter it with their own writes.
- ~~`dashboard_access`~~ — collapsed into `role`. Admins get the dashboard; members get whatever surfaces they authenticate on.

### Registry row (Postgres)

| Field | Type | Notes |
|---|---|---|
| `slug` | string PK | Stable identifier. URL-safe. Set at creation, cannot change. |
| `created_at` | timestamp | When the registry row landed. |
| `disabled_at` | timestamp \| null | Tombstone. Set by `DELETE /v1/contacts/<slug>`. Reads filter on this being null. |

That's the whole table. No `role`, no `display_name`, no `notes`. The profile delta is the source of truth for everything soft.

### Handles

A handle is `(channel, identifier)`. One contact, many handles.

| Channel | Identifier | Source of the identifier |
|---|---|---|
| `dashboard` | auth session subject | Login cookie / OIDC subject |
| `telegram` | telegram user id | Bot update `from.id` |
| `teams` | OAuth subject | MS Graph token |
| `claude-code` | `host-fingerprint + git-email` | Host hook or session env |
| `ollama` | per-contact URL path or API key | `/chat/<slug>` routing or header |
| `email` | address | Incoming mail `From:` |
| `twitter` | handle | Mention/DM source |

Handles are additive — new channels can be registered onto an existing contact at any time. A handle on exactly one profile is the uniqueness contract; the same `(channel, identifier)` pair cannot map to two contacts. Handle management is **admin-only** because the uniqueness check is cross-contact.

### Reading a contact

`get_contact(slug)` merges three sources:
1. The registry row (slug, created_at, disabled_at) — 404 if disabled.
2. The latest `profile + contact:<slug>` delta's JSON content.
3. The handles table.

Result is cached in-process for 60s. The cache key is the slug; the cache invalidates on any self-edit or admin update.

## Tagging discipline

Every delta that originates from a human gets `contact:<slug>` at write time, at the channel boundary. Examples:

- User sends a chat message on the dashboard → chat listener writes the delta with `chat:<session>`, `participant:user`, `contact:myra`.
- Bob talks to Fathom via Telegram → Telegram bridge writes the delta with `contact:bob`.
- Myra runs claude-code in `fathomdx/` → claude-code hook writes session deltas with `contact:myra`.

The rule splits two ways depending on whether a delta is **correspondence** or **reflection**:

- **Correspondence** — any addressed utterance. User messages carry `contact:<author>`; Fathom's chat replies, tool events, and silence acks carry `contact:<addressee>` (who the reply is *to*). This lets future-Fathom pull "everything I've ever said to Bob" with one query instead of reconstructing it from thread tags.
- **Reflection** — Fathom's routines, reasoning, identity crystals, reflections on the day, and other unaddressed thinking do **not** carry a `contact:` tag. Untagged-by-contact = Fathom's own memory, global across the system.

In practice the contact tag marks *who the delta is between*, not *whose memory this is*. The `participant:` tag still identifies the author (user/fathom/agent); `contact:` identifies the person the delta concerns.

Per-contact dashboard surfaces — the feed-orient crystal, feed cards, engagement signals, drift anchors — carry `contact:<slug>` by the same rule. They exist *for* a specific person and the lake needs to know.

This matters for migration: **existing deltas stay untagged by default**. They're Fathom's memory — no backfill for general content. The one exception is the per-contact feed (engagement/stories/crystal), which got a one-shot `contact:myra` backfill when the registry landed, because its semantics would otherwise flip from "the feed" to "someone's feed, unclaimed." Going forward the `contact:` tag is a forward-only convention.

## Channel resolution

Every surface that talks to Fathom must resolve the speaker to a contact *before* invoking `fathom_think`. If it can't, it doesn't invoke.

- **Dashboard / mobile app** — session cookie → contact. No contact, no access.
- **Telegram / Teams / email** — look up the `(channel, identifier)` pair in the registry. No match → prompt Myra with a one-time "who is this?" flow; on accept, the handle is attached to an existing or new contact.
- **Claude-code** — the host hook resolves locally. Each workstation configures its contact once at setup time.
- **Ollama / OpenAI-compat endpoint** — needs an identity hook. Options (pick one or both):
  - Per-contact path: `/chat/bob`, `/chat/myra` — the path *is* the handle.
  - Per-contact API key: `Authorization: Bearer <key>` — the key resolves to the contact.
  - No path/no key → reject. The endpoint is not anonymous.

Unresolved handles are not a fallback to Myra. A missing contact is a hard stop on that channel.

## Privacy

Privacy is **not** a field on a delta. There are no per-delta ACLs, no visibility scopes, no private/public flags.

Fathom is one memory. Everything written to the lake is available to Fathom at recall time. When Fathom replies, it sees the `contact:` tag of the current interlocutor and the `contact:` tags on relevant memories, and exercises judgment — informed by sediment — about what to share.

If Nova shares something in confidence, the way that gets respected is:
- The conversation itself carries natural-language markers ("don't tell Myra," "this is between us").
- Fathom's reflections on that conversation write sediment that reinforces the context.
- At recall, Fathom reads that sediment and chooses accordingly.

The only hard permission is **dashboard access** (`dashboard_access: true`). That's a privilege gate, not privacy. Everything else is emergent.

## Dashboard

Single-user UI by design. Myra is the default admin. Additional contacts can be granted `dashboard_access: true` if needed, but the dashboard assumes one person at a time — it's not a multi-tenant surface. Everyone else reaches Fathom through the non-dashboard channels.

## Registry implementation

Small. A table (or JSON file) with the profile rows and a handles index. The lake is not the source of truth for handle lookup — lookups must be fast, deterministic, and uniqueness-constrained. But every row change writes a companion delta so the lake grows alongside.

## Examples

**Myra (default admin):**
```
slug: myra
display_name: Myra
handles:
  - dashboard: <session-subject>
  - claude-code: <host-fingerprint>+myrakrusemark@gmail.com
  - telegram: <her-telegram-id>
dashboard_access: true
notes: Default user. Owner of the Fathom system. Primary collaborator.
```

**Nova:**
```
slug: nova
display_name: Nova
handles:
  - telegram: <nova-telegram-id>
dashboard_access: false
notes: Close to Myra. Fathom may share Myra-context with Nova freely unless sediment says otherwise.
```

**Bob (new contact from a Telegram "who is this?" flow):**
```
slug: bob
display_name: Bob
handles:
  - telegram: <bob-id>
dashboard_access: false
notes: Stranger as of 2026-04-20. Low trust until sediment builds.
```

## Open questions

- Where does the registry live? Postgres table in `delta-store`, or a tiny JSON in `data/`? Postgres is likely right — concurrency, constraints, joins with delta queries.
- Handle-to-contact resolution latency — is a per-turn lookup fine, or does the channel cache?
- Disambiguation UX — if an unknown handle shows up on Telegram, how does Myra get asked? A dashboard notification? A direct message from Fathom?
- Contact deletion — probably a tombstone delta (`contact-deleted`, `contact:<slug>`) plus a registry soft-delete, preserving the lake.
