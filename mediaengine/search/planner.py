"""Query execution: FTS, structured filters, facets — in one round trip.

The planner compiles a :class:`~mediaengine.search.query.Query` into a single
SQL statement over ``assets``: full text becomes a join against the FTS5
index, each label filter becomes an ``EXISTS`` against ``annotations``, and
the scalar filters land in the ``WHERE``. SQLite runs the whole thing off
covering indexes; there is no candidate-set assembly in Python, because
intersecting id lists by hand is exactly the work the database exists to do.

Facets are computed *from the matched set* at query time, grouped by
namespace, by the repository layer. That is the payoff of the whole design:
the facet panel for a query is whatever namespaces its results actually carry
— a plugin installed this morning shows up as a filter group by lunch, with no
code change anywhere.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..db.repositories import Repositories
from ..errors import QueryError
from .query import LabelFilter, Query

__all__ = ["SearchPlanner", "SearchResult", "sanitize_match"]

#: Hard ceiling on facet scoping. Beyond this many matches, facet counts are
#: computed over the first N ids — a documented approximation, not a bug: an
#: IN list of 100k ids costs more than the counts are worth.
_FACET_SCOPE_CAP = 5000

_FTS_TOKEN = re.compile(r"[^\s\"'*:^]+")


def sanitize_match(text: str) -> str:
    """Turn free text into an FTS5 MATCH expression that cannot error.

    FTS5 has its own query language; a stray ``-`` or ``:`` from a filename
    raises a syntax error at query time. Every token is therefore quoted, and
    the final token gets a ``*`` so the search box behaves as type-ahead.
    """
    tokens = _FTS_TOKEN.findall(text or "")
    if not tokens:
        return ""
    quoted = [f'"{token}"' for token in tokens]
    quoted[-1] = f"{quoted[-1]}*" if len(tokens[-1]) >= 2 else quoted[-1]
    return " ".join(quoted)


class SearchResult(dict[str, Any]):
    """A plain dict subclass so callers can treat it as JSON directly.

    The result rows live under the ``"items"`` key for JSON consumers; the
    typed accessor is :attr:`hits`, because shadowing :meth:`dict.items` would
    break every generic consumer of the mapping.
    """

    @property
    def total(self) -> int:
        return int(self.get("total", 0))

    @property
    def hits(self) -> list[dict[str, Any]]:
        return list(self.get("items", []))


class SearchPlanner:
    """Compiles and runs queries against one :class:`Repositories`."""

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos

    # ── public ──────────────────────────────────────────────────────────────

    def search(self, query: Query, *, with_facets: bool = True) -> SearchResult:
        """Run one query; returns items, total, facets and timing."""
        started = time.monotonic()
        where, joins, params, order = self._compile(query)

        base = (
            "FROM assets a "
            "LEFT JOIN technical_metadata t ON t.asset_id = a.id "
            "LEFT JOIN asset_primary_file f ON f.asset_id = a.id "
            + " ".join(joins)
        )
        clause = f" WHERE {' AND '.join(where)}" if where else ""

        total = int(
            self.repos.db.scalar(f"SELECT COUNT(*) {base}{clause}", params) or 0
        )

        rows = self.repos.db.query(
            "SELECT a.id, a.media_type, a.mime_type, a.size_bytes, a.captured_at, "
            "a.perceptual_hash, a.content_hash, t.width, t.height, t.duration_s, "
            "t.camera_make, t.camera_model, f.path AS primary_path "
            f"{base}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, max(1, min(query.limit, 1000)), max(0, query.offset)],
        )
        items = [self._shape(dict(row)) for row in rows]

        result = SearchResult(
            total=total,
            items=items,
            limit=query.limit,
            offset=query.offset,
            took_ms=round((time.monotonic() - started) * 1000, 1),
        )
        if with_facets:
            result["facets"] = self._facets(base, clause, params, total, query)
        return result

    # ── compilation ─────────────────────────────────────────────────────────

    def _compile(self, query: Query) -> tuple[list[str], list[str], list[Any], str]:
        where: list[str] = []
        joins: list[str] = []
        params: list[Any] = []

        match = sanitize_match(query.text)
        if match:
            # An INNER JOIN against the term index: non-matching assets fall
            # out before any other predicate runs.
            joins.append(
                "JOIN (SELECT rowid AS fts_id, bm25(search_index) AS rank "
                "FROM search_index WHERE search_index MATCH ?) s ON s.fts_id = a.id"
            )
            params.append(match)

        if query.media_types:
            marks = ",".join("?" * len(query.media_types))
            where.append(f"a.media_type IN ({marks})")
            params.extend(query.media_types)

        for index, label_filter in enumerate(query.labels):
            clause, clause_params = self._label_exists(label_filter, query, index)
            where.append(clause)
            params.extend(clause_params)

        for index, label_filter in enumerate(query.excluded_labels):
            clause, clause_params = self._label_exists(
                label_filter, query, index, alias_prefix="ex"
            )
            where.append(f"NOT ({clause})")
            params.extend(clause_params)

        if query.camera:
            where.append("(t.camera_make LIKE ? OR t.camera_model LIKE ?)")
            like = f"%{query.camera}%"
            params.extend([like, like])

        if query.extension:
            where.append(
                "EXISTS (SELECT 1 FROM files fx WHERE fx.asset_id = a.id AND fx.extension = ?)"
            )
            params.append(query.extension)

        # ISO-8601-Z strings compare chronologically; that storage guarantee is
        # what lets a date range be two string comparisons.
        if query.captured_after:
            where.append("a.captured_at >= ?")
            params.append(query.captured_after)
        if query.captured_before:
            where.append("a.captured_at <= ?")
            params.append(query.captured_before)

        if query.has_location is True:
            where.append("EXISTS (SELECT 1 FROM asset_geo g WHERE g.id = a.id)")
        elif query.has_location is False:
            where.append("NOT EXISTS (SELECT 1 FROM asset_geo g WHERE g.id = a.id)")

        order = self._order(query, text_ranked=bool(match))
        return where, joins, params, order

    def _label_exists(
        self,
        label_filter: LabelFilter,
        query: Query,
        index: int,
        *,
        alias_prefix: str = "an",
    ) -> tuple[str, list[Any]]:
        """One label filter as an EXISTS subquery.

        Namespace matching is prefix-aware (``color`` also matches
        ``color.dominant``) to mirror the repository's facet semantics —
        the two must agree or clicking a facet value could yield zero results.
        """
        alias = f"{alias_prefix}{index}"
        conditions = [
            f"{alias}.asset_id = a.id",
            f"{alias}.superseded_by IS NULL",
            f"({alias}.namespace = ? OR {alias}.namespace LIKE ?)",
        ]
        params: list[Any] = [label_filter.namespace, f"{label_filter.namespace}.%"]

        if label_filter.label is not None:
            conditions.append(f"{alias}.label = ?")
            params.append(label_filter.label)

        threshold = (
            label_filter.min_confidence
            if label_filter.min_confidence is not None
            else query.min_confidence
        )
        if threshold is not None:
            conditions.append(f"({alias}.confidence IS NULL OR {alias}.confidence >= ?)")
            params.append(threshold)

        if query.sources:
            marks = ",".join("?" * len(query.sources))
            conditions.append(f"{alias}.source IN ({marks})")
            params.extend(query.sources)

        return (
            f"EXISTS (SELECT 1 FROM annotations {alias} WHERE {' AND '.join(conditions)})",
            params,
        )

    @staticmethod
    def _order(query: Query, *, text_ranked: bool) -> str:
        direction = "DESC" if query.descending else "ASC"
        if query.sort == "relevance":
            if text_ranked:
                # bm25 returns lower-is-better; direction applies to recency
                # tiebreak only. Rank order is not something users flip.
                return "s.rank, a.captured_at DESC NULLS LAST, a.id DESC"
            return "a.captured_at DESC NULLS LAST, a.id DESC"
        column = {
            "captured_at": "a.captured_at",
            "imported_at": "a.imported_at",
            "size_bytes": "a.size_bytes",
        }.get(query.sort)
        if column is None:
            raise QueryError(f"unknown sort key: {query.sort!r}")
        nulls = " NULLS LAST" if column == "a.captured_at" else ""
        return f"{column} {direction}{nulls}, a.id {direction}"

    # ── output shaping ──────────────────────────────────────────────────────

    @staticmethod
    def _shape(row: dict[str, Any]) -> dict[str, Any]:
        from pathlib import PurePath

        path = row.pop("primary_path", None)
        row["filename"] = PurePath(str(path)).name if path else None
        row["path"] = path
        return row

    def _facets(
        self,
        base: str,
        clause: str,
        params: list[Any],
        total: int,
        query: Query,
    ) -> dict[str, list[dict[str, Any]]]:
        """Facet counts scoped to this query's matches.

        For an unfiltered query the scope is the whole library, and passing
        ``asset_ids=None`` lets the repository use its indexes directly rather
        than materialising every id.
        """
        scoped_ids: list[int] | None = None
        if not query.is_empty():
            rows = self.repos.db.query(
                f"SELECT a.id {base}{clause} LIMIT ?", [*params, _FACET_SCOPE_CAP]
            )
            scoped_ids = [int(r["id"]) for r in rows]
            if not scoped_ids:
                return {}
        facets = self.repos.annotations.all_facets(
            asset_ids=scoped_ids, min_confidence=query.min_confidence
        )
        # Media type is the one facet not derived from annotations — it is an
        # engine column — and the UI wants it in the same shape.
        type_rows = self.repos.db.query(
            f"SELECT a.media_type AS label, COUNT(*) AS count {base}{clause} "
            "GROUP BY a.media_type ORDER BY count DESC",
            params,
        )
        if type_rows:
            facets = {
                "media.type": [
                    {"namespace": "media.type", "label": str(r["label"]),
                     "count": int(r["count"]), "avg_confidence": None}
                    for r in type_rows
                ],
                **facets,
            }
        return facets
