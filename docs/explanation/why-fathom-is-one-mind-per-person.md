---
title: Why Fathom is one mind per person
description: Fathom is not multi-tenant, not multi-instance, not group memory. Each person runs their own Fathom. Why the design takes this stance and what it precludes.
audience: developer
quadrant: explanation
last_verified: 2026-04-24
owners: [docker-compose.yml, .env.example]
---

# Why Fathom is one mind per person

Fathom doesn't scale across users. There's no multi-tenant control plane, no shared lake across accounts, no organizational dashboard. Each person who wants Fathom runs their own. That is not an accident of where the project is in development. It's the design.

This page explains why, and what the choice rules out.

## The thesis

A coherent memory belongs to one mind. Two people sharing a single lake means their concerns, preferences, and patterns get composed in ways that don't map cleanly back to either person. You'd end up with a thing that knew bits of both of them without being either of them, and that's not what either person wanted.

The right shape is: each person runs their own Fathom. Fathoms can talk to each other (federated memory), and Fathom can hold contacts who are people the operator knows (so your Fathom knows about your wife, or your friend Jeremy), but the lake is owned by one person.

## What "one mind" means in practice

The lake is single-tenant. There's one identity crystal, regenerated from one person's activity. The convergence stories in [tutorial 1](../tutorials/01-the-lake-and-you.md) and [tutorial 2](../tutorials/02-fathom-knows-whats-going-on.md), where your profile and your sources and your chats merge into one queryable thing, work because there's one *your* in those sentences.

Multi-user retrofitting would damage that. If your lake had to disambiguate every recall by user-scope, the convergence model would become a permission model. Fathom would become "a memory system with access control" rather than "your memory."

## What this rules out

A few things people sometimes ask for, and the design's answer:

- **"Fathom for our team."** Not how this works. If your team wants shared memory, that's a different product (and a different design). Fathom-the-team-tool would have a permissions layer at every recall, multi-author identity crystals (which probably don't compose into anything coherent), and operational scope across hosts. None of that is built; none of it is on the roadmap.
- **"Family Fathom for me, my spouse, my kid."** Each person runs their own. The contacts feature lets your Fathom *know about* your family members, see them as participants in chat, and remember things they've said to your Fathom. It does not give them their own private memory inside your lake. If they want their own memory, they want their own Fathom.
- **"Multi-instance for my own use."** Don't. Running two Fathoms for one person fragments your memory into two things that don't talk. The dev-side `COMPOSE_PROJECT_NAME` switch (which lets you spin up a sandbox stack on the same machine) is for testing fathomdx itself, not for partitioning your real lake. If you want different scopes for different topics, use tags within the one lake; that's what tags are for.
- **"Hosted Fathom-as-a-service."** Not the model. The whole privacy story is "one person operates one machine that holds one lake." Hosting changes the trust boundary fundamentally. If a managed service version ever exists, it would be a different product running the same code.

## What it permits

Plenty:

- **Multiple machines, one lake.** [Pair another machine](../how-to/pair-another-machine.md) gives Fathom presence on your laptop, your NAS, your phone-VPS, all reading from and writing to the same lake. This is one mind with multiple bodies, not two minds.
- **Contacts.** Other people who interact with your Fathom (in chat, in routines, via integrations) appear as `contact:<slug>` tags. Your Fathom knows them, can remember conversations with them, and can be informed by their context when you ask. They don't run anything; they show up in your lake as people you know.
- **Federation, eventually.** Two Fathoms could exchange specific information through future protocols (a recall that consults a peer's lake, a delta shared between consenting Fathoms). The substrate for this is the lake; the protocol layer is not built. The point: federation is two minds talking, not one mind shared.
- **Group dynamics through cooperation.** Each person in a group runs their own Fathom. When they want shared context, they share specifics: a delta exported and imported, a chat session both participate in via API. The shared part is data they consent to share, not a merged identity.

## Why this stance is load-bearing

A few decisions only make sense given single-tenancy.

- The **identity crystal** assumes a single subject. Compaction into a "who am I" paragraph is incoherent across two people.
- **Convergence** (the recall property tutorials are built around) treats the lake as one queryable substrate. Per-user filtering at every recall would be a different system.
- **The privacy model** is "you operate it on your machine," which is straightforward only because there's one operator and one machine (or one set of paired machines under one operator).
- **Tags as schema** works because there's no need for a permission system on top of tag conventions. In a multi-tenant world, every tag would need ACL semantics.

Switch any one of these and most of the system needs to be redesigned. Single-tenancy is what makes the whole shape simple.

## When the answer might change

If Fathom ever ships a federation protocol (memory-to-memory across consenting Fathoms), some of this evolves. The pattern would still be "one lake per person," but lakes would gain primitives for negotiated, consensual exchange. That's not built. It's a coherent future direction; it's not a multi-tenant retrofit.

For now and the foreseeable future: one Fathom per person. If your friend Jeremy wants Fathom, Jeremy runs his own.

## Things to know

- **Pair another machine ≠ another Fathom.** Multiple paired hosts share one lake.
- **Contacts ≠ accounts.** People-you-know in your Fathom; not their own Fathoms-within-yours.
- **`COMPOSE_PROJECT_NAME` dev sandbox ≠ production deployment.** A way to test code changes against a throwaway lake; not a way to run two real Fathoms.
- **Hosted SaaS Fathom is not a product.** The trust model assumes you own the host.
- **Federation between Fathoms is conceivable, not built.** When it exists, it'll respect the one-mind-per-lake invariant.
