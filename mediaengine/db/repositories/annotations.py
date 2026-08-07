"""Producers, regions, annotations and namespaces — the extension point.

This is the module that makes taxonomies pluggable. The core never learns what
``garment.color`` means; it stores the assertion, records who made it, and lets
the search layer group by namespace at query time.

Three invariants are enforced here and nowhere else:

**Provenance.** Every annotation carries a ``producer_id`` resolving to
``(plugin_id, version, model_id, config_hash)``. Nothing derived is anonymous.

**User supremacy.** :meth:`commit` will not supersede or delete an annotation
whose ``source`` is ``user``. A human correction survives re-indexing, model
upgrades and full re-analysis (principle 4).

**Supersession, not deletion.** When a new producer version commits, the prior
version's claims are marked ``superseded_by`` rather than dropped, so the
question "which model version claimed this, and when" stays answerable. A claim
the new version *retracted* — it no longer emits that label at all — is marked
by pointing ``superseded_by`` at the row's own id. That self-reference is the
documented convention for "retracted"; it keeps the row out of every live view
(all of which filter ``superseded_by IS NULL``) while preserving the history.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...errors import ImmutableUserDataError
from ...types import AnnotationSource
from ...util import json_dumps, json_loads, utcnow_iso
from .base import Repository, placeholders, row_to_dict, rows_to_dicts

__all__ = ["AnnotationRepository", "CommitResult", "PendingAnnotation", "PendingRegion"]


@dataclass(slots=True)
class PendingRegion:
    """A bounding box a plugin returned, before it has a database id."""

    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    frame_time: float | None = None
    page_number: int | None = None
    kind: str | None = None

    def key(self) -> tuple[Any, ...]:
        """Identity for de-duplication within one commit."""
        return (self.x, self.y, self.w, self.h, self.frame_time, self.page_number, self.kind)


@dataclass(slots=True)
class PendingAnnotation:
    """A plugin's claim, normalised and ready to commit."""

    namespace: str
    label: str
    value: dict[str, Any] | None = None
    confidence: float | None = None
    region: PendingRegion | None = None
    embedding: Sequence[float] | None = None
    source: AnnotationSource = AnnotationSource.DERIVED


@dataclass(slots=True)
class CommitResult:
    """What a commit actually changed. Returned so callers can emit events."""

    inserted: int = 0
    updated: int = 0
    superseded: int = 0
    retracted: int = 0
    regions_created: int = 0
    embeddings_created: int = 0
    blocked_by_user: int = 0
    annotation_ids: list[int] = field(default_factory=list)

    def total(self) -> int:
        return self.inserted + self.updated


