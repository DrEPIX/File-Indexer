"""Identities, clusters, region↔identity links, and embeddings.

Two ideas are kept deliberately apart:

* a **cluster** is a machine grouping of similar vectors. It has no name and no
  meaning. A clustering plugin creates and rewrites these freely.
* an **identity** is a person a human named. Binding a region to an identity is
  a *user* act.

``region_identity.confirmed`` is the boundary. Once it is 1, no plugin may
change that row — :meth:`link_region` refuses. This is principle 4 made
concrete, and it is why re-running face recognition after a model upgrade
cannot un-name your family.

Everything in this module is also what :meth:`purge_biometrics` deletes. Face
templates are separately regulated in several jurisdictions (see docs/DATA.md);
segregating them into their own tables is what makes a one-transaction purge
possible without touching the media index.
"""

from __future__ import annotations

import array
import math
import sqlite3
from collections.abc import Sequence
from typing import Any

import numpy as np

from ...errors import ImmutableUserDataError, NotFoundError
from ...util import utcnow_iso
from .base import Repository, placeholders, row_to_dict, rows_to_dicts

__all__ = ["IdentityRepository", "unpack_vector", "pack_vector"]


def pack_vector(values: Sequence[float]) -> tuple[bytes, int, float]:
    """Pack floats to little-endian float32 bytes. Returns ``(blob, dim, norm)``."""
    packed = array.array("f", [float(v) for v in values])
    if array.array("f").itemsize != 4:  # pragma: no cover - exotic platform
        raise RuntimeError("float32 assumption violated")
    norm = math.sqrt(sum(float(v) * float(v) for v in packed)) or 1.0
    return packed.tobytes(), len(packed), norm


def unpack_vector(blob: bytes) -> np.ndarray:
    """Unpack a stored blob back to a float32 NumPy array."""
    return np.frombuffer(blob, dtype="<f4")


