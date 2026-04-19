"""Vault source — continuous ingester + watcher for Obsidian-style markdown vaults.

On each poll:
  - walk vault, diff against sidecar state
  - ADDED files  → emit N chunk RawItems
  - MODIFIED     → emit 1 diff-delta RawItem (pure additive; old chunks stay)
  - DELETED      → emit 1 tombstone RawItem
  - Images uploaded directly to /deltas/media/upload (runner can't do multipart)

Sidecar state lives at  $DATA_DIR/vault-watch/{path_slug}/:
  files.json            {relpath: {mtime_ns, sha256}}
  images.json           {image_abs_path: media_hash}
  last-content/{slug}.txt   last-ingested raw text, for diff computation
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .base import ProducedDelta, RawItem, SourceProducer
from .vault_diff import compute_diff, render_diff_delta, render_tombstone
from .vault_parsing import (
    IMAGE_EXTENSIONS,
    ParsedChunk,
    chunk_raw_item_id,
    dedup_tags,
    diff_raw_item_id,
    doc_tag,
    find_vault_files,
    find_vault_images,
    parse_document,
    resolve_image_src,
    subfolder_tag,
    tombstone_raw_item_id,
)

log = logging.getLogger("source.vault")

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
DEBOUNCE_SECONDS = 2.0  # skip files modified in the last 2s (Obsidian autosave)


class VaultProducer(SourceProducer):
    source_type = "vault"
    display_name = "Vault"
    description = "Continuous ingester + watcher for Obsidian-style markdown vaults"
    version = "0.1.0"
    author = "fathom"
    auth_type = "none"
    schedule_type = "watch"  # declarative; implemented via 1m poll
    default_interval = "1m"
    digestion = "raw"  # we control output precisely
    default_expiry_days = None
    expiry_configurable = True

    # ── Required: poll ──────────────────────────────────────────────────

    async def poll(self, config: dict, since: float | None = None) -> list[RawItem]:
        vault_path = self._vault_path(config)
        if vault_path is None:
            return []
        workspace = self._infer_workspace(vault_path)
        state_dir = self._state_dir_for(vault_path)
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "last-content").mkdir(exist_ok=True)

        files_state = self._load_files_state(state_dir)
        image_state = self._load_image_state(state_dir)

        items: list[RawItem] = []
        current_files = find_vault_files(vault_path)
        current_relpaths = {str(f.relative_to(vault_path)) for f in current_files}
        prior_relpaths = set(files_state.keys())
        debounce_cutoff = time.time() - DEBOUNCE_SECONDS

        # ── ADDED / MODIFIED ────────────────────────────────────────────
        for f in current_files:
            relpath = str(f.relative_to(vault_path))
            try:
                stat = f.stat()
            except OSError:
                continue
            # Debounce: skip files still being written
            if stat.st_mtime > debounce_cutoff:
                continue

            mtime_ns = stat.st_mtime_ns
            prior = files_state.get(relpath)
            if prior and prior.get("mtime_ns") == mtime_ns:
                continue  # unchanged

            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                log.warning("Failed to read %s", f, exc_info=True)
                continue
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

            if prior and prior.get("sha256") == sha:
                # mtime bumped but content unchanged — just refresh state
                files_state[relpath] = {"mtime_ns": mtime_ns, "sha256": sha}
                continue

            is_new = prior is None

            # First-run baseline: if doc tag already exists in lake, seed state without emitting
            if is_new and await self._doc_already_ingested(workspace, relpath):
                log.info("vault: baseline seed (already in lake): %s", relpath)
                self._write_last_content(state_dir, relpath, content)
                files_state[relpath] = {"mtime_ns": mtime_ns, "sha256": sha}
                continue

            parsed = parse_document(content, workspace=workspace, relpath=relpath)

            # Upload images referenced by the note (dedup via image_state)
            await self._upload_referenced_images(
                parsed_images=parsed.all_images,
                md_file=f,
                vault_path=vault_path,
                workspace=workspace,
                relpath=relpath,
                parent_doc_tag=parsed.doc_tag,
                image_state=image_state,
            )

            now_ns = time.time_ns()
            if is_new:
                items.extend(
                    self._build_chunk_items(
                        parsed=parsed,
                        workspace=workspace,
                        relpath=relpath,
                        mtime=stat.st_mtime,
                    )
                )
            else:
                old_content = self._read_last_content(state_dir, relpath)
                items.append(
                    self._build_diff_item(
                        workspace=workspace,
                        relpath=relpath,
                        parent_doc_tag=parsed.doc_tag,
                        old_content=old_content,
                        new_content=content,
                        mtime_ns=mtime_ns,
                        now_ns=now_ns,
                    )
                )

            self._write_last_content(state_dir, relpath, content)
            files_state[relpath] = {"mtime_ns": mtime_ns, "sha256": sha}

        # ── DELETED ─────────────────────────────────────────────────────
        for relpath in prior_relpaths - current_relpaths:
            old_content = self._read_last_content(state_dir, relpath) or ""
            items.append(
                self._build_tombstone_item(
                    workspace=workspace,
                    relpath=relpath,
                    old_content=old_content,
                    now_ns=time.time_ns(),
                )
            )
            files_state.pop(relpath, None)
            self._delete_last_content(state_dir, relpath)

        # ── Standalone images not referenced by any note ────────────────
        await self._upload_standalone_images(
            vault_path=vault_path,
            workspace=workspace,
            image_state=image_state,
        )

        self._save_files_state(state_dir, files_state)
        self._save_image_state(state_dir, image_state)

        if items:
            log.info("vault[%s]: emitting %d items", workspace, len(items))
        return items

    # ── Required: digest ────────────────────────────────────────────────

    def digest(self, item: RawItem, config: dict | None = None) -> ProducedDelta:
        meta = item.meta or {}
        tags = dedup_tags(list(meta.get("tags") or []))
        return ProducedDelta(
            content=item.content,
            tags=tags,
            source=meta.get("source", "vault"),
            modality="text",
            timestamp=item.timestamp,
        )

    # ── Required: validate_config ───────────────────────────────────────

    def validate_config(self, config: dict) -> list[str]:
        path = config.get("path", "").strip()
        if not path:
            return ["'path' is required (vault directory)"]
        p = Path(path).expanduser()
        if not p.exists():
            return [f"path does not exist: {path}"]
        if not p.is_dir():
            return [f"path is not a directory: {path}"]
        if not os.access(p, os.R_OK):
            return [f"path is not readable: {path}"]
        return []

    def default_tags(self, config: dict) -> list[str]:
        return ["vault-note"]

    # ── Internal: RawItem builders ──────────────────────────────────────

    def _build_chunk_items(
        self,
        *,
        parsed,
        workspace: str,
        relpath: str,
        mtime: float,
    ) -> list[RawItem]:
        ts = _iso_ts(mtime)
        sub = subfolder_tag(relpath)
        base_tags = ["vault-note", workspace, parsed.doc_tag]
        if sub:
            base_tags.append(sub)
        base_tags.extend(parsed.frontmatter_tags)
        source = f"vault/{workspace}"

        items: list[RawItem] = []
        for chunk in parsed.chunks:
            content = chunk.content
            if chunk.heading_trail and not content.lstrip().startswith("#"):
                content = f"{chunk.heading_trail}\n\n{content}"
            chunk_tags = dedup_tags(
                base_tags + chunk.inline_tags + [f"link:{w}" for w in chunk.wikilinks]
            )
            raw_id = chunk_raw_item_id(parsed.doc_tag, chunk.index, chunk.content_hash)
            items.append(
                RawItem(
                    id=raw_id,
                    content=content,
                    timestamp=ts,
                    title=chunk.heading_trail or Path(relpath).stem,
                    meta={"tags": chunk_tags, "source": source},
                )
            )
        return items

    def _build_diff_item(
        self,
        *,
        workspace: str,
        relpath: str,
        parent_doc_tag: str,
        old_content: str | None,
        new_content: str,
        mtime_ns: int,
        now_ns: int,
    ) -> RawItem:
        summary = compute_diff(old_content or "", new_content, relpath=relpath)
        body = render_diff_delta(summary) or f"vault-diff: {relpath} (no textual diff)"
        sub = subfolder_tag(relpath)
        tags = ["vault-diff", workspace, parent_doc_tag]
        if sub:
            tags.append(sub)
        return RawItem(
            id=diff_raw_item_id(parent_doc_tag, mtime_ns),
            content=body,
            timestamp=_iso_ts(now_ns / 1e9),
            title=f"Diff: {relpath}",
            meta={"tags": dedup_tags(tags), "source": f"vault/{workspace}"},
        )

    def _build_tombstone_item(
        self,
        *,
        workspace: str,
        relpath: str,
        old_content: str,
        now_ns: int,
    ) -> RawItem:
        body = render_tombstone(relpath, old_content)
        parent_doc_tag = doc_tag(workspace, relpath)
        sub = subfolder_tag(relpath)
        tags = ["vault-deletion", "deleted", workspace, parent_doc_tag]
        if sub:
            tags.append(sub)
        return RawItem(
            id=tombstone_raw_item_id(parent_doc_tag, now_ns),
            content=body,
            timestamp=_iso_ts(now_ns / 1e9),
            title=f"Deleted: {relpath}",
            meta={"tags": dedup_tags(tags), "source": f"vault/{workspace}"},
        )

    # ── Image upload ────────────────────────────────────────────────────

    async def _upload_referenced_images(
        self,
        *,
        parsed_images,
        md_file: Path,
        vault_path: Path,
        workspace: str,
        relpath: str,
        parent_doc_tag: str,
        image_state: dict,
    ) -> None:
        if not parsed_images:
            return
        sub = subfolder_tag(relpath)
        for img in parsed_images:
            kind, resolved = resolve_image_src(img.src, md_file, vault_path)
            if kind != "local" or resolved is None:
                continue  # URLs ride along in chunk content; missing stays missing
            abs_key = str(resolved)
            if abs_key in image_state:
                continue
            tags = ["vault-image", "image", workspace, parent_doc_tag]
            if sub:
                tags.append(sub)
            result = await self._upload_image(
                path=resolved,
                content=img.alt or resolved.name,
                tags=dedup_tags(tags),
                source=f"vault/{workspace}",
            )
            if result:
                image_state[abs_key] = result

    async def _upload_standalone_images(
        self,
        *,
        vault_path: Path,
        workspace: str,
        image_state: dict,
    ) -> None:
        for img_path in find_vault_images(vault_path):
            abs_key = str(img_path)
            if abs_key in image_state:
                continue
            rel = str(img_path.relative_to(vault_path))
            sub_parts = Path(rel).parts
            tags = ["vault-image", "image", workspace]
            if len(sub_parts) > 1:
                tags.append(f"vault:{sub_parts[0]}")
            result = await self._upload_image(
                path=img_path,
                content=rel,
                tags=dedup_tags(tags),
                source=f"vault/{workspace}",
            )
            if result:
                image_state[abs_key] = result

    async def _upload_image(
        self,
        *,
        path: Path,
        content: str,
        tags: list[str],
        source: str,
    ) -> str | None:
        """POST an image to /deltas/media/upload. Returns media_hash or None."""
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > MAX_IMAGE_SIZE:
            log.warning("vault: skip image (too large): %s", path)
            return None

        url = _delta_url() + "/deltas/media/upload"
        headers = _delta_headers()
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(path, "rb") as f:
                    resp = await client.post(
                        url,
                        files={"file": (path.name, f.read(), "application/octet-stream")},
                        data={
                            "content": content,
                            "tags": ",".join(tags),
                            "source": source,
                        },
                        headers=headers,
                    )
                resp.raise_for_status()
                body = resp.json()
                return body.get("media_hash") or body.get("id")
        except Exception as e:
            log.warning("vault: image upload failed (%s): %s", path.name, e)
            return None

    # ── Baseline check against existing lake ────────────────────────────

    async def _doc_already_ingested(self, workspace: str, relpath: str) -> bool:
        """Return True if the lake already has deltas tagged with this doc tag.

        Used on first run so we don't re-ingest files previously imported via
        any prior import path (e.g. the pre-v0.1 vault-import script).
        """
        tag = doc_tag(workspace, relpath)
        url = _delta_url() + "/deltas"
        params = {"tags_include": tag, "limit": 1}
        headers = _delta_headers()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code != 200:
                    return False
                return len(resp.json()) > 0
        except Exception:
            return False

    # ── Paths & state helpers ───────────────────────────────────────────

    def _vault_path(self, config: dict) -> Path | None:
        raw = config.get("path", "").strip()
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    def _infer_workspace(self, vault_path: Path) -> str:
        """Path like /home/.../Work/{workspace}/vault/... → {workspace}."""
        if vault_path.name == "vault":
            return vault_path.parent.name
        for parent in vault_path.parents:
            if parent.name == "vault":
                return parent.parent.name
        return vault_path.name

    def _state_dir_for(self, vault_path: Path) -> Path:
        base = Path(os.environ.get("DATA_DIR", "/data")) / "vault-watch"
        slug = hashlib.sha1(str(vault_path).encode("utf-8")).hexdigest()[:16]
        return base / slug

    def _load_files_state(self, state_dir: Path) -> dict[str, dict[str, Any]]:
        path = state_dir / "files.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            log.warning("vault: corrupt files.json at %s; starting fresh", path)
            return {}

    def _save_files_state(self, state_dir: Path, state: dict) -> None:
        path = state_dir / "files.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(path)

    def _load_image_state(self, state_dir: Path) -> dict[str, str]:
        path = state_dir / "images.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _save_image_state(self, state_dir: Path, state: dict) -> None:
        path = state_dir / "images.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(path)

    def _last_content_path(self, state_dir: Path, relpath: str) -> Path:
        slug = relpath.replace("/", "__").replace("\\", "__")
        return state_dir / "last-content" / f"{slug}.txt"

    def _read_last_content(self, state_dir: Path, relpath: str) -> str | None:
        path = self._last_content_path(state_dir, relpath)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def _write_last_content(self, state_dir: Path, relpath: str, content: str) -> None:
        path = self._last_content_path(state_dir, relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".txt.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def _delete_last_content(self, state_dir: Path, relpath: str) -> None:
        path = self._last_content_path(state_dir, relpath)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


# ── Module-level helpers ─────────────────────────────────────────────────


def _iso_ts(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _delta_url() -> str:
    return os.environ.get("DELTA_STORE_URL", "http://localhost:4246").rstrip("/")


def _delta_headers() -> dict[str, str]:
    key = os.environ.get("DELTA_API_KEY", "")
    return {"X-API-Key": key} if key else {}


# Exported for tests / introspection
__all__ = ["VaultProducer", "IMAGE_EXTENSIONS", "ParsedChunk"]
