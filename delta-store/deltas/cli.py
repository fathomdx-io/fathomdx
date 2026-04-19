"""Delta store CLI.

Thin HTTP client for the delta store server. All heavy lifting
(embedding, scoring, subsets) happens server-side.

Usage:
    python -m deltas.cli search "podcasting" --subset
    python -m deltas.cli search "episodes" --subset-id ss_abc123
    python -m deltas.cli write "some content" --tags podcast,episode
    python -m deltas.cli query --since 2h --tags podcast
    python -m deltas.cli tags
    python -m deltas.cli stats
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta

import httpx

DEFAULT_URL = "http://localhost:4246"
DEFAULT_TIMEOUT = 30.0


# ── Client ──────────────────────────────────────────────────────────────────


class DeltaClient:
    """HTTP client for the delta store API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get("DELTA_STORE_URL", DEFAULT_URL)).rstrip("/")
        self.api_key = api_key or os.environ.get("DELTA_API_KEY", "")
        self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        resp = self._client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp

    def _post(self, path: str, data: dict) -> httpx.Response:
        resp = self._client.post(f"{self.base_url}{path}", headers=self._headers(), json=data)
        resp.raise_for_status()
        return resp

    def search(
        self,
        origin: str,
        *,
        radii: dict | None = None,
        session_id: str | None = None,
        create_subset: bool = False,
        subset_id: str | None = None,
        tags_include: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        modality: str | None = None,
        limit: int | None = None,
    ) -> dict:
        body: dict = {"origin": origin}
        if radii:
            body["radii"] = radii
        if session_id:
            body["session_id"] = session_id
        if create_subset:
            body["create_subset"] = True
        if subset_id:
            body["subset_id"] = subset_id
        if tags_include:
            body["tags_include"] = tags_include
        if tags_exclude:
            body["tags_exclude"] = tags_exclude
        if modality:
            body["modality"] = modality
        return self._post("/search", body).json()

    def write(
        self,
        content: str,
        *,
        tags: list[str] | None = None,
        source: str = "cli",
        modality: str = "text",
    ) -> dict:
        body: dict = {"content": content, "source": source, "modality": modality}
        if tags:
            body["tags"] = tags
        return self._post("/deltas", body).json()

    def write_image(
        self,
        image_path: str,
        content: str = "",
        *,
        tags: list[str] | None = None,
        source: str = "cli",
    ) -> dict:
        """Upload an image file as a media delta."""
        import mimetypes

        with open(image_path, "rb") as f:
            image_bytes = f.read()

        files = {"file": (image_path.split("/")[-1], image_bytes, mimetypes.guess_type(image_path)[0] or "image/png")}
        data = {"source": source}
        if content:
            data["content"] = content
        if tags:
            data["tags"] = ",".join(tags)

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        resp = self._client.post(
            f"{self.base_url}/deltas/media/upload",
            headers=headers,
            files=files,
            data=data,
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def get(self, delta_id: str) -> dict:
        return self._get(f"/deltas/{delta_id}").json()

    def query(
        self,
        *,
        time_start: str | None = None,
        time_end: str | None = None,
        tags_include: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        modality: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        params: dict = {"limit": limit, "offset": offset}
        if time_start:
            params["time_start"] = time_start
        if time_end:
            params["time_end"] = time_end
        if tags_include:
            params["tags_include"] = tags_include
        if tags_exclude:
            params["tags_exclude"] = tags_exclude
        if modality:
            params["modality"] = modality
        if source:
            params["source"] = source
        return self._get("/deltas", params=params).json()

    def tags(self) -> dict:
        return self._get("/tags").json()

    def stats(self) -> dict:
        return self._get("/stats").json()

    def plan(self, plan_json: dict) -> dict:
        return self._post("/plan", plan_json).json()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_relative_time(value: str) -> str:
    """Parse relative time like '2h', '30m', '1d' to ISO timestamp."""
    m = re.match(r"^(\d+)([smhd])$", value)
    if not m:
        return value  # assume ISO already
    amount, unit = int(m.group(1)), m.group(2)
    delta = {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]
    ts = datetime.now(UTC) - delta
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _format_delta_row(d: dict) -> str:
    """Format a delta as a compact one-line string."""
    did = d.get("id", "?")[:10]
    ts = d.get("timestamp", "")[:16]
    tags = ", ".join(d.get("tags", []))
    content = d.get("content", "").replace("\n", " ")[:80]
    img = f"  img:{d['media_hash']}" if d.get("media_hash") else ""
    return f"  {did}  {ts}  [{tags}]  {content}{img}"


def _format_scored_row(s: dict) -> str:
    """Format a scored delta as a compact one-line string."""
    d = s.get("delta", s)
    dist = s.get("distance", "")
    did = d.get("id", "?")[:10]
    ts = d.get("timestamp", "")[:16]
    tags = ", ".join(d.get("tags", []))
    content = d.get("content", "").replace("\n", " ")[:80]
    dist_str = f"{dist:.3f}" if isinstance(dist, (int, float)) else str(dist)
    img = f"  img:{d['media_hash']}" if d.get("media_hash") else ""
    return f"  {did}  {ts}  d={dist_str}  [{tags}]  {content}{img}"


# ── Commands ────────────────────────────────────────────────────────────────


def _build_search_plan(
    query: str,
    *,
    radii: dict | None = None,
    subset_id: str | None = None,
    limit: int = 20,
) -> dict:
    """Build a compositional search plan from a query string.

    Every search is a plan: direct search + chain outward + union.
    """
    # Direct search: tight radius to capture the core cluster.
    # Chain: wider radius from direct's centroid to escape the cluster.
    # Diff: subtract direct from chain to find only NEW context.
    # Union: combine core results with new context.
    sem = 0.5
    chain_sem = 0.8

    direct_step: dict = {
        "id": "direct",
        "search": query,
        "radii": {"semantic": sem},
        "limit": limit,
    }
    if subset_id:
        return {"steps": [direct_step]}

    return {
        "steps": [
            direct_step,
            {
                "id": "outward",
                "chain": "direct",
                "radii": {"semantic": chain_sem},
                "limit": limit * 3,
            },
            {
                "id": "by_tag",
                "filter": {"tags_include": [query.lower().split()[0]]},
                "tags_exclude": ["user"],
                "limit": limit,
            },
            {"id": "new_context", "diff": ["outward", "direct"]},
            {"id": "tag_context", "diff": ["by_tag", "direct"]},
            {"id": "extra", "union": ["new_context", "tag_context"]},
            {"id": "results", "union": ["direct", "extra"]},
        ],
    }


def cmd_search(args: argparse.Namespace) -> None:
    client = DeltaClient()
    radii = None
    if args.radii:
        parts = [float(x) for x in args.radii.split(",")]
        if len(parts) == 3:
            radii = {"temporal": parts[0], "semantic": parts[1], "provenance": parts[2]}

    plan = _build_search_plan(
        args.query,
        radii=radii,
        subset_id=args.subset_id,
        limit=20,
    )

    result = client.plan(plan)

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    warnings = result.get("warnings", [])
    for w in warnings:
        print(f"  ! {w}", file=sys.stderr)

    # Show the final combined step (or "direct" if no union)
    final_key = "results" if "results" in result.get("steps", {}) else "direct"
    step = result.get("steps", {}).get(final_key, {})
    deltas = step.get("deltas", [])

    if not deltas:
        print("No results.")
        return

    count = step.get("count", len(deltas))
    print(f"{count} results:")
    for d in deltas:
        did = d.get("id", "?")[:10]
        ts = d.get("timestamp", "")[:16]
        tags = ", ".join(d.get("tags", []))
        content = d.get("content", "").replace("\n", " ")[:80]
        img = f"  img:{d['media_hash']}" if d.get("media_hash") else ""
        print(f"  {did}  {ts}  [{tags}]  {content}{img}")


def cmd_write(args: argparse.Namespace) -> None:
    client = DeltaClient()
    tags = args.tags.split(",") if args.tags else None

    if args.image:
        result = client.write_image(
            args.image,
            content=args.content or "",
            tags=tags,
            source=args.source,
        )
    else:
        result = client.write(
            args.content,
            tags=tags,
            source=args.source,
            modality=args.modality,
        )

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    delta_id = result.get("id", "?")
    media_hash = result.get("media_hash")
    if media_hash:
        print(f"Written: {delta_id}  media: {media_hash}")
    else:
        print(f"Written: {delta_id}")


def cmd_query(args: argparse.Namespace) -> None:
    client = DeltaClient()
    time_start = _parse_relative_time(args.since) if args.since else None
    time_end = _parse_relative_time(args.until) if args.until else None
    tags_include = args.tags.split(",") if args.tags else None
    tags_exclude = args.not_tags.split(",") if args.not_tags else None

    results = client.query(
        time_start=time_start,
        time_end=time_end,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        modality=args.modality,
        source=args.source,
        limit=args.limit,
    )

    if args.json:
        json.dump(results, sys.stdout, indent=2)
        print()
        return

    if not results:
        print("No results.")
        return

    print(f"{len(results)} deltas:")
    for d in results:
        print(_format_delta_row(d))


def cmd_get(args: argparse.Namespace) -> None:
    client = DeltaClient()
    try:
        result = client.get(args.id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print(f"Delta {args.id} not found.")
            sys.exit(1)
        raise

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    print(f"ID:        {result['id']}")
    print(f"Timestamp: {result['timestamp']}")
    print(f"Modality:  {result['modality']}")
    print(f"Source:    {result['source']}")
    print(f"Tags:      {', '.join(result.get('tags', []))}")
    if result.get("media_hash"):
        print(f"Media:     {result['media_hash']}")
    print(f"Content:\n{result['content']}")


_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
_IMAGE_MAGIC = {
    b"RIFF": "webp",
    b"\x89PNG": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF8": "gif",
}


def cmd_view(args: argparse.Namespace) -> None:
    """Fetch an image by media hash and save to a temp file."""
    import tempfile

    client = DeltaClient()
    media_hash = args.media_hash.replace(".webp", "")

    if not all(c in "0123456789abcdef" for c in media_hash):
        print(f"Error: invalid media hash '{media_hash}'", file=sys.stderr)
        sys.exit(1)

    headers = {}
    if client.api_key:
        headers["X-API-Key"] = client.api_key
    resp = client._client.get(f"{client.base_url}/media/{media_hash}", headers=headers)
    if resp.status_code == 404:
        print(f"Image {media_hash} not found.")
        sys.exit(1)
    resp.raise_for_status()

    data = resp.content
    if len(data) > _MAX_IMAGE_BYTES:
        print(
            f"Error: image too large ({len(data) / 1024 / 1024:.1f} MB, limit {_MAX_IMAGE_BYTES // 1024 // 1024} MB)",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(data) < 4:
        print("Error: response too small to be a valid image", file=sys.stderr)
        sys.exit(1)

    # Detect actual format from magic bytes
    ext = "webp"
    for magic, fmt in _IMAGE_MAGIC.items():
        if data[: len(magic)] == magic:
            ext = fmt
            break

    out = args.output
    if not out:
        out = os.path.join(tempfile.gettempdir(), f"delta-{media_hash}.{ext}")
    with open(out, "wb") as f:
        f.write(data)
    print(out)


def cmd_tags(args: argparse.Namespace) -> None:
    client = DeltaClient()
    result = client.tags()

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    if not result:
        print("No tags.")
        return

    for tag, count in sorted(result.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {count}")


def cmd_stats(args: argparse.Namespace) -> None:
    client = DeltaClient()
    result = client.stats()

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    print(f"Total:    {result.get('total', 0)}")
    print(f"Embedded: {result.get('embedded', 0)}")
    print(f"Pending:  {result.get('pending', 0)}")
    print(f"Coverage: {result.get('percent', 0)}%")


def cmd_plan(args: argparse.Namespace) -> None:
    client = DeltaClient()

    try:
        plan_json = json.loads(args.plan_json)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON — {e}", file=sys.stderr)
        sys.exit(1)

    result = client.plan(plan_json)

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    warnings = result.get("warnings", [])
    if warnings:
        for w in warnings:
            print(f"  ⚠ {w}", file=sys.stderr)
        print(file=sys.stderr)

    timing = result.get("timing_ms", 0)

    for step_id, step_data in result.get("steps", {}).items():
        if "buckets" in step_data:
            # Aggregate step
            print(f"── {step_id} (aggregate) ──")
            for b in step_data["buckets"]:
                print(f"  {b['bucket']}: {b['count']}")
        else:
            # Delta set step
            count = step_data.get("count", 0)
            deltas = step_data.get("deltas", [])
            print(f"── {step_id} ({count} results) ──")
            for d in deltas:
                ts = d.get("timestamp", "")[:19]
                src = d.get("source", "")
                content = d.get("content", "")[:120].replace("\n", " ")
                print(f"  [{ts}] ({src}) {content}")
        print()

    print(f"({timing:.0f}ms)")


# ── Argparse ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fathom delta", description="Delta store CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p_search = sub.add_parser("search", help="Semantic search across deltas")
    p_search.add_argument("query", help="Search query text")
    p_search.add_argument("--subset", action="store_true", help="Create a subset from results")
    p_search.add_argument("--subset-id", help="Search within an existing subset")
    p_search.add_argument("--radii", help="Dimension radii as T,S,P (e.g. 1.5,1.0,1.0)")
    p_search.add_argument("--json", action="store_true", help="JSON output")
    p_search.set_defaults(func=cmd_search)

    # write
    p_write = sub.add_parser("write", help="Write a delta")
    p_write.add_argument("content", nargs="?", default="", help="Delta content")
    p_write.add_argument("--image", help="Path to image file — creates a media delta")
    p_write.add_argument("--tags", help="Comma-separated tags")
    p_write.add_argument("--source", default="cli", help="Source identifier")
    p_write.add_argument("--modality", default="text", help="Content modality")
    p_write.add_argument("--json", action="store_true", help="JSON output")
    p_write.set_defaults(func=cmd_write)

    # query
    p_query = sub.add_parser("query", help="Filter deltas by time, tags, source")
    p_query.add_argument("--since", help="Start time (e.g. 2h, 30m, 1d, or ISO)")
    p_query.add_argument("--until", help="End time (e.g. 1h or ISO)")
    p_query.add_argument("--tags", help="Include tags (comma-separated, AND match)")
    p_query.add_argument("--not-tags", help="Exclude tags (comma-separated)")
    p_query.add_argument("--modality", help="Filter by modality")
    p_query.add_argument("--source", help="Filter by source")
    p_query.add_argument("--limit", type=int, default=100, help="Max results")
    p_query.add_argument("--json", action="store_true", help="JSON output")
    p_query.set_defaults(func=cmd_query)

    # get
    p_get = sub.add_parser("get", help="Get a single delta by ID")
    p_get.add_argument("id", help="Delta ID")
    p_get.add_argument("--json", action="store_true", help="JSON output")
    p_get.set_defaults(func=cmd_get)

    # view (image)
    p_view = sub.add_parser("view", help="Fetch an image by media hash, save to file, print path")
    p_view.add_argument("media_hash", help="Media hash from search/get results")
    p_view.add_argument("--output", "-o", help="Output path (default: temp file)")
    p_view.set_defaults(func=cmd_view)

    # tags
    p_tags = sub.add_parser("tags", help="List all tags with counts")
    p_tags.add_argument("--json", action="store_true", help="JSON output")
    p_tags.set_defaults(func=cmd_tags)

    # stats
    p_stats = sub.add_parser("stats", help="Embedding stats")
    p_stats.add_argument("--json", action="store_true", help="JSON output")
    p_stats.set_defaults(func=cmd_stats)

    # plan
    p_plan = sub.add_parser("plan", help="Execute a compositional query plan")
    p_plan.add_argument("plan_json", help="JSON query plan (string or @file)")
    p_plan.add_argument("--json", action="store_true", help="JSON output")
    p_plan.set_defaults(func=cmd_plan)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except httpx.ConnectError:
        print(f"Error: cannot connect to delta store at {DeltaClient().base_url}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"Error: {e.response.status_code} — {e.response.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
