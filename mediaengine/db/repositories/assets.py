"""Assets, files, relations, technical metadata and document text.

The central idea in here is **content identity**. An asset is a hash, not a
path. ``upsert_file`` therefore does two things that a naive indexer does not:

* if a path is already known and its ``(size, mtime_ns, inode)`` are unchanged,
  it reports "seen" and does no work at all — the triage fast path;
* if a *new* path hashes to an *existing* asset, it attaches the path to that
  asset instead of creating a duplicate. A moved or copied file is the same
  asset with two rows in ``files``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ...types import FileStatus, MediaType, RelationKind
from ...util import json_dumps, json_loads, utcnow_iso
from .base import Repository, placeholders, row_to_dict, rows_to_dicts

__all__ = ["AssetRepository", "TriageResult"]


class TriageResult:
    """Outcome of probing a path against the ``files`` table.

    ``unchanged`` is the hot path: a re-scan of a 200k-file library should hit
    it for essentially every file and never open a single one of them.
    """

    __slots__ = ("file_id", "asset_id", "unchanged", "known_path")

    def __init__(
        self,
        *,
        file_id: int | None = None,
        asset_id: int | None = None,
        unchanged: bool = False,
        known_path: bool = False,
    ) -> None:
        self.file_id = file_id
        self.asset_id = asset_id
        self.unchanged = unchanged
        self.known_path = known_path

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"TriageResult(file_id={self.file_id}, asset_id={self.asset_id}, "
            f"unchanged={self.unchanged}, known_path={self.known_path})"
        )


class AssetRepository(Repository):
    """CRUD for the identity layer."""

    # ── triage ──────────────────────────────────────────────────────────────

    def triage(self, path: str, stat: os.stat_result) -> TriageResult:
        """Decide whether ``path`` needs hashing.

        Compares the recorded ``(size, mtime_ns, inode)`` tuple. Any mismatch
        means the bytes may have changed, so the file goes on to be re-hashed.
        """
        row = self._one(
            "SELECT id, asset_id, size_bytes, mtime_ns, inode FROM files WHERE path = ?",
            (path,),
        )
        if row is None:
            return TriageResult()
        unchanged = (
            row["size_bytes"] == stat.st_size
            and row["mtime_ns"] == stat.st_mtime_ns
            and (row["inode"] in (None, 0) or row["inode"] == stat.st_ino)
        )
        return TriageResult(
            file_id=int(row["id"]),
            asset_id=int(row["asset_id"]),
            unchanged=unchanged,
            known_path=True,
        )

    def triage_batch(self, paths: Sequence[str]) -> dict[str, sqlite3.Row]:
        """Fetch many ``files`` rows at once, keyed by path.

        One query per batch instead of one per file; this is what keeps a
        re-scan of a large library in the seconds range.
        """
        out: dict[str, sqlite3.Row] = {}
        for i in range(0, len(paths), 500):
            chunk = paths[i : i + 500]
            rows = self._query(
                "SELECT id, asset_id, path, size_bytes, mtime_ns, inode, status "
                f"FROM files WHERE path IN ({placeholders(len(chunk))})",
                chunk,
            )
            for row in rows:
                out[str(row["path"])] = row
        return out

    # ── assets ──────────────────────────────────────────────────────────────

    def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        return row_to_dict(self._one("SELECT * FROM assets WHERE id = ?", (asset_id,)))

    def get_asset_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        return row_to_dict(
            self._one("SELECT * FROM assets WHERE content_hash = ?", (content_hash,))
        )

    def asset_id_for_hash(self, content_hash: str) -> int | None:
        value = self._scalar("SELECT id FROM assets WHERE content_hash = ?", (content_hash,))
        return None if value is None else int(value)

    def upsert_asset(
        self,
        *,
        content_hash: str,
        media_type: MediaType | str,
        size_bytes: int,
        mime_type: str | None = None,
        hash_algorithm: str = "blake3",
        perceptual_hash: str | None = None,
        captured_at: str | None = None,
        captured_at_tz: str | None = None,
        captured_at_source: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[int, bool]:
        """Create the asset for ``content_hash``, or return the existing one.

        Returns ``(asset_id, created)``. Idempotent: re-ingesting identical
        bytes never produces a second asset. Only fills in ``captured_at`` and
        ``perceptual_hash`` when they are currently NULL, so a later cheap pass
        cannot clobber a better value an earlier expensive pass found.
        """

        def _op(c: sqlite3.Connection) -> tuple[int, bool]:
            existing = c.execute(
                "SELECT id, captured_at, perceptual_hash FROM assets WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            if existing is not None:
                asset_id = int(existing["id"])
                patch: dict[str, Any] = {}
                if existing["captured_at"] is None and captured_at is not None:
                    patch["captured_at"] = captured_at
                    patch["captured_at_tz"] = captured_at_tz
                    patch["captured_at_source"] = captured_at_source
                if existing["perceptual_hash"] is None and perceptual_hash is not None:
                    patch["perceptual_hash"] = perceptual_hash
                if patch:
                    self._update(c, "assets", patch, "id = ?", (asset_id,))
                return asset_id, False

            asset_id = self._insert(
                c,
                "assets",
                {
                    "content_hash": content_hash,
                    "hash_algorithm": hash_algorithm,
                    "perceptual_hash": perceptual_hash,
                    "media_type": str(media_type),
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "captured_at": captured_at,
                    "captured_at_tz": captured_at_tz,
                    "captured_at_source": captured_at_source,
                    "imported_at": utcnow_iso(),
                },
            )
            return asset_id, True

        return self._write(_op, conn, label="upsert_asset")

    def set_captured_at(
        self,
        asset_id: int,
        captured_at: str | None,
        tz: str | None = None,
        source: str | None = None,
        *,
        force: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Set the capture timestamp.

        Without ``force`` this refuses to overwrite a value whose source is
        ``user``: a human who corrected a wrong date must not have that
        correction undone by the next re-index (principle 4).
        """

        def _op(c: sqlite3.Connection) -> None:
            if not force:
                row = c.execute(
                    "SELECT captured_at_source FROM assets WHERE id = ?", (asset_id,)
                ).fetchone()
                if row is not None and row["captured_at_source"] == "user":
                    return
            self._update(
                c,
                "assets",
                {
                    "captured_at": captured_at,
                    "captured_at_tz": tz,
                    "captured_at_source": source,
                },
                "id = ?",
                (asset_id,),
            )

        self._write(_op, conn, label="set_captured_at")

    def set_perceptual_hash(
        self, asset_id: int, phash: str | None, *, conn: sqlite3.Connection | None = None
    ) -> None:
        self._write(
            lambda c: self._update(c, "assets", {"perceptual_hash": phash}, "id = ?", (asset_id,)),
            conn,
            label="set_phash",
        )

    def mark_indexed(
        self, asset_ids: Sequence[int], *, conn: sqlite3.Connection | None = None
    ) -> None:
        """Stamp ``indexed_at`` after a search-index rebuild."""
        if not asset_ids:
            return
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> None:
            c.executemany("UPDATE assets SET indexed_at = ? WHERE id = ?", [(now, a) for a in asset_ids])

        self._write(_op, conn, label="mark_indexed")

    def delete_asset(self, asset_id: int, *, conn: sqlite3.Connection | None = None) -> bool:
        """Hard-delete an asset. Cascades to every derived table."""

        def _op(c: sqlite3.Connection) -> bool:
            c.execute("DELETE FROM asset_geo WHERE id = ?", (asset_id,))
            cur = c.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            return cur.rowcount > 0

        return self._write(_op, conn, label="delete_asset")

    def count(self, media_type: MediaType | str | None = None) -> int:
        if media_type is None:
            return int(self._scalar("SELECT COUNT(*) FROM assets") or 0)
        return int(
            self._scalar("SELECT COUNT(*) FROM assets WHERE media_type = ?", (str(media_type),))
            or 0
        )

    def counts_by_type(self) -> dict[str, int]:
        rows = self._query("SELECT media_type, COUNT(*) AS n FROM assets GROUP BY media_type")
        return {str(r["media_type"]): int(r["n"]) for r in rows}

    def iter_asset_ids(
        self,
        *,
        media_types: Sequence[str] | None = None,
        after_id: int = 0,
        limit: int = 1000,
    ) -> list[int]:
        """Keyset pagination over asset ids. Used by backfill enumeration."""
        params: list[Any] = [after_id]
        clause = ""
        if media_types:
            clause = f" AND media_type IN ({placeholders(len(media_types))})"
            params.extend(media_types)
        params.append(limit)
        rows = self._query(
            f"SELECT id FROM assets WHERE id > ?{clause} ORDER BY id LIMIT ?", params
        )
        return [int(r["id"]) for r in rows]

    # ── files ───────────────────────────────────────────────────────────────

    def upsert_file(
        self,
        *,
        asset_id: int,
        path: str,
        stat: os.stat_result | None = None,
        volume_id: str | None = None,
        status: FileStatus | str = FileStatus.PRESENT,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Attach a path to an asset, creating or updating the ``files`` row.

        If the path already exists pointing at a *different* asset — the file's
        contents changed in place — it is repointed, and the old asset is left
        alone. Garbage collection of now-orphaned assets is a separate,
        explicit operation, because "no file points here" can also mean "the
        external drive is unplugged".
        """
        p = Path(path)
        now = utcnow_iso()
        values: dict[str, Any] = {
            "asset_id": asset_id,
            "path": path,
            "filename": p.name,
            "extension": p.suffix.lower().lstrip(".") or None,
            "parent_dir": str(p.parent),
            "status": str(status),
            "last_seen_at": now,
            "volume_id": volume_id,
        }
        if stat is not None:
            values.update(
                {
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "inode": stat.st_ino or None,
                    "device": stat.st_dev or None,
                }
            )

        def _op(c: sqlite3.Connection) -> int:
            existing = c.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
            if existing is not None:
                file_id = int(existing["id"])
                self._update(c, "files", values, "id = ?", (file_id,))
                return file_id
            values["first_seen_at"] = now
            return self._insert(c, "files", values)

        return self._write(_op, conn, label="upsert_file")

    def touch_files(
        self, file_ids: Sequence[int], *, conn: sqlite3.Connection | None = None
    ) -> None:
        """Bump ``last_seen_at`` for files confirmed present during a scan."""
        if not file_ids:
            return
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> None:
            c.executemany(
                "UPDATE files SET last_seen_at = ?, status = 'present' WHERE id = ?",
                [(now, f) for f in file_ids],
            )

        self._write(_op, conn, label="touch_files")

    def files_for_asset(self, asset_id: int) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self._query(
                "SELECT * FROM files WHERE asset_id = ? ORDER BY status, id", (asset_id,)
            )
        )

    def primary_path(self, asset_id: int) -> str | None:
        """The path the engine should read when it needs the actual bytes."""
        value = self._scalar(
            "SELECT path FROM files WHERE asset_id = ? AND status = 'present' "
            "ORDER BY id LIMIT 1",
            (asset_id,),
        )
        return None if value is None else str(value)

    def mark_missing_under(
        self,
        root: str,
        scan_started_at: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Flag files under ``root`` not seen during the current scan.

        Rows are marked ``missing`` rather than deleted. An unmounted network
        share must not destroy the index; the user reconnects it and the next
        scan flips them back to ``present``.
        """

        def _op(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "UPDATE files SET status = 'missing' "
                "WHERE status = 'present' AND path LIKE ? AND "
                "(last_seen_at IS NULL OR last_seen_at < ?)",
                (root.rstrip("/\\") + os.sep + "%", scan_started_at),
            )
            return cur.rowcount

        return self._write(_op, conn, label="mark_missing")

    def delete_file(self, file_id: int, *, conn: sqlite3.Connection | None = None) -> None:
        self._write(
            lambda c: c.execute("DELETE FROM files WHERE id = ?", (file_id,)),
            conn,
            label="delete_file",
        )

    def orphaned_asset_ids(self, limit: int = 1000) -> list[int]:
        """Assets with no ``present`` file. Candidates for garbage collection."""
        rows = self._query(
            "SELECT a.id FROM assets a "
            "WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.asset_id = a.id AND f.status = 'present') "
            "LIMIT ?",
            (limit,),
        )
        return [int(r["id"]) for r in rows]

    # ── relations ───────────────────────────────────────────────────────────

    def add_relation(
        self,
        parent_asset_id: int,
        child_asset_id: int,
        relation: RelationKind | str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Record a RAW+JPEG pair, motion photo, or sidecar link."""
        if parent_asset_id == child_asset_id:
            return

        def _op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT OR IGNORE INTO asset_relations"
                "(parent_asset_id, child_asset_id, relation, created_at) VALUES (?,?,?,?)",
                (parent_asset_id, child_asset_id, str(relation), utcnow_iso()),
            )

        self._write(_op, conn, label="add_relation")

    def relations_for_asset(self, asset_id: int) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self._query(
                "SELECT parent_asset_id, child_asset_id, relation, created_at "
                "FROM asset_relations WHERE parent_asset_id = ? OR child_asset_id = ?",
                (asset_id, asset_id),
            )
        )

    # ── technical metadata ──────────────────────────────────────────────────

    def set_technical_metadata(
        self,
        asset_id: int,
        payload: dict[str, Any],
        *,
        raw: dict[str, Any] | None = None,
        extractor: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Write the promoted hot columns plus the untruncated raw payload."""
        columns = {
            "width", "height", "duration_s", "frame_rate", "video_codec", "audio_codec",
            "audio_channels", "sample_rate", "bit_rate", "bit_depth", "orientation",
            "page_count", "word_count", "camera_make", "camera_model", "lens_model",
            "iso", "f_number", "exposure_time", "focal_length", "color_space",
            "container", "has_alpha", "is_animated",
        }
        values: dict[str, Any] = {k: v for k, v in payload.items() if k in columns}
        values["asset_id"] = asset_id
        values["extractor"] = extractor
        values["extracted_at"] = utcnow_iso()
        values["raw_json"] = json_dumps(raw if raw is not None else {})

        def _op(c: sqlite3.Connection) -> None:
            self._upsert(c, "technical_metadata", values, ["asset_id"])

        self._write(_op, conn, label="set_technical_metadata")

    def get_technical_metadata(self, asset_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM technical_metadata WHERE asset_id = ?", (asset_id,))
        result = row_to_dict(row)
        if result is not None:
            result["raw"] = json_loads(result.pop("raw_json", None), {})
        return result

    # ── document text ───────────────────────────────────────────────────────

    def set_document_text(
        self,
        asset_id: int,
        text: str,
        *,
        truncated: bool = False,
        ocr_eligible: bool = False,
        extractor: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        values = {
            "asset_id": asset_id,
            "text": text,
            "char_count": len(text),
            "truncated": int(truncated),
            "ocr_eligible": int(ocr_eligible),
            "extractor": extractor,
            "extracted_at": utcnow_iso(),
        }
        self._write(
            lambda c: self._upsert(c, "document_text", values, ["asset_id"]),
            conn,
            label="set_document_text",
        )

    def get_document_text(self, asset_id: int) -> str | None:
        value = self._scalar("SELECT text FROM document_text WHERE asset_id = ?", (asset_id,))
        return None if value is None else str(value)

    def ocr_eligible_asset_ids(self, limit: int = 1000) -> list[int]:
        """PDFs with no text layer, awaiting an OCR plugin."""
        rows = self._query(
            "SELECT asset_id FROM document_text WHERE ocr_eligible = 1 LIMIT ?", (limit,)
        )
        return [int(r["asset_id"]) for r in rows]

    # ── duplicates ──────────────────────────────────────────────────────────

    def exact_duplicate_groups(self, limit: int = 100) -> list[dict[str, Any]]:
        """Assets reachable from more than one present path."""
        rows = self._query(
            "SELECT a.id AS asset_id, a.content_hash, a.size_bytes, COUNT(f.id) AS copies "
            "FROM assets a JOIN files f ON f.asset_id = a.id AND f.status = 'present' "
            "GROUP BY a.id HAVING copies > 1 ORDER BY copies DESC, a.size_bytes DESC LIMIT ?",
            (limit,),
        )
        out = rows_to_dicts(rows)
        for group in out:
            group["paths"] = [
                str(r["path"])
                for r in self._query(
                    "SELECT path FROM files WHERE asset_id = ? AND status = 'present'",
                    (group["asset_id"],),
                )
            ]
        return out

    def near_duplicate_groups(self, max_distance: int = 4, limit: int = 100) -> list[list[int]]:
        """Group assets by Hamming distance between perceptual hashes.

        Brute force over the phash column. Fine to a few hundred thousand
        images; beyond that this wants a BK-tree, which is a deliberate
        non-goal for the core (a plugin can do better).
        """
        rows = self._query(
            "SELECT id, perceptual_hash FROM assets "
            "WHERE perceptual_hash IS NOT NULL ORDER BY id LIMIT 200000"
        )
        items = [(int(r["id"]), int(str(r["perceptual_hash"]), 16)) for r in rows]
        seen: set[int] = set()
        groups: list[list[int]] = []
        for i, (aid, ah) in enumerate(items):
            if aid in seen:
                continue
            group = [aid]
            for bid, bh in items[i + 1 :]:
                if bid in seen:
                    continue
                if bin(ah ^ bh).count("1") <= max_distance:
                    group.append(bid)
                    seen.add(bid)
            if len(group) > 1:
                seen.add(aid)
                groups.append(group)
                if len(groups) >= limit:
                    break
        return groups

    # ── export ──────────────────────────────────────────────────────────────

    def export_asset(self, asset_id: int) -> dict[str, Any] | None:
        """Everything the system knows about one asset, as plain JSON data.

        Required by the data-handling section: a user must be able to see the
        complete record, not a curated summary of it.
        """
        asset = self.get_asset(asset_id)
        if asset is None:
            return None
        return {
            "asset": asset,
            "files": self.files_for_asset(asset_id),
            "technical_metadata": self.get_technical_metadata(asset_id),
            "document_text": self.get_document_text(asset_id),
            "relations": self.relations_for_asset(asset_id),
            "annotations": rows_to_dicts(
                self._query(
                    "SELECT a.*, p.plugin_id, p.version AS producer_version, p.model_id "
                    "FROM annotations a JOIN producers p ON p.id = a.producer_id "
                    "WHERE a.asset_id = ? ORDER BY a.id",
                    (asset_id,),
                )
            ),
            "regions": rows_to_dicts(
                self._query("SELECT * FROM regions WHERE asset_id = ? ORDER BY id", (asset_id,))
            ),
            "locations": rows_to_dicts(
                self._query("SELECT * FROM asset_locations WHERE asset_id = ?", (asset_id,))
            ),
            "tags": rows_to_dicts(
                self._query(
                    "SELECT t.namespace, t.name, at.source, at.confidence "
                    "FROM asset_tags at JOIN tags t ON t.id = at.tag_id WHERE at.asset_id = ?",
                    (asset_id,),
                )
            ),
            "identities": rows_to_dicts(
                self._query(
                    "SELECT ri.region_id, ri.identity_id, i.display_name, ri.confidence, "
                    "ri.source, ri.confirmed FROM region_identity ri "
                    "JOIN regions r ON r.id = ri.region_id "
                    "LEFT JOIN identities i ON i.id = ri.identity_id WHERE r.asset_id = ?",
                    (asset_id,),
                )
            ),
            "derivatives": rows_to_dicts(
                self._query("SELECT * FROM derivatives WHERE asset_id = ?", (asset_id,))
            ),
            "embeddings": rows_to_dicts(
                self._query(
                    "SELECT id, region_id, producer_id, kind, dim, created_at "
                    "FROM embeddings WHERE asset_id = ?",
                    (asset_id,),
                )
            ),
            "analysis_tasks": rows_to_dicts(
                self._query("SELECT * FROM analysis_tasks WHERE asset_id = ?", (asset_id,))
            ),
        }

    # ── bulk helpers used by the indexer ────────────────────────────────────

    def assets_needing_index(self, limit: int = 500) -> list[int]:
        """Assets whose search row is missing or older than their last change."""
        rows = self._query(
            "SELECT a.id FROM assets a "
            "LEFT JOIN search_docs s ON s.asset_id = a.id "
            "WHERE s.asset_id IS NULL OR a.indexed_at IS NULL "
            "ORDER BY a.id LIMIT ?",
            (limit,),
        )
        return [int(r["id"]) for r in rows]

    def hashes_for_assets(self, asset_ids: Iterable[int]) -> dict[int, str]:
        ids = list(asset_ids)
        if not ids:
            return {}
        out: dict[int, str] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            for row in self._query(
                f"SELECT id, content_hash FROM assets WHERE id IN ({placeholders(len(chunk))})",
                chunk,
            ):
                out[int(row["id"])] = str(row["content_hash"])
        return out