class AnnotationRepository(Repository):
    """Reads and writes for the generic annotation layer."""

    # ── producers ───────────────────────────────────────────────────────────

    def get_or_create_producer(
        self,
        plugin_id: str,
        version: str,
        *,
        model_id: str | None = None,
        config_hash: str | None = None,
        transport: str = "in_process",
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Resolve the producer tuple to an id, creating it if new.

        ``model_id`` and ``config_hash`` are stored as ``''`` rather than NULL
        because SQLite treats NULLs as distinct in a UNIQUE constraint, which
        would create a fresh producer row on every single call.
        """
        values = {
            "plugin_id": plugin_id,
            "version": version,
            "model_id": model_id or "",
            "config_hash": config_hash or "",
            "transport": transport,
        }

        def _op(c: sqlite3.Connection) -> int:
            row = c.execute(
                "SELECT id FROM producers WHERE plugin_id=? AND version=? "
                "AND model_id=? AND config_hash=?",
                (values["plugin_id"], values["version"], values["model_id"], values["config_hash"]),
            ).fetchone()
            if row is not None:
                return int(row["id"])
            return self._insert(c, "producers", {**values, "first_seen_at": utcnow_iso()})

        return self._write(_op, conn, label="get_or_create_producer")

    def get_producer(self, producer_id: int) -> dict[str, Any] | None:
        return row_to_dict(self._one("SELECT * FROM producers WHERE id = ?", (producer_id,)))

    def list_producers(self) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM annotations a WHERE a.producer_id = p.id) AS annotation_count, "
            "(SELECT COUNT(*) FROM embeddings e WHERE e.producer_id = p.id) AS embedding_count "
            "FROM producers p ORDER BY p.plugin_id, p.version"
        )
        return rows_to_dicts(rows)

    def producer_ids_for_plugin(self, plugin_id: str, *, exclude: int | None = None) -> list[int]:
        rows = self._query("SELECT id FROM producers WHERE plugin_id = ?", (plugin_id,))
        return [int(r["id"]) for r in rows if exclude is None or int(r["id"]) != exclude]

    # ── the commit path ─────────────────────────────────────────────────────

    def commit(
        self,
        asset_id: int,
        producer_id: int,
        plugin_id: str,
        annotations: Iterable[PendingAnnotation],
        *,
        supersede_prior_versions: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> CommitResult:
        """Atomically write one plugin's output for one asset.

        Everything here happens in a single transaction: either the whole
        plugin run lands or none of it does. Partial output would make
        ``analysis_tasks.state = 'done'`` a lie.
        """
        pending = list(annotations)

        def _op(c: sqlite3.Connection) -> CommitResult:
            result = CommitResult()
            now = utcnow_iso()

            # A plugin may emit several annotations sharing one box. Create
            # each distinct region once and reuse the id.
            #
            # Re-use also has to survive across *runs*, not just within one:
            # re-running the same producer must not create a second identical
            # region, or the annotation attached to it would get a different
            # region_id and slip past the unique index, silently duplicating
            # everything on every re-analysis. So look for an existing
            # geometrically identical region from this producer first.
            region_ids: dict[tuple[Any, ...], int] = {}
            for item in pending:
                if item.region is None:
                    continue
                key = item.region.key()
                if key in region_ids:
                    continue
                reg = item.region
                existing = c.execute(
                    "SELECT id FROM regions WHERE asset_id = ? AND producer_id = ? "
                    "AND x IS ? AND y IS ? AND w IS ? AND h IS ? "
                    "AND frame_time IS ? AND page_number IS ? AND kind IS ?",
                    (
                        asset_id, producer_id, reg.x, reg.y, reg.w, reg.h,
                        reg.frame_time, reg.page_number, reg.kind,
                    ),
                ).fetchone()
                if existing is not None:
                    region_ids[key] = int(existing["id"])
                    continue
                region_ids[key] = self._insert(
                    c,
                    "regions",
                    {
                        "asset_id": asset_id,
                        "frame_time": reg.frame_time,
                        "page_number": reg.page_number,
                        "x": reg.x,
                        "y": reg.y,
                        "w": reg.w,
                        "h": reg.h,
                        "kind": reg.kind,
                        "producer_id": producer_id,
                        "created_at": now,
                    },
                )
                result.regions_created += 1

            # Which (namespace,label) pairs a user has already spoken on for
            # this asset. Those are off limits.
            user_locked = {
                (str(r["namespace"]), str(r["label"]))
                for r in c.execute(
                    "SELECT namespace, label FROM annotations "
                    "WHERE asset_id = ? AND source = 'user' AND superseded_by IS NULL",
                    (asset_id,),
                ).fetchall()
            }

            new_by_key: dict[tuple[str, str], int] = {}
            for item in pending:
                key_nl = (item.namespace, item.label)
                if key_nl in user_locked:
                    result.blocked_by_user += 1
                    continue

                region_id = region_ids.get(item.region.key()) if item.region else None
                row = {
                    "asset_id": asset_id,
                    "region_id": region_id,
                    "namespace": item.namespace,
                    "label": item.label,
                    "value_json": json_dumps(item.value) if item.value is not None else None,
                    "confidence": item.confidence,
                    "source": str(item.source),
                    "producer_id": producer_id,
                    "created_at": now,
                }
                # The unique index makes a repeat run of the same producer a
                # no-op rather than a duplicate row (principle 5).
                cur = c.execute(
                    "INSERT INTO annotations"
                    "(asset_id,region_id,namespace,label,value_json,confidence,source,"
                    " producer_id,created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(asset_id, COALESCE(region_id,-1), namespace, label, producer_id) "
                    "DO UPDATE SET value_json=excluded.value_json, "
                    "confidence=excluded.confidence, superseded_by=NULL "
                    "RETURNING id, created_at",
                    (
                        row["asset_id"], row["region_id"], row["namespace"], row["label"],
                        row["value_json"], row["confidence"], row["source"],
                        row["producer_id"], row["created_at"],
                    ),
                )
                returned = cur.fetchone()
                cur.close()
                annotation_id = int(returned["id"])
                # DO UPDATE deliberately leaves created_at alone, so a returned
                # timestamp older than this commit's means the row already
                # existed. `excluded.created_at` is not visible in RETURNING.
                if str(returned["created_at"]) != now:
                    result.updated += 1
                else:
                    result.inserted += 1
                result.annotation_ids.append(annotation_id)
                new_by_key.setdefault(key_nl, annotation_id)

                if item.embedding:
                    self._insert_embedding(
                        c,
                        asset_id=asset_id,
                        region_id=region_id,
                        producer_id=producer_id,
                        vector=item.embedding,
                        kind=item.namespace,
                        now=now,
                    )
                    result.embeddings_created += 1

            if supersede_prior_versions:
                superseded, retracted = self._supersede_prior(
                    c, asset_id, plugin_id, producer_id, new_by_key
                )
                result.superseded = superseded
                result.retracted = retracted

            return result

        return self._write(_op, conn, label="commit_annotations")

    def _supersede_prior(
        self,
        c: sqlite3.Connection,
        asset_id: int,
        plugin_id: str,
        producer_id: int,
        new_by_key: dict[tuple[str, str], int],
    ) -> tuple[int, int]:
        """Retire the previous producer version's claims for this asset.

        Never touches ``source = 'user'`` rows — that is the whole point.
        """
        old = c.execute(
            "SELECT a.id, a.namespace, a.label FROM annotations a "
            "JOIN producers p ON p.id = a.producer_id "
            "WHERE a.asset_id = ? AND p.plugin_id = ? AND a.producer_id <> ? "
            "AND a.superseded_by IS NULL AND a.source <> 'user'",
            (asset_id, plugin_id, producer_id),
        ).fetchall()

        superseded = retracted = 0
        for row in old:
            old_id = int(row["id"])
            replacement = new_by_key.get((str(row["namespace"]), str(row["label"])))
            if replacement is not None:
                c.execute(
                    "UPDATE annotations SET superseded_by = ? WHERE id = ?", (replacement, old_id)
                )
                superseded += 1
            else:
                # The new version no longer makes this claim. Self-reference
                # marks it retracted: excluded from live views, still auditable.
                c.execute(
                    "UPDATE annotations SET superseded_by = id WHERE id = ?", (old_id,)
                )
                retracted += 1
        return superseded, retracted

    @staticmethod
    def _insert_embedding(
        c: sqlite3.Connection,
        *,
        asset_id: int | None,
        region_id: int | None,
        producer_id: int,
        vector: Sequence[float],
        kind: str,
        now: str,
    ) -> int:
        import array
        import math

        packed = array.array("f", [float(v) for v in vector])
        norm = math.sqrt(sum(v * v for v in packed)) or 1.0
        # Idempotent: re-running a producer replaces its vector rather than
        # appending a second one, which would double-count in similarity search.
        if region_id is not None:
            c.execute(
                "DELETE FROM embeddings WHERE region_id = ? AND producer_id = ? AND kind = ?",
                (region_id, producer_id, kind),
            )
        else:
            c.execute(
                "DELETE FROM embeddings WHERE asset_id = ? AND region_id IS NULL "
                "AND producer_id = ? AND kind = ?",
                (asset_id, producer_id, kind),
            )
        cur = c.execute(
            "INSERT INTO embeddings(asset_id,region_id,producer_id,kind,dim,norm,vector,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                asset_id if region_id is None else None,
                region_id,
                producer_id,
                kind,
                len(packed),
                norm,
                packed.tobytes(),
                now,
            ),
        )
        try:
            return int(cur.lastrowid or 0)
        finally:
            cur.close()

    # ── user annotations ────────────────────────────────────────────────────

    def add_user_annotation(
        self,
        asset_id: int,
        namespace: str,
        label: str,
        *,
        value: dict[str, Any] | None = None,
        region: PendingRegion | None = None,
        producer_id: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Record a human's assertion. Outranks everything a plugin can say.

        Any live machine claim on the same ``(namespace, label)`` for this asset
        is superseded by the new user row immediately.
        """

        def _op(c: sqlite3.Connection) -> int:
            pid = producer_id
            if pid is None:
                row = c.execute(
                    "SELECT id FROM producers WHERE plugin_id='core.user' AND version='1'"
                ).fetchone()
                pid = (
                    int(row["id"])
                    if row
                    else self._insert(
                        c,
                        "producers",
                        {
                            "plugin_id": "core.user",
                            "version": "1",
                            "model_id": "",
                            "config_hash": "",
                            "transport": "user",
                            "first_seen_at": utcnow_iso(),
                        },
                    )
                )

            region_id: int | None = None
            now = utcnow_iso()
            if region is not None:
                region_id = self._insert(
                    c,
                    "regions",
                    {
                        "asset_id": asset_id,
                        "frame_time": region.frame_time,
                        "page_number": region.page_number,
                        "x": region.x, "y": region.y, "w": region.w, "h": region.h,
                        "kind": region.kind,
                        "producer_id": pid,
                        "created_at": now,
                    },
                )

            cur = c.execute(
                "INSERT INTO annotations"
                "(asset_id,region_id,namespace,label,value_json,confidence,source,producer_id,created_at)"
                " VALUES (?,?,?,?,?,1.0,'user',?,?) "
                "ON CONFLICT(asset_id, COALESCE(region_id,-1), namespace, label, producer_id) "
                "DO UPDATE SET value_json=excluded.value_json, superseded_by=NULL "
                "RETURNING id",
                (
                    asset_id, region_id, namespace, label,
                    json_dumps(value) if value is not None else None, pid, now,
                ),
            )
            new_id = int(cur.fetchone()["id"])
            cur.close()

            c.execute(
                "UPDATE annotations SET superseded_by = ? "
                "WHERE asset_id = ? AND namespace = ? AND label = ? "
                "AND id <> ? AND source <> 'user' AND superseded_by IS NULL",
                (new_id, asset_id, namespace, label, new_id),
            )
            return new_id

        return self._write(_op, conn, label="add_user_annotation")

    def delete_annotation(
        self, annotation_id: int, *, allow_user: bool = False, conn: sqlite3.Connection | None = None
    ) -> bool:
        """Delete one annotation. Refuses user rows unless explicitly allowed."""

        def _op(c: sqlite3.Connection) -> bool:
            row = c.execute(
                "SELECT source FROM annotations WHERE id = ?", (annotation_id,)
            ).fetchone()
            if row is None:
                return False
            if row["source"] == "user" and not allow_user:
                raise ImmutableUserDataError(
                    f"annotation {annotation_id} is user data; pass allow_user=True to delete"
                )
            c.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
            return True

        return self._write(_op, conn, label="delete_annotation")

    # ── reads ───────────────────────────────────────────────────────────────

    def for_asset(
        self,
        asset_id: int,
        *,
        namespace: str | None = None,
        include_superseded: bool = False,
        min_confidence: float | None = None,
        sources: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Annotations on one asset, joined to producer and region detail."""
        clauses = ["a.asset_id = ?"]
        params: list[Any] = [asset_id]
        if not include_superseded:
            clauses.append("a.superseded_by IS NULL")
        if namespace:
            # Prefix match so 'garment' also returns 'garment.color'.
            clauses.append("(a.namespace = ? OR a.namespace LIKE ?)")
            params.extend([namespace, f"{namespace}.%"])
        if min_confidence is not None:
            clauses.append("(a.confidence IS NULL OR a.confidence >= ?)")
            params.append(min_confidence)
        if sources:
            clauses.append(f"a.source IN ({placeholders(len(sources))})")
            params.extend(sources)

        rows = self._query(
            "SELECT a.*, p.plugin_id, p.version AS producer_version, p.model_id, "
            "r.x, r.y, r.w, r.h, r.frame_time, r.page_number, r.kind AS region_kind "
            "FROM annotations a "
            "JOIN producers p ON p.id = a.producer_id "
            "LEFT JOIN regions r ON r.id = a.region_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY a.namespace, a.confidence DESC NULLS LAST, a.label",
            params,
        )
        out = rows_to_dicts(rows)
        for item in out:
            item["value"] = json_loads(item.pop("value_json", None))
        return out

    def asset_ids_with(
        self,
        namespace: str,
        label: str | None = None,
        *,
        min_confidence: float | None = None,
        sources: Sequence[str] | None = None,
        limit: int = 10_000,
    ) -> list[int]:
        """Asset ids matching a namespace/label. The primitive search builds on."""
        clauses = ["superseded_by IS NULL", "(namespace = ? OR namespace LIKE ?)"]
        params: list[Any] = [namespace, f"{namespace}.%"]
        if label is not None:
            clauses.append("label = ?")
            params.append(label)
        if min_confidence is not None:
            clauses.append("(confidence IS NULL OR confidence >= ?)")
            params.append(min_confidence)
        if sources:
            clauses.append(f"source IN ({placeholders(len(sources))})")
            params.extend(sources)
        params.append(limit)
        rows = self._query(
            f"SELECT DISTINCT asset_id FROM annotations WHERE {' AND '.join(clauses)} LIMIT ?",
            params,
        )
        return [int(r["asset_id"]) for r in rows]

    def labels_for_asset(self, asset_id: int) -> str:
        """Space-joined live labels, for the FTS ``labels`` column."""
        rows = self._query(
            "SELECT DISTINCT label FROM annotations "
            "WHERE asset_id = ? AND superseded_by IS NULL",
            (asset_id,),
        )
        return " ".join(str(r["label"]) for r in rows)

    # ── facets: the payoff of the whole design ──────────────────────────────

    def namespaces_in_use(self, *, min_count: int = 1) -> list[dict[str, Any]]:
        """Every namespace that actually has live annotations, with counts.

        Computed from data, not from a registry. A plugin the UI has never
        heard of shows up here the moment it commits its first annotation, and
        the UI renders a filter control for it with no code change. This is the
        whole point of the design.
        """
        rows = self._query(
            "SELECT a.namespace, COUNT(*) AS total, "
            "COUNT(DISTINCT a.label) AS distinct_labels, "
            "COUNT(DISTINCT a.asset_id) AS asset_count "
            "FROM annotations a WHERE a.superseded_by IS NULL "
            "GROUP BY a.namespace HAVING total >= ? ORDER BY a.namespace",
            (min_count,),
        )
        out = rows_to_dicts(rows)
        registered = {
            str(r["namespace"]): row_to_dict(r) for r in self._query("SELECT * FROM namespaces")
        }
        for item in out:
            meta = registered.get(str(item["namespace"])) or {}
            item["display_name"] = meta.get("display_name") or item["namespace"]
            item["value_type"] = meta.get("value_type") or "categorical"
            item["facetable"] = bool(meta.get("facetable", 1))
            item["registered_by"] = meta.get("registered_by")
        return out

    def facet(
        self,
        namespace: str,
        *,
        asset_ids: Sequence[int] | None = None,
        min_confidence: float | None = None,
        sources: Sequence[str] | None = None,
        limit: int = 50,
        min_count: int = 1,
    ) -> list[dict[str, Any]]:
        """Distinct labels in a namespace with asset counts.

        Passing ``asset_ids`` scopes the facet to a result set, which is what
        makes drill-down filtering work: counts reflect the current query, not
        the whole library.
        """
        clauses = ["superseded_by IS NULL", "(namespace = ? OR namespace LIKE ?)"]
        params: list[Any] = [namespace, f"{namespace}.%"]
        if min_confidence is not None:
            clauses.append("(confidence IS NULL OR confidence >= ?)")
            params.append(min_confidence)
        if sources:
            clauses.append(f"source IN ({placeholders(len(sources))})")
            params.extend(sources)
        if asset_ids is not None:
            if not asset_ids:
                return []
            # Cap the IN list; beyond a few thousand ids a temp table would be
            # faster, but that is a pathological facet request anyway.
            capped = list(asset_ids[:5000])
            clauses.append(f"asset_id IN ({placeholders(len(capped))})")
            params.extend(capped)
        params.extend([min_count, limit])
        rows = self._query(
            "SELECT namespace, label, COUNT(DISTINCT asset_id) AS count, "
            "AVG(confidence) AS avg_confidence "
            f"FROM annotations WHERE {' AND '.join(clauses)} "
            "GROUP BY namespace, label HAVING count >= ? "
            "ORDER BY count DESC, label LIMIT ?",
            params,
        )
        return rows_to_dicts(rows)

    def all_facets(
        self,
        *,
        asset_ids: Sequence[int] | None = None,
        limit_per_namespace: int = 20,
        min_confidence: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Facets for every facetable namespace at once."""
        out: dict[str, list[dict[str, Any]]] = {}
        for ns in self.namespaces_in_use():
            if not ns["facetable"]:
                continue
            name = str(ns["namespace"])
            values = self.facet(
                name,
                asset_ids=asset_ids,
                limit=limit_per_namespace,
                min_confidence=min_confidence,
            )
            if values:
                out[name] = values
        return out

    # ── namespace self-registration ─────────────────────────────────────────

    def register_namespace(
        self,
        namespace: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        value_type: str = "categorical",
        facetable: bool = True,
        searchable: bool = True,
        registered_by: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Optional UI hints for a namespace. Search works without this."""
        values = {
            "namespace": namespace,
            "display_name": display_name or namespace,
            "description": description,
            "value_type": value_type,
            "facetable": int(facetable),
            "searchable": int(searchable),
            "registered_by": registered_by,
            "registered_at": utcnow_iso(),
        }
        self._write(
            lambda c: self._upsert(c, "namespaces", values, ["namespace"]),
            conn,
            label="register_namespace",
        )

    def list_namespaces(self) -> list[dict[str, Any]]:
        return rows_to_dicts(self._query("SELECT * FROM namespaces ORDER BY namespace"))

    # ── purge ───────────────────────────────────────────────────────────────

    def purge_producer(
        self, producer_id: int, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        """Delete everything one producer ever wrote, in one transaction.

        Required by the spec: every plugin's output must be individually
        purgeable and re-derivable. Regions created by the producer go too,
        which cascades to any annotation attached to them.
        """

        def _op(c: sqlite3.Connection) -> dict[str, int]:
            counts: dict[str, int] = {}
            # Unpick supersession first: rows retired *by* this producer's
            # output must become live again, or purging would silently hide
            # the previous version's perfectly good annotations.
            c.execute(
                "UPDATE annotations SET superseded_by = NULL WHERE superseded_by IN "
                "(SELECT id FROM annotations WHERE producer_id = ?)",
                (producer_id,),
            )
            for table in ("annotations", "embeddings", "regions"):
                cur = c.execute(
                    f"DELETE FROM {table} WHERE producer_id = ?", (producer_id,)  # noqa: S608
                )
                counts[table] = cur.rowcount
                cur.close()
            cur = c.execute(
                "DELETE FROM asset_tags WHERE producer_id = ?", (producer_id,)
            )
            counts["asset_tags"] = cur.rowcount
            cur.close()
            cur = c.execute(
                "DELETE FROM asset_locations WHERE producer_id = ? AND source <> 'user'",
                (producer_id,),
            )
            counts["asset_locations"] = cur.rowcount
            cur.close()
            return counts

        return self._write(_op, conn, label="purge_producer")

    def purge_plugin(
        self, plugin_id: str, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        """Purge every producer version of one plugin."""
        totals: dict[str, int] = {}
        for pid in self.producer_ids_for_plugin(plugin_id):
            for table, n in self.purge_producer(pid, conn=conn).items():
                totals[table] = totals.get(table, 0) + n
        return totals

    def purge_asset_derived(
        self, asset_id: int, *, keep_user: bool = True, conn: sqlite3.Connection | None = None
    ) -> dict[str, int]:
        """Drop all machine-derived data for one asset, keeping user data."""

        def _op(c: sqlite3.Connection) -> dict[str, int]:
            counts: dict[str, int] = {}
            where = "asset_id = ?" + (" AND source <> 'user'" if keep_user else "")
            cur = c.execute(f"DELETE FROM annotations WHERE {where}", (asset_id,))  # noqa: S608
            counts["annotations"] = cur.rowcount
            cur.close()
            cur = c.execute("DELETE FROM embeddings WHERE asset_id = ?", (asset_id,))
            counts["embeddings"] = cur.rowcount
            cur.close()
            cur = c.execute(
                "DELETE FROM regions WHERE asset_id = ? AND id NOT IN "
                "(SELECT region_id FROM annotations WHERE region_id IS NOT NULL)",
                (asset_id,),
            )
            counts["regions"] = cur.rowcount
            cur.close()
            return counts

        return self._write(_op, conn, label="purge_asset_derived")

    def stats(self) -> dict[str, Any]:
        return {
            "annotations_live": int(
                self._scalar("SELECT COUNT(*) FROM annotations WHERE superseded_by IS NULL") or 0
            ),
            "annotations_total": int(self._scalar("SELECT COUNT(*) FROM annotations") or 0),
            "annotations_user": int(
                self._scalar("SELECT COUNT(*) FROM annotations WHERE source='user'") or 0
            ),
            "namespaces": int(
                self._scalar(
                    "SELECT COUNT(DISTINCT namespace) FROM annotations WHERE superseded_by IS NULL"
                )
                or 0
            ),
            "regions": int(self._scalar("SELECT COUNT(*) FROM regions") or 0),
            "producers": int(self._scalar("SELECT COUNT(*) FROM producers") or 0),
        }
