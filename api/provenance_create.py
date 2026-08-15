"""The single validated path that creates a provenance node.

Two surfaces create provenance and they MUST go through the same
gate: the Grand Loop harness (`tool_propose_provenance`) and the
MCP/CLI-facing `POST /v1/provenance` endpoint an episodic agent calls
to crystallize provenance on an event, in its own voice, without the
loop running. This module holds the shared logic so neither surface
can drift into a costume version of provenance.

What "the real path" means here:

  1. Every constituent id is resolved against the lake — a model
     that hallucinates ids in recall's display format can't poison
     the hierarchy with `from:*` pointers to nonexistent deltas.
  2. The level is floored at `max(child levels) + 1` — a provenance
     always sits at or above its children (an era can't be filed
     under an episode).
  3. A per-level minimum-constituent count keeps nodes from being
     minted over a single delta.
  4. The node is drafted as a `kind:proposal tool:provenance` card
     and then auto-approved through the policy path — which is what
     writes the real `kind:provenance` delta whose
     `provenance_embedding` is the CENTROID of its constituents
     (walked to base moments), not the embedding of its own
     title+summary text. That centroid is what makes provenance
     findable by SUBSTANCE, not just by its label.

`ProvenanceValidationError` carries a caller-fixable message (bad
ids, wrong level, too few constituents). The harness returns it to
the model verbatim; the HTTP endpoint maps it to a 400.
`ProvenanceWriteError` is an infra failure on the proposal write —
the harness surfaces it as an error string, the endpoint as a 500.
"""

from __future__ import annotations

import json

from . import delta_client


class ProvenanceValidationError(ValueError):
    """A caller-fixable problem with a provenance request. The message
    is safe to show a model or return as an HTTP 400 detail."""


class ProvenanceWriteError(RuntimeError):
    """The proposal card failed to land in the lake — an infra fault,
    not the caller's fault. Endpoint maps this to 500."""