class IdentityRepository(Repository):
    """Reads and writes for the biometric/identity layer."""

    # ── embeddings ──────────────────────────────────────────────────────────

    def add_embedding(
        self,
        *,
        producer_id: int,
        vector: Sequence[float],
        asset_id: int | None = None,
        region_id: int | None = None,
        kind: str = "image",
        conn: sqlite3.Connection | None = None,
    ) -> int:
        if asset_id is None and region_id is None:
            raise ValueError("an embedding must attach to an asset or a region")
        blob, dim, norm = pack_vector(vector)
        return self._write(
            lambda c: self._insert(
                c,
                "embeddings",
                {
                    "asset_id": asset_id, "region_id": region_id, "producer_id": producer_id,
                    "kind": kind, "dim": dim, "norm": norm, "vector": blob,
                    "created_at": utcnow_iso(),
                },
            ),
            conn,
            label="add_embedding",
        )

    def get_embedding(self, embedding_id: int) -> tuple[np.ndarray, dict[str, Any]] | None:
        row = self._one("SELECT * FROM embeddings WHERE id = ?", (embedding_id,))
        if row is None:
            return None
        meta = row_to_dict(row) or {}
        blob = meta.pop("vector")
        return unpack_vector(blob), meta

    def embeddings_for_asset(self, asset_id: int, *, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self._query(
                "SELECT id, region_id, producer_id, kind, dim, norm FROM embeddings "
                "WHERE asset_id = ? AND kind = ?",
                (asset_id, kind),
            )
        else:
            rows = self._query(
                "SELECT id, region_id, producer_id, kind, dim, norm FROM embeddings "
                "WHERE asset_id = ?",
                (asset_id,),
            )
        return rows_to_dicts(rows)

    def iter_vectors(
        self,
        *,
        producer_id: int | None = None,
        kind: str | None = None,
        dim: int | None = None,
        limit: int = 1_000_000,
    ) -> tuple[np.ndarray, list[dict[str, Any]]]:
        """Load a vector matrix and its row metadata.

        Returns ``(matrix, meta)`` where ``matrix`` is ``(n, dim)`` float32.
        Rows whose dimension differs from the majority are dropped rather than
        padded — vectors from different models are not comparable and mixing
        them would produce confident nonsense.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if producer_id is not None:
            clauses.append("producer_id = ?")
            params.append(producer_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if dim is not None:
            clauses.append("dim = ?")
            params.append(dim)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._query(
            f"SELECT id, asset_id, region_id, producer_id, kind, dim, norm, vector "  # noqa: S608
            f"FROM embeddings{where} ORDER BY id LIMIT ?",
            params,
        )
        if not rows:
            return np.zeros((0, dim or 1), dtype="float32"), []

        target_dim = dim
        if target_dim is None:
            counts: dict[int, int] = {}
            for row in rows:
                counts[int(row["dim"])] = counts.get(int(row["dim"]), 0) + 1
            target_dim = max(counts, key=lambda k: counts[k])

        vectors: list[np.ndarray] = []
        meta: list[dict[str, Any]] = []
        for row in rows:
            if int(row["dim"]) != target_dim:
                continue
            vectors.append(unpack_vector(row["vector"]))
            meta.append(
                {
                    "embedding_id": int(row["id"]),
                    "asset_id": None if row["asset_id"] is None else int(row["asset_id"]),
                    "region_id": None if row["region_id"] is None else int(row["region_id"]),
                    "producer_id": int(row["producer_id"]),
                    "kind": str(row["kind"]),
                }
            )
        if not vectors:
            return np.zeros((0, target_dim), dtype="float32"), []
        return np.vstack(vectors).astype("float32", copy=False), meta

    def embedding_producers(self) -> list[dict[str, Any]]:
        """Which producers have vectors, and at what dimension."""
        return rows_to_dicts(
            self._query(
                "SELECT e.producer_id, p.plugin_id, p.version, e.kind, e.dim, COUNT(*) AS n "
                "FROM embeddings e JOIN producers p ON p.id = e.producer_id "
                "GROUP BY e.producer_id, e.kind, e.dim ORDER BY n DESC"
            )
        )

    # ── identities ──────────────────────────────────────────────────────────

    def create_identity(
        self,
        display_name: str,
        *,
        notes: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> int:
            row = c.execute(
                "SELECT id FROM identities WHERE display_name = ?", (display_name,)
            ).fetchone()
            if row is not None:
                return int(row["id"])
            return self._insert(
                c,
                "identities",
                {"display_name": display_name, "created_at": now, "updated_at": now, "notes": notes},
            )

        return self._write(_op, conn, label="create_identity")

    def rename_identity(
        self, identity_id: int, display_name: str, *, conn: sqlite3.Connection | None = None
    ) -> None:
        self._write(
            lambda c: self._update(
                c,
                "identities",
                {"display_name": display_name, "updated_at": utcnow_iso()},
                "id = ?",
                (identity_id,),
            ),
            conn,
            label="rename_identity",
        )

    def list_identities(self) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self._query(
                "SELECT i.*, "
                "(SELECT COUNT(*) FROM region_identity ri WHERE ri.identity_id = i.id) AS region_count, "
                "(SELECT COUNT(DISTINCT r.asset_id) FROM region_identity ri "
                "  JOIN regions r ON r.id = ri.region_id WHERE ri.identity_id = i.id) AS asset_count, "
                "(SELECT COUNT(*) FROM region_identity ri "
                "  WHERE ri.identity_id = i.id AND ri.confirmed = 1) AS confirmed_count "
                "FROM identities i ORDER BY i.display_name"
            )
        )

    def get_identity(self, identity_id: int) -> dict[str, Any] | None:
        return row_to_dict(self._one("SELECT * FROM identities WHERE id = ?", (identity_id,)))

    def delete_identity(self, identity_id: int, *, conn: sqlite3.Connection | None = None) -> None:
        """Delete a person. Their region links go; the regions themselves stay."""
        self._write(
            lambda c: c.execute("DELETE FROM identities WHERE id = ?", (identity_id,)),
            conn,
            label="delete_identity",
        )

    def assets_for_identity(self, identity_id: int, limit: int = 1000) -> list[int]:
        rows = self._query(
            "SELECT DISTINCT r.asset_id FROM region_identity ri "
            "JOIN regions r ON r.id = ri.region_id WHERE ri.identity_id = ? LIMIT ?",
            (identity_id, limit),
        )
        return [int(r["asset_id"]) for r in rows]

    def people_for_asset(self, asset_id: int) -> str:
        """Space-joined identity names, for the FTS ``people`` column."""
        rows = self._query(
            "SELECT DISTINCT i.display_name FROM region_identity ri "
            "JOIN regions r ON r.id = ri.region_id "
            "JOIN identities i ON i.id = ri.identity_id WHERE r.asset_id = ?",
            (asset_id,),
        )
        return " ".join(str(r["display_name"]) for r in rows)

    # ── region ↔ identity ───────────────────────────────────────────────────

    def link_region(
        self,
        region_id: int,
        *,
        identity_id: int | None = None,
        cluster_id: int | None = None,
        confidence: float | None = None,
        source: str = "derived",
        confirmed: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Attach a region to an identity and/or cluster.

        Refuses to modify a row a human already confirmed, unless the caller is
        itself the human (``source='user'``). This is the single hard guarantee
        that model upgrades cannot undo manual naming.
        """
        if confirmed and source != "user":
            raise ValueError("only source='user' may set confirmed=1")

        def _op(c: sqlite3.Connection) -> None:
            existing = c.execute(
                "SELECT confirmed FROM region_identity WHERE region_id = ?", (region_id,)
            ).fetchone()
            if existing is not None and int(existing["confirmed"]) == 1 and source != "user":
                raise ImmutableUserDataError(
                    f"region {region_id} has a user-confirmed identity; plugins may not change it"
                )
            self._upsert(
                c,
                "region_identity",
                {
                    "region_id": region_id,
                    "identity_id": identity_id,
                    "cluster_id": cluster_id,
                    "confidence": confidence,
                    "source": source,
                    "confirmed": int(confirmed),
                    "updated_at": utcnow_iso(),
                },
                ["region_id"],
            )

        self._write(_op, conn, label="link_region")

    def confirm_region_identity(
        self,
        region_id: int,
        identity_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """A human confirms this region is this person. Immutable afterwards."""

        def _op(c: sqlite3.Connection) -> None:
            if c.execute("SELECT 1 FROM regions WHERE id = ?", (region_id,)).fetchone() is None:
                raise NotFoundError(f"region {region_id} does not exist")
            if (
                c.execute("SELECT 1 FROM identities WHERE id = ?", (identity_id,)).fetchone()
                is None
            ):
                raise NotFoundError(f"identity {identity_id} does not exist")
            self._upsert(
                c,
                "region_identity",
                {
                    "region_id": region_id,
                    "identity_id": identity_id,
                    "cluster_id": None,
                    "confidence": 1.0,
                    "source": "user",
                    "confirmed": 1,
                    "updated_at": utcnow_iso(),
                },
                ["region_id"],
            )

        self._write(_op, conn, label="confirm_region_identity")

    def reject_region_identity(
        self, region_id: int, *, conn: sqlite3.Connection | None = None
    ) -> None:
        """A human says this region is *not* whoever was suggested.

        The link is cleared but the row is kept, confirmed, with a NULL
        identity — a permanent "not this person" that clustering must respect.
        """
        self._write(
            lambda c: self._upsert(
                c,
                "region_identity",
                {
                    "region_id": region_id, "identity_id": None, "cluster_id": None,
                    "confidence": None, "source": "user", "confirmed": 1,
                    "updated_at": utcnow_iso(),
                },
                ["region_id"],
            ),
            conn,
            label="reject_region_identity",
        )

    def unconfirmed_regions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Suggestions awaiting human review."""
        return rows_to_dicts(
            self._query(
                "SELECT ri.*, r.asset_id, r.x, r.y, r.w, r.h, r.frame_time, i.display_name "
                "FROM region_identity ri JOIN regions r ON r.id = ri.region_id "
                "LEFT JOIN identities i ON i.id = ri.identity_id "
                "WHERE ri.confirmed = 0 ORDER BY ri.confidence DESC NULLS LAST LIMIT ?",
                (limit,),
            )
        )

    # ── clusters ────────────────────────────────────────────────────────────

    def create_cluster(
        self,
        producer_id: int,
        *,
        centroid: Sequence[float] | None = None,
        label: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        blob, dim, _ = pack_vector(centroid) if centroid else (None, None, 0.0)
        return self._write(
            lambda c: self._insert(
                c,
                "clusters",
                {
                    "producer_id": producer_id, "label": label, "centroid": blob,
                    "dim": dim, "size": 0, "created_at": utcnow_iso(),
                },
            ),
            conn,
            label="create_cluster",
        )

    def assign_cluster_identity(
        self, cluster_id: int, identity_id: int | None, *, conn: sqlite3.Connection | None = None
    ) -> int:
        """Name a whole cluster at once, skipping user-confirmed members.

        This is the "that's all Alice" bulk action. It propagates to every
        *unconfirmed* member; anything a human already ruled on is left alone.
        """

        def _op(c: sqlite3.Connection) -> int:
            self._update(c, "clusters", {"identity_id": identity_id}, "id = ?", (cluster_id,))
            cur = c.execute(
                "UPDATE region_identity SET identity_id = ?, updated_at = ? "
                "WHERE cluster_id = ? AND confirmed = 0",
                (identity_id, utcnow_iso(), cluster_id),
            )
            return cur.rowcount

        return self._write(_op, conn, label="assign_cluster_identity")

    def update_cluster_size(self, cluster_id: int, *, conn: sqlite3.Connection | None = None) -> None:
        self._write(
            lambda c: c.execute(
                "UPDATE clusters SET size = "
                "(SELECT COUNT(*) FROM region_identity WHERE cluster_id = ?) WHERE id = ?",
                (cluster_id, cluster_id),
            ),
            conn,
            label="update_cluster_size",
        )

    def list_clusters(self, *, producer_id: int | None = None) -> list[dict[str, Any]]:
        if producer_id is None:
            rows = self._query(
                "SELECT c.id, c.producer_id, c.label, c.dim, c.size, c.identity_id, c.created_at, "
                "i.display_name FROM clusters c LEFT JOIN identities i ON i.id = c.identity_id "
                "ORDER BY c.size DESC"
            )
        else:
            rows = self._query(
                "SELECT c.id, c.producer_id, c.label, c.dim, c.size, c.identity_id, c.created_at, "
                "i.display_name FROM clusters c LEFT JOIN identities i ON i.id = c.identity_id "
                "WHERE c.producer_id = ? ORDER BY c.size DESC",
                (producer_id,),
            )
        return rows_to_dicts(rows)

    # ── purge ───────────────────────────────────────────────────────────────

    def purge_biometrics(self, *, conn: sqlite3.Connection | None = None) -> dict[str, int]:
        """Delete every face region, embedding, cluster and identity link.

        One transaction, and it does not touch the media index: assets, files,
        technical metadata, tags, locations and non-face annotations all
        survive. Required by the data-handling section so an operator can
        satisfy a deletion obligation without rebuilding their library.

        A region counts as biometric if its ``kind`` mentions a face or person,
        or if it carries an identity link. Non-face regions from an object
        detector are left alone.
        """

        def _op(c: sqlite3.Connection) -> dict[str, int]:
            counts: dict[str, int] = {}
            # Reference packs are biometric templates too.  Count each layer
            # explicitly before removing identities so purge reports are
            # auditable rather than relying on uncounted FK cascades.
            counts["face_match_suggestions"] = c.execute(
                "DELETE FROM face_match_suggestions"
            ).rowcount
            counts["face_reference_embeddings"] = c.execute(
                "DELETE FROM face_reference_embeddings"
            ).rowcount
            counts["face_reference_people"] = c.execute(
                "DELETE FROM face_reference_people"
            ).rowcount
            counts["face_reference_packs"] = c.execute(
                "DELETE FROM face_reference_packs"
            ).rowcount
            # Materialise the target set *first*. Evaluating the subquery
            # lazily would be a correctness bug: deleting region_identity
            # shrinks it, so regions that were biometric only by virtue of
            # carrying an identity link would survive the purge.
            targets = [
                int(r["id"])
                for r in c.execute(
                    "SELECT id FROM regions WHERE kind IN ('face','person','body','head') "
                    "UNION SELECT region_id FROM region_identity WHERE region_id IS NOT NULL"
                ).fetchall()
            ]

            counts["region_identity"] = c.execute("DELETE FROM region_identity").rowcount
            counts["clusters"] = c.execute("DELETE FROM clusters").rowcount

            deleted_emb = deleted_ann = deleted_reg = 0
            for i in range(0, len(targets), 500):
                chunk = targets[i : i + 500]
                marks = placeholders(len(chunk))
                deleted_emb += c.execute(
                    f"DELETE FROM embeddings WHERE region_id IN ({marks})", chunk  # noqa: S608
                ).rowcount
                deleted_ann += c.execute(
                    f"DELETE FROM annotations WHERE region_id IN ({marks})", chunk  # noqa: S608
                ).rowcount
                deleted_reg += c.execute(
                    f"DELETE FROM regions WHERE id IN ({marks})", chunk  # noqa: S608
                ).rowcount
            counts["embeddings"] = deleted_emb
            counts["annotations"] = deleted_ann
            counts["regions"] = deleted_reg
            counts["identities"] = c.execute("DELETE FROM identities").rowcount
            return counts

        return self._write(_op, conn, label="purge_biometrics")

    def purge_embeddings_for_producer(
        self, producer_id: int, *, conn: sqlite3.Connection | None = None
    ) -> int:
        def _op(c: sqlite3.Connection) -> int:
            cur = c.execute("DELETE FROM embeddings WHERE producer_id = ?", (producer_id,))
            return cur.rowcount

        return self._write(_op, conn, label="purge_embeddings_for_producer")

    def stats(self) -> dict[str, int]:
        return {
            "identities": int(self._scalar("SELECT COUNT(*) FROM identities") or 0),
            "clusters": int(self._scalar("SELECT COUNT(*) FROM clusters") or 0),
            "region_links": int(self._scalar("SELECT COUNT(*) FROM region_identity") or 0),
            "confirmed_links": int(
                self._scalar("SELECT COUNT(*) FROM region_identity WHERE confirmed = 1") or 0
            ),
            "embeddings": int(self._scalar("SELECT COUNT(*) FROM embeddings") or 0),
        }

    def region_ids(self, region_ids: Sequence[int]) -> list[dict[str, Any]]:
        if not region_ids:
            return []
        return rows_to_dicts(
            self._query(
                f"SELECT * FROM regions WHERE id IN ({placeholders(len(region_ids))})",
                list(region_ids),
            )
        )
