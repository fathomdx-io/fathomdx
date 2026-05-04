#!/usr/bin/env python3
"""Backfill provenance centroid embeddings for existing kind:provenance
deltas.

For every kind:provenance delta in the lake, walks its `from:<id>`
constituents, fetches their `embedding` vectors, computes the
element-wise mean, and writes the result to the provenance's
`provenance_embedding` column via the delta-store's update endpoint.

Idempotent — safe to re-run. Skips provenance deltas that already
have a non-null provenance_embedding unless --force is passed.

Usage:
    python scripts/backfill_provenance_centroids.py
    python scripts/backfill_provenance_centroids.py --force
    python scripts/backfill_provenance_centroids.py --dry-run

Run inside the api container so it can reach the delta-store:
    podman exec fathom-prov_api_1 python -m scripts.backfill_provenance_centroids
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def main(force: bool, dry_run: bool) -> int:
    sys.path.insert(0, "/app")
    from api import delta_client
    from api.provenance_centroid import compute_centroid

    print("Querying kind:provenance deltas...")
    rows = await delta_client.query(
        tags_include=["kind:provenance"],
        limit=5000,
    )
    print(f"  found {len(rows)} provenance deltas")

    needs_update: list[dict] = []
    already_set = 0
    for row in rows:
        prov_emb = row.get("provenance_embedding")
        if prov_emb and not force and isinstance(prov_emb, list) and len(prov_emb) > 0:
            already_set += 1
            continue
        needs_update.append(row)

    print(f"  {already_set} already have a centroid (skip; pass --force to recompute)")
    print(f"  {len(needs_update)} need centroid computed")
    if dry_run:
        print("--dry-run: not writing anything")
        return 0

    written = 0
    skipped_no_constituents = 0
    skipped_no_embeddings = 0
    failed = 0

    for i, row in enumerate(needs_update, start=1):
        delta_id = row.get("id") or ""
        if not delta_id:
            failed += 1
            continue
        from_ids = [
            t.split(":", 1)[1]
            for t in (row.get("tags") or [])
            if isinstance(t, str) and t.startswith("from:")
        ]
        if not from_ids:
            skipped_no_constituents += 1
            continue

        centroid = await compute_centroid(from_ids)
        if not centroid:
            skipped_no_embeddings += 1
            continue

        # Update via the delta-store's set-provenance-embedding endpoint.
        # We only have update_embeddings (which sets BOTH), so we need
        # to also pass the existing content embedding back unchanged.
        existing_emb = row.get("embedding")
        if not existing_emb:
            # No content embedding yet — skip; the embed loop will fill
            # in the embedding shortly, then a re-run picks this up.
            skipped_no_embeddings += 1
            continue

        try:
            client = await delta_client._get()
            r = await client.post(
                "/deltas/update-embeddings",
                json={
                    "id": delta_id,
                    "embedding": existing_emb,
                    "provenance_embedding": centroid,
                },
            )
            if r.status_code == 404:
                # Endpoint may not exist; fallback path uses the
                # write/upsert with id supplied.
                from_ids_tags = [t for t in (row.get("tags") or [])]
                r2 = await client.post(
                    "/deltas",
                    json={
                        "id": delta_id,
                        "content": row.get("content") or "",
                        "tags": from_ids_tags,
                        "source": row.get("source") or "harness-proposal",
                        "embedding": existing_emb,
                        "provenance_embedding": centroid,
                    },
                )
                r2.raise_for_status()
            else:
                r.raise_for_status()
            written += 1
            if i % 25 == 0:
                print(
                    f"  [{i}/{len(needs_update)}] wrote {written}, "
                    f"skipped {skipped_no_constituents + skipped_no_embeddings}"
                )
        except Exception as e:
            failed += 1
            print(f"  [{i}] {delta_id} failed: {type(e).__name__}: {e}")

    print()
    print(
        f"Done. wrote={written} "
        f"skipped_no_constituents={skipped_no_constituents} "
        f"skipped_no_embeddings={skipped_no_embeddings} failed={failed}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true", help="recompute centroids even if already present"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be written, don't write"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(force=args.force, dry_run=args.dry_run)))
