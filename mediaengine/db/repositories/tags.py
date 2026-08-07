"""Tags, the derivative cache index, and the full-text search document.

Tags are normalised: a ``tags`` table and an ``asset_tags`` junction. There is
no array column and no ``frequency`` column — frequency is a view over the
junction table, so it cannot go stale the way a denormalised counter does.

The search-document writer lives here too, because it is a write against
``search_docs`` and belongs on the writer thread with everything else. The FTS5
index is kept in sync by triggers, so writing ``search_docs`` is the only thing
the indexer has to remember to do.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from ...types import AnnotationSource
from ...util import utcnow_iso
from .base import Repository, placeholders, row_to_dict, rows_to_dicts

__all__ = ["TagRepository", "DerivativeRepository", "SearchDocRepository"]


class TagRepository(Repository):
    """Normalised tagging."""

    def get_or_create_tag(
        self,
        name: str,
        *,
        namespace: str = "user",
        color: str | None = None,
        parent_id: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        def _op(c: sqlite3.Connection) -> int:
            row = c.execute(
                "SELECT id FROM tags WHERE namespace = ? AND name = ?", (namespace, name)
            ).fetchone()
            if row is not None:
                return int(row["id"])
            return self._insert(
                c,
                "tags",
                {
                    "namespace": namespace, "name": name, "color": color,
                    "parent_id": parent_id, "created_at": utcnow_iso(),
                },
            )

        return self._write(_op, conn, label="get_or_create_tag")

    def tag_asset(
        self,
        asset_id: int,
        tag_id: int,
        *,
        source: AnnotationSource | str = AnnotationSource.USER,
        producer_id: int | None = None,
        confidence: float | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Attach a tag. The PK is ``(asset, tag, source)``, so a machine tag
        and a user tag of the same name coexist and can be told apart."""
        self._write(
            lambda c: c.execute(
                "INSERT INTO asset_tags(asset_id,tag_id,source,producer_id,confidence,created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(asset_id,tag_id,source) "
                "DO UPDATE SET confidence=excluded.confidence",
                (asset_id, tag_id, str(source), producer_id, confidence, utcnow_iso()),
            ),
            conn,
            label="tag_asset",
        )

    def untag_asset(
        self,
        asset_id: int,
        tag_id: int,
        *,
        source: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        def _op(c: sqlite3.Connection) -> int:
            if source:
                cur = c.execute(
                    "DELETE FROM asset_tags WHERE asset_id=? AND tag_id=? AND source=?",
                    (asset_id, tag_id, source),
                )
            else:
                cur = c.execute(
                    "DELETE FROM asset_tags WHERE asset_id=? AND tag_id=?", (asset_id, tag_id)
                )
            return cur.rowcount

        return self._write(_op, conn, label="untag_asset")

    def tags_for_asset(self, asset_id: int) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self._query(
                "SELECT t.id, t.namespace, t.name, t.color, at.source, at.confidence "
                "FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
                "WHERE at.asset_id = ? ORDER BY t.namespace, t.name",
                (asset_id,),
            )
        )

    def tag_names_for_asset(self, asset_id: int) -> str:
        """Space-joined tag names, for the FTS ``tags`` column."""
        rows = self._query(
            "SELECT DISTINCT t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            "WHERE at.asset_id = ?",
            (asset_id,),
        )
        return " ".join(str(r["name"]) for r in rows)

    def list_tags(self, *, namespace: str | None = None, with_counts: bool = True) -> list[dict[str, Any]]:
        """All tags. Counts come from the ``tag_frequency`` view."""
        base = (
            "SELECT t.*, COALESCE(f.n, 0) AS asset_count FROM tags t "
            "LEFT JOIN tag_frequency f ON f.tag_id = t.id"
            if with_counts
            else "SELECT t.* FROM tags t"
        )
        if namespace:
            return rows_to_dicts(
                self._query(f"{base} WHERE t.namespace = ? ORDER BY t.name", (namespace,))
            )
        return rows_to_dicts(self._query(f"{base} ORDER BY t.namespace, t.name"))

    def assets_with_tag(self, tag_id: int, limit: int = 1000) -> list[int]:
        rows = self._query(
            "SELECT DISTINCT asset_id FROM asset_tags WHERE tag_id = ? LIMIT ?", (tag_id, limit)
        )
        return [int(r["asset_id"]) for r in rows]

    def assets_with_tag_names(
        self, names: Sequence[str], *, require_all: bool = False, limit: int = 10_000
    ) -> list[int]:
        """Assets carrying the named tags. ``require_all`` switches OR to AND."""
        if not names:
            return []
        sql = (
            "SELECT at.asset_id FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            f"WHERE t.name IN ({placeholders(len(names))}) GROUP BY at.asset_id"
        )
        params: list[Any] = list(names)
        if require_all:
            sql += " HAVING COUNT(DISTINCT t.name) = ?"
            params.append(len(set(names)))
        sql += " LIMIT ?"
        params.append(limit)
        return [int(r["asset_id"]) for r in self._query(sql, params)]

    def delete_tag(self, tag_id: int, *, conn: sqlite3.Connection | None = None) -> None:
        self._write(
            lambda c: c.execute("DELETE FROM tags WHERE id = ?", (tag_id,)), conn, label="delete_tag"
        )

    def rename_tag(self, tag_id: int, name: str, *, conn: sqlite3.Connection | None = None) -> None:
        self._write(
            lambda c: self._update(c, "tags", {"name": name}, "id = ?", (tag_id,)),
            conn,
            label="rename_tag",
        )


