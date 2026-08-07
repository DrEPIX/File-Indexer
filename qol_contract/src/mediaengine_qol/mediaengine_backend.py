"""Concrete adapter from validated QoL search plans to MediaEngine reads.

The planner is deliberately SQL-free and the engine repositories deliberately
do not know about public filter names.  This module is the narrow seam between
them.  Every field is mapped through an allowlist; editing ``change_sheet.toml``
can expose an existing field but can never inject SQL.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from typing import Any, cast

from .errors import QueryError
from .models import GroupOperator, PlannedClause, PlannedGroup, SearchPlan


_DIRECT_FIELDS = {
    "assets.media_type": "a.media_type",
    "assets.size_bytes": "a.size_bytes",
    "assets.captured_at": "a.captured_at",
    "assets.imported_at": "a.imported_at",
    "technical_metadata.width": "tm.width",
    "technical_metadata.height": "tm.height",
    "technical_metadata.duration_s": "tm.duration_s",
    "technical_metadata.frame_rate": "tm.frame_rate",
    "technical_metadata.video_codec": "tm.video_codec",
    "technical_metadata.audio_codec": "tm.audio_codec",
    "technical_metadata.page_count": "tm.page_count",
    "metadata.camera_make": "tm.camera_make",
    "metadata.camera_model": "tm.camera_model",
    "computed.file_copy_count": (
        "(SELECT COUNT(*) FROM files fc WHERE fc.asset_id=a.id AND fc.status='present')"
    ),
    "computed.orientation_class": (
        "CASE WHEN tm.width IS NULL OR tm.height IS NULL THEN NULL "
        "WHEN tm.width=tm.height THEN 'square' WHEN tm.width>tm.height "
        "THEN 'landscape' ELSE 'portrait' END"
    ),
}

_FILE_FIELDS = {
    "files.filename": "fx.filename",
    "files.extension": "fx.extension",
    "files.path": "fx.path",
    "files.status": "fx.status",
    "files.mtime_ns": "fx.mtime_ns",
}

_ANNOTATION_FIELDS = {
    "annotations.namespace": "ax.namespace",
    "annotations.label": "ax.label",
    "annotations.confidence": "ax.confidence",
    "annotations.source": "ax.source",
    "producers.plugin_id": "px.plugin_id",
}


class MediaEngineBackend:
    """Implement :class:`BackendPort` using a started ``MediaEngine``."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.engine.start()

    def capabilities(self) -> frozenset[str]:
        # Vector similarity stays gated until a query-vector producer is wired.
        return frozenset({"structured", "fts", "annotations", "tags", "identities", "spatial", "facets"})

    def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        self.engine.start()
        return cast(dict[str, Any] | None, self.engine.repos.assets.export_asset(asset_id))

    def list_facets(self, namespace: str | None = None) -> dict[str, Any]:
        self.engine.start()
        repos = self.engine.repos
        if namespace:
            return {"namespace": namespace, "values": repos.annotations.facet(namespace)}
        return {
            "namespaces": repos.annotations.namespaces_in_use(),
            "annotations": repos.annotations.all_facets(),
            "tags": repos.tags.list_tags(),
            "identities": repos.identities.list_identities(),
            "places": repos.places.list_places(),
        }

    def execute_search(self, plan: SearchPlan) -> dict[str, Any]:
        self.engine.start()
        params: list[Any] = []
        predicates: list[str] = []
        group_sql = self._group(plan.where, params)
        if group_sql:
            predicates.append(group_sql)

        join_fts = ""
        match = ""
        if plan.text:
            # Reuse the core search parser's FTS escaping so the API and CLI
            # cannot disagree on punctuation or prefix-search behaviour.
            from mediaengine.search import sanitize_match

            match = sanitize_match(plan.text)
        if match:
            join_fts = " JOIN search_index ON search_index.rowid=a.id AND search_index MATCH ?"
            params.insert(0, match)

        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        sort_expr = self._sort_expression(plan.sort_field, bool(match))
        direction = "ASC" if plan.direction.lower() == "asc" else "DESC"
        if plan.sort_field == "search.relevance":
            # bm25 is lower-is-better. Negation preserves the public desc contract.
            sort_expr = "-bm25(search_index)" if match else "a.imported_at"

        offset = self._decode_cursor(plan.cursor)
        sql = (
            "WITH primary_files AS ("
            " SELECT asset_id, MIN(id) AS file_id, COUNT(*) AS copy_count"
            " FROM files WHERE status='present' GROUP BY asset_id"
            ") "
            "SELECT a.id, a.content_hash, a.media_type, a.mime_type, a.size_bytes,"
            " a.captured_at, a.imported_at, pf.copy_count, f.path, f.filename,"
            " f.extension, f.status, tm.width, tm.height, tm.duration_s,"
            " tm.frame_rate, tm.video_codec, tm.audio_codec, tm.page_count,"
            " tm.camera_make, tm.camera_model"
            " FROM assets a"
            f"{join_fts}"
            " LEFT JOIN primary_files pf ON pf.asset_id=a.id"
            " LEFT JOIN files f ON f.id=pf.file_id"
            " LEFT JOIN technical_metadata tm ON tm.asset_id=a.id"
            f"{where} ORDER BY {sort_expr} {direction}, a.id {direction} LIMIT ? OFFSET ?"
        )
        rows = self.engine.db.query(sql, [*params, plan.page_size + 1, offset])
        has_more = len(rows) > plan.page_size
        rows = rows[: plan.page_size]
        items = [dict(row) for row in rows]
        asset_ids = [int(item["id"]) for item in items]

        count_sql = f"SELECT COUNT(*) FROM assets a{join_fts} LEFT JOIN technical_metadata tm ON tm.asset_id=a.id{where}"
        total = int(self.engine.db.scalar(count_sql, params) or 0)
        facets: dict[str, Any] = {}
        if plan.include_facets:
            requested = plan.facet_namespaces
            if requested:
                facets = {
                    name: self.engine.repos.annotations.facet(name, asset_ids=asset_ids)
                    for name in requested
                }
            else:
                facets = self.engine.repos.annotations.all_facets(asset_ids=asset_ids)

        return {
            "items": items,
            "total": total,
            "page_size": plan.page_size,
            "next_cursor": self._encode_cursor(offset + len(items)) if has_more else None,
            "facets": facets,
        }

    def _group(self, group: PlannedGroup, params: list[Any]) -> str:
        parts = [self._clause(clause, params) for clause in group.clauses]
        parts.extend(value for child in group.groups if (value := self._group(child, params)))
        if not parts:
            return "1=1" if group.operator is GroupOperator.ALL else "1=0"
        joiner = " AND " if group.operator is GroupOperator.ALL else " OR "
        return "(" + joiner.join(parts) + ")"

    def _clause(self, clause: PlannedClause, params: list[Any]) -> str:
        field = clause.backend_field
        if field in _DIRECT_FIELDS:
            return self._value_predicate(_DIRECT_FIELDS[field], clause.operator, clause.value, params)
        if field in _FILE_FIELDS:
            return self._related_predicate(
                "files fx", "fx.asset_id=a.id", _FILE_FIELDS[field], clause.operator, clause.value, params
            )
        if field in _ANNOTATION_FIELDS:
            return self._related_predicate(
                "annotations ax JOIN producers px ON px.id=ax.producer_id",
                "ax.asset_id=a.id AND ax.superseded_by IS NULL",
                _ANNOTATION_FIELDS[field], clause.operator, clause.value, params,
            )
        if field == "tags.name":
            return self._related_predicate(
                "asset_tags atx JOIN tags tx ON tx.id=atx.tag_id",
                "atx.asset_id=a.id", "tx.name", clause.operator, clause.value, params,
            )
        if field == "identities.id":
            return self._related_predicate(
                "regions rx JOIN region_identity rix ON rix.region_id=rx.id",
                "rx.asset_id=a.id", "rix.identity_id", clause.operator, clause.value, params,
            )
        if field == "places.id":
            return self._related_predicate(
                "asset_locations alx", "alx.asset_id=a.id", "alx.place_id",
                clause.operator, clause.value, params,
            )
        if field == "computed.has_people":
            exists = "EXISTS (SELECT 1 FROM regions rx WHERE rx.asset_id=a.id AND rx.kind='face')"
            return exists if clause.operator == "is_true" else f"NOT ({exists})"
        if field == "computed.has_location":
            exists = "EXISTS (SELECT 1 FROM asset_locations alx WHERE alx.asset_id=a.id)"
            return exists if clause.operator == "is_true" else f"NOT ({exists})"
        if field == "asset_geo":
            return self._geo_predicate(clause.operator, clause.value, params)
        if field == "embeddings.asset_id":
            raise QueryError("semantic similarity is unavailable until a query-vector producer is enabled")
        raise QueryError(f"backend field is not implemented: {field!r}")

    def _related_predicate(
        self,
        table: str,
        correlation: str,
        expression: str,
        operator: str,
        value: Any,
        params: list[Any],
    ) -> str:
        negative = operator in {"neq", "not_in", "not_under"}
        positive = {"neq": "eq", "not_in": "in", "not_under": "under"}.get(operator, operator)
        condition = self._value_predicate(expression, positive, value, params)
        exists = f"EXISTS (SELECT 1 FROM {table} WHERE {correlation} AND {condition})"
        return f"NOT ({exists})" if negative else exists

    @staticmethod
    def _value_predicate(expression: str, operator: str, value: Any, params: list[Any]) -> str:
        binary = {
            "eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
            "before": "<", "after": ">",
        }
        if operator in binary:
            params.append(value)
            return f"{expression} {binary[operator]} ?"
        if operator in {"exists", "missing"}:
            return f"{expression} IS {'NOT ' if operator == 'exists' else ''}NULL"
        if operator in {"is_true", "is_false"}:
            return f"COALESCE({expression},0) = {1 if operator == 'is_true' else 0}"
        if operator in {"contains", "starts_with", "ends_with", "under"}:
            text = str(value)
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = {
                "contains": f"%{escaped}%",
                "starts_with": f"{escaped}%",
                "ends_with": f"%{escaped}",
                "under": f"{escaped.rstrip('/\\\\')}%",
            }[operator]
            params.append(pattern)
            return f"{expression} LIKE ? ESCAPE '\\'"
        if operator == "regex":
            params.append(value)
            return f"{expression} REGEXP ?"
        if operator in {"in", "not_in"}:
            values = list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
            if not values:
                return "1=0" if operator == "in" else "1=1"
            params.extend(values)
            keyword = "IN" if operator == "in" else "NOT IN"
            return f"{expression} {keyword} ({','.join('?' for _ in values)})"
        if operator == "between":
            values = list(value)
            params.extend(values)
            return f"{expression} BETWEEN ? AND ?"
        raise QueryError(f"operator is not implemented by the backend: {operator!r}")

    @staticmethod
    def _geo_predicate(operator: str, value: Any, params: list[Any]) -> str:
        if not isinstance(value, dict):
            raise QueryError("geo filter value must be an object")
        try:
            if operator == "within_bbox":
                raw_south = value.get("min_lat", value.get("south"))
                raw_north = value.get("max_lat", value.get("north"))
                raw_west = value.get("min_lon", value.get("west"))
                raw_east = value.get("max_lon", value.get("east"))
                if (
                    raw_south is None
                    or raw_north is None
                    or raw_west is None
                    or raw_east is None
                ):
                    raise ValueError("bbox requires south, north, west, and east")
                south = float(raw_south)
                north = float(raw_north)
                west = float(raw_west)
                east = float(raw_east)
                params.extend([south, north, west, east])
                return (
                    "EXISTS (SELECT 1 FROM asset_geo gx WHERE gx.id=a.id "
                    "AND gx.max_lat>=? AND gx.min_lat<=? AND gx.max_lon>=? AND gx.min_lon<=?)"
                )
            if operator == "within_radius":
                lat = float(value["latitude"])
                lon = float(value["longitude"])
                radius = float(value["radius_km"])
                params.extend([lat, lon, radius])
                return (
                    "EXISTS (SELECT 1 FROM asset_locations alx WHERE alx.asset_id=a.id "
                    "AND haversine_km(?,?,alx.latitude,alx.longitude)<=?)"
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise QueryError(f"invalid geo filter: {value!r}") from exc
        raise QueryError(f"geo operator is not implemented: {operator!r}")

    @staticmethod
    def _sort_expression(field: str, has_text: bool) -> str:
        fields = {
            "assets.captured_at": "COALESCE(a.captured_at,a.imported_at)",
            "assets.imported_at": "a.imported_at",
            "files.filename": "LOWER(COALESCE(f.filename,''))",
            "assets.size_bytes": "a.size_bytes",
            "technical_metadata.duration_s": "COALESCE(tm.duration_s,0)",
            "search.relevance": "-bm25(search_index)" if has_text else "a.imported_at",
        }
        try:
            return fields[field]
        except KeyError as exc:
            raise QueryError(f"sort field is not implemented: {field!r}") from exc

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            value = json.loads(raw.decode("utf-8"))
            offset = int(value["offset"])
            if offset < 0:
                raise ValueError
            return offset
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            raise QueryError("invalid search cursor") from exc

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        raw = json.dumps({"offset": offset}, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