async def propose_and_autoapprove_provenance(
    *,
    level: int | None,
    title: str,
    summary: str,
    from_ids: list | None,
    rationale: str = "",
    test_questions: list | None = None,
    produced_by: str = "harness",
    source: str = "harness-proposal",
    seed: str = "in-situ harness call",
    session_tag: str = "",
) -> dict:
    """Validate, draft, and auto-approve a provenance node.

    Returns a structured result the caller formats for its surface:

        {
          "proposal_id":         the kind:proposal card's delta id,
          "provenance_delta_id": the real kind:provenance delta id
                                 ("" if auto-approve soft-failed),
          "level":               the resolved level (1-3),
          "from_count":          number of constituents,
          "auto_approved":       bool — did the real provenance land,
          "auto_error":          exception type name if auto-approve
                                 soft-failed, else "",
          "test_questions":      cleaned recall-probe questions,
        }

    Raises `ProvenanceValidationError` for any caller-fixable problem
    and `ProvenanceWriteError` if the proposal card write itself fails.

    `produced_by` flows onto the proposal card's `produced-by:` tag
    AND through auto-approve onto the real provenance delta, so later
    analysis can tell a harness-authored node from an agent-authored
    one. `source`, `seed`, and `session_tag` are surface-specific
    display/routing bits.
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    rationale = (rationale or "").strip()

    raw_ids = from_ids or []
    if not isinstance(raw_ids, (list, tuple)):
        raise ProvenanceValidationError("from_ids must be a list of delta-id strings")
    cleaned_ids: list[str] = []
    for x in raw_ids:
        if isinstance(x, str):
            s = x.strip()
            if s and s not in cleaned_ids:
                cleaned_ids.append(s)

    # Per-level constituent floor. L1 (episode): 2 — a tight resonant
    # pair already names a stretch. L2+ (topic / era): 3 — these group
    # already-named children; below 3 it's a pair, and pairs are L1's
    # job. Level may be None here (caller didn't specify); validate
    # conservatively as L1, then the level-vs-children check below
    # enforces structural correctness.
    candidate_level = level if isinstance(level, int) else 1
    min_constituents = 3 if candidate_level >= 2 else 2
    if len(cleaned_ids) < min_constituents:
        raise ProvenanceValidationError(
            f"L{candidate_level} provenance needs at least "
            f"{min_constituents} constituent ids (got {len(cleaned_ids)}). "
            f"L1 (episode) accepts 2+ tightly-related deltas; L2+ "
            f"(topic/era) needs 3+ child stretches. The review pass "
            f"should call `skip` when below the floor."
        )

    if not title:
        raise ProvenanceValidationError("title is required")
    if not summary:
        raise ProvenanceValidationError("summary is required")

    # Resolve every constituent against the lake and note the highest
    # child provenance-level. A model tends to hallucinate ids in the
    # display format it sees in recall output (e.g.
    # "20260420T042833Z-fathom-chat-a1b2c3") — those aren't real 12-char
    # hex delta ids. Without this check the provenance writes fine but
    # its from:<id> tags point at nothing and the hierarchy is poisoned.
    max_child_level = -1  # -1 sentinel = pure base moments
    missing: list[str] = []
    for fid in cleaned_ids:
        try:
            d = await delta_client.get_delta(fid)
        except Exception:
            d = None
        if not isinstance(d, dict):
            missing.append(fid)
            continue
        for t in d.get("tags") or []:
            if isinstance(t, str) and t.startswith("provenance-level:"):
                try:
                    lvl = int(t.split(":", 1)[1])
                except (TypeError, ValueError):
                    continue
                if lvl > max_child_level:
                    max_child_level = lvl
    if missing:
        sample = ", ".join(missing[:5])
        more = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
        raise ProvenanceValidationError(
            f"{len(missing)} of {len(cleaned_ids)} constituent ids do not "
            f"resolve to real deltas in the lake — likely hallucinated from "
            f"recall output formatting. Missing: {sample}{more}. Real delta ids "
            f"are 12-char lowercase hex; only propose constituents whose ids "
            f"you've actually seen as the leading id field of a recall hit, "
            f"not strings reconstructed from timestamp+source."
        )

    if max_child_level < 0:
        min_level = 1
    elif max_child_level >= 3:
        min_level = 3  # cap; no L4 in the current schema
    else:
        min_level = max_child_level + 1

    if level is None:
        level_int = min_level
    else:
        try:
            level_int = int(level)
        except (TypeError, ValueError):
            raise ProvenanceValidationError(f"level must be an integer, got {level!r}") from None
    if level_int < min_level:
        raise ProvenanceValidationError(
            f"level {level_int} is below the minimum {min_level} for "
            f"these constituents (highest child level is {max_child_level}). "
            f"A provenance must sit at or above its children."
        )
    if level_int > 3:
        raise ProvenanceValidationError(
            f"level must be 1-3 (current schema cap), got {level_int}"
        )

    raw_qs = test_questions or []
    if not isinstance(raw_qs, (list, tuple)):
        raw_qs = []
    cleaned_qs = [q for q in raw_qs if isinstance(q, str) and q.strip()]

    payload = {
        "kicker": f"{produced_by} · level-{level_int}",
        "title": title,
        "body": summary,
        "tail": rationale,
        "route": "tool:provenance",
        "tool": "provenance",
        "tool_args": {
            "action": "create",
            "title": title,
            "summary": summary,
            "level": level_int,
            "from_ids": cleaned_ids,
            "rationale": rationale,
            "test_questions": cleaned_qs,
            "seed": seed,
        },
        "axes": {},
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    title_slug = (
        "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")[:80] or "untitled"
    )
    base_tags = [
        "feed-card",
        "synthesis",
        "kind:proposal",
        "proposal-status:pending",
        "tool:provenance",
        "action:create",
        "route:tool:provenance",
        f"provenance-level:{level_int}",
        "provenance-version:v1-experimental",
        f"produced-by:{produced_by}",
        f"title:{title_slug}",
    ]

    try:
        lake_delta = await delta_client.write(
            content=payload_json,
            tags=list(base_tags),
            source=source,
        )
    except Exception as e:
        raise ProvenanceWriteError(f"lake write failed — {type(e).__name__}: {e}") from e
    lake_id = (lake_delta or {}).get("id") or ""

    # Puddle echo so the dashboard feed surfaces the proposal card
    # immediately. Soft-fail — the lake write is the source of truth.
    try:
        from .loop.intents import CONVO_TAG
        from .loop.puddle import puddle

        puddle_tags = [CONVO_TAG, *base_tags]
        if session_tag:
            puddle_tags.insert(1, session_tag)
        if lake_id:
            puddle_tags.append(f"lake-id:{lake_id}")
            puddle_tags.append(f"recalled-id:{lake_id[:24]}")
        await puddle.write(
            content=payload_json,
            tags=puddle_tags,
            source=source,
            ttl_seconds=7 * 24 * 60 * 60,
        )
    except Exception as e:
        print(f"[provenance_create] puddle echo failed: {type(e).__name__}: {e}")

    # Auto-approve gate: every provenance level auto-accepts. L1/L2 are
    # bounded; L3+ eras make stronger identity claims but the operator
    # sees them as "L<n> created" alerts in the header bell rather than
    # a blocking pending decision. The auto-approve is what writes the
    # real kind:provenance delta with the constituent centroid. Soft-
    # fail leaves the proposal pending for manual operator review.
    provenance_delta_id = ""
    auto_approved = False
    auto_error = ""
    if lake_id:
        try:
            from .routes.proposals import auto_approve_provenance

            auto = await auto_approve_provenance(
                proposal_id=lake_id,
                args=dict(payload["tool_args"]),
                produced_by=produced_by,
            )
            provenance_delta_id = (auto.get("result") or {}).get("delta_id") or ""
            auto_approved = True
        except Exception as e:
            auto_error = type(e).__name__
            print(f"[provenance_create] auto-approve failed: {auto_error}: {e}")

    return {
        "proposal_id": lake_id,
        "provenance_delta_id": provenance_delta_id,
        "level": level_int,
        "from_count": len(cleaned_ids),
        "auto_approved": auto_approved,
        "auto_error": auto_error,
        "test_questions": cleaned_qs,
    }