class DerivativeRepository(Repository):
    """Index of generated thumbnails, proxies, keyframes and audio.

    Tracked in the database so the cache can be purged per-asset, or swept for
    orphans, without walking a tree of hundreds of thousands of files.
    """

    def record(
        self,
        asset_id: int,
        kind: str,
        variant: str,
        rel_path: str,
        *,
        size_bytes: int | None = None,
        width: int | None = None,
        height: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        return self._write(
            lambda c: self._upsert(
                c,
                "derivatives",
                {
                    "asset_id": asset_id, "kind": kind, "variant": variant,
                    "rel_path": rel_path, "size_bytes": size_bytes,
                    "width": width, "height": height, "created_at": utcnow_iso(),
                },
                ["asset_id", "kind", "variant"],
            ),
            conn,
            label="record_derivative",
        )

    def get(self, asset_id: int, kind: str, variant: str) -> dict[str, Any] | None:
        return row_to_dict(
            self._one(
                "SELECT * FROM derivatives WHERE asset_id=? AND kind=? AND variant=?",
                (asset_id, kind, variant),
            )
        )

    def for_asset(self, asset_id: int, *, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self._query(
                "SELECT * FROM derivatives WHERE asset_id = ? AND kind = ? ORDER BY variant",
                (asset_id, kind),
            )
        else:
            rows = self._query(
                "SELECT * FROM derivatives WHERE asset_id = ? ORDER BY kind, variant", (asset_id,)
            )
        return rows_to_dicts(rows)

    def keyframes(self, asset_id: int) -> list[dict[str, Any]]:
        """Keyframes in time order. ``variant`` is the timestamp as a string."""
        rows = self._query(
            "SELECT * FROM derivatives WHERE asset_id = ? AND kind = 'keyframe' "
            "ORDER BY CAST(variant AS REAL)",
            (asset_id,),
        )
        return rows_to_dicts(rows)

    def delete_for_asset(self, asset_id: int, *, conn: sqlite3.Connection | None = None) -> list[str]:
        """Forget an asset's derivatives; returns paths for the caller to unlink."""

        def _op(c: sqlite3.Connection) -> list[str]:
            paths = [
                str(r["rel_path"])
                for r in c.execute(
                    "SELECT rel_path FROM derivatives WHERE asset_id = ?", (asset_id,)
                ).fetchall()
            ]
            c.execute("DELETE FROM derivatives WHERE asset_id = ?", (asset_id,))
            return paths

        return self._write(_op, conn, label="delete_derivatives")

    def total_bytes(self) -> int:
        return int(self._scalar("SELECT COALESCE(SUM(size_bytes), 0) FROM derivatives") or 0)

    def counts_by_kind(self) -> dict[str, int]:
        rows = self._query("SELECT kind, COUNT(*) AS n FROM derivatives GROUP BY kind")
        return {str(r["kind"]): int(r["n"]) for r in rows}


class SearchDocRepository(Repository):
    """Writes the FTS content row for an asset.

    The FTS5 index is external-content over ``search_docs`` and kept in lockstep
    by triggers, so this is the only place that has to know how indexing works.
    """

    def upsert(
        self,
        asset_id: int,
        *,
        filename: str = "",
        tags: str = "",
        labels: str = "",
        doc_text: str = "",
        place_names: str = "",
        people: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        values = {
            "asset_id": asset_id,
            "filename": filename or "",
            "tags": tags or "",
            "labels": labels or "",
            "doc_text": doc_text or "",
            "place_names": place_names or "",
            "people": people or "",
        }
        self._write(
            lambda c: self._upsert(c, "search_docs", values, ["asset_id"]),
            conn,
            label="upsert_search_doc",
        )

    def delete(self, asset_id: int, *, conn: sqlite3.Connection | None = None) -> None:
        self._write(
            lambda c: c.execute("DELETE FROM search_docs WHERE asset_id = ?", (asset_id,)),
            conn,
            label="delete_search_doc",
        )

    def rebuild_index(self, *, conn: sqlite3.Connection | None = None) -> None:
        """Rebuild the FTS index from ``search_docs``. Repair operation."""
        self._write(
            lambda c: c.execute("INSERT INTO search_index(search_index) VALUES('rebuild')"),
            conn,
            label="rebuild_fts",
        )

    def optimize(self, *, conn: sqlite3.Connection | None = None) -> None:
        self._write(
            lambda c: c.execute("INSERT INTO search_index(search_index) VALUES('optimize')"),
            conn,
            label="optimize_fts",
        )

    def indexed_count(self) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM search_docs") or 0)
