"""Licensed local reference-face packs and human-review suggestions."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from ...errors import ImmutableUserDataError, NotFoundError
from ...util import json_dumps, json_loads, stable_hash, utcnow_iso
from .base import Repository, row_to_dict, rows_to_dicts
from .identities import pack_vector, unpack_vector

__all__ = ["FaceReferenceRepository"]


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class FaceReferenceRepository(Repository):
    """Stores reference embeddings, never source photographs.

    Pack imports are immutable by ``(name, version, model_id)``.  Changing a
    pack therefore requires a new version, preserving the provenance of old
    suggestions and preventing a same-named pack from silently changing.
    """

    SCHEMA = "mediaengine.face-reference-pack/1"

    def import_pack(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self._validate_pack(payload)
        digest = stable_hash(normalized, length=32)
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> dict[str, Any]:
            existing = c.execute(
                "SELECT id, content_hash FROM face_reference_packs "
                "WHERE name=? AND version=? AND model_id=?",
                (normalized["name"], normalized["version"], normalized["model_id"]),
            ).fetchone()
            if existing is not None:
                if str(existing["content_hash"]) != digest:
                    raise ValueError(
                        "a different pack already uses this name/version/model_id; "
                        "increment the pack version"
                    )
                return self._pack_summary(c, int(existing["id"]), imported=False)

            metadata = dict(normalized.get("metadata") or {})
            pack_id = self._insert(
                c,
                "face_reference_packs",
                {
                    "name": normalized["name"],
                    "version": normalized["version"],
                    "model_id": normalized["model_id"],
                    "embedding_dim": normalized["embedding_dim"],
                    "content_hash": digest,
                    "source_url": normalized["source_url"],
                    "license_name": normalized["license_name"],
                    "attribution": normalized.get("attribution"),
                    "rights_statement": normalized["rights_statement"],
                    "retention_policy": normalized["retention_policy"],
                    "metadata_json": json_dumps(metadata),
                    "created_at": now,
                },
            )
            for person in normalized["people"]:
                person_id = self._insert(
                    c,
                    "face_reference_people",
                    {
                        "pack_id": pack_id,
                        "external_id": person["external_id"],
                        "display_name": person["display_name"],
                        "source_url": person["source_url"],
                        "metadata_json": json_dumps(person.get("metadata") or {}),
                        "created_at": now,
                    },
                )
                for reference in person["references"]:
                    blob, dim, norm = pack_vector(reference["embedding"])
                    self._insert(
                        c,
                        "face_reference_embeddings",
                        {
                            "person_id": person_id,
                            "dim": dim,
                            "norm": norm,
                            "vector": blob,
                            "source_ref": reference["source_ref"],
                            "source_sha256": reference["source_sha256"],
                            "created_at": now,
                        },
                    )
            return self._pack_summary(c, pack_id, imported=True)

        return self._write(_op, None, label="import_face_reference_pack")

    @classmethod
    def _validate_pack(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        if payload.get("schema") != cls.SCHEMA:
            raise ValueError(f"schema must be {cls.SCHEMA!r}")
        dim = int(payload.get("embedding_dim") or 0)
        if dim < 1:
            raise ValueError("embedding_dim must be positive")
        people_value = payload.get("people")
        if not isinstance(people_value, Sequence) or isinstance(people_value, (str, bytes)):
            raise ValueError("people must be an array")
        if not people_value:
            raise ValueError("a reference pack must contain at least one person")

        out: dict[str, Any] = {
            "schema": cls.SCHEMA,
            "name": _required_text(payload.get("name"), "name"),
            "version": _required_text(payload.get("version"), "version"),
            "model_id": _required_text(payload.get("model_id"), "model_id"),
            "embedding_dim": dim,
            "source_url": _required_text(payload.get("source_url"), "source_url"),
            "license_name": _required_text(payload.get("license_name"), "license_name"),
            "rights_statement": _required_text(
                payload.get("rights_statement"), "rights_statement"
            ),
            "retention_policy": _required_text(
                payload.get("retention_policy"), "retention_policy"
            ),
            "attribution": str(payload.get("attribution") or "").strip() or None,
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {},
            "people": [],
        }
        seen_people: set[str] = set()
        for index, raw_person in enumerate(people_value):
            if not isinstance(raw_person, Mapping):
                raise ValueError(f"people[{index}] must be an object")
            external_id = _required_text(raw_person.get("external_id"), f"people[{index}].external_id")
            if external_id in seen_people:
                raise ValueError(f"duplicate external_id {external_id!r}")
            seen_people.add(external_id)
            refs_value = raw_person.get("references")
            if not isinstance(refs_value, Sequence) or isinstance(refs_value, (str, bytes)) or not refs_value:
                raise ValueError(f"people[{index}].references must be a non-empty array")
            references: list[dict[str, Any]] = []
            for ref_index, raw_ref in enumerate(refs_value):
                if not isinstance(raw_ref, Mapping):
                    raise ValueError(f"people[{index}].references[{ref_index}] must be an object")
                vector = raw_ref.get("embedding")
                if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
                    raise ValueError("reference embedding must be an array")
                try:
                    embedding = [float(value) for value in vector]
                except (TypeError, ValueError) as exc:
                    raise ValueError("reference embedding values must be numeric") from exc
                if len(embedding) != dim or not np.isfinite(embedding).all():
                    raise ValueError(f"every reference embedding must contain {dim} finite values")
                source_sha256 = _required_text(
                    raw_ref.get("source_sha256"), "source_sha256"
                ).lower()
                if not _SHA256_RE.fullmatch(source_sha256):
                    raise ValueError("source_sha256 must be exactly 64 hexadecimal characters")
                references.append(
                    {
                        "embedding": embedding,
                        "source_ref": _required_text(raw_ref.get("source_ref"), "source_ref"),
                        "source_sha256": source_sha256,
                    }
                )
            out["people"].append(
                {
                    "external_id": external_id,
                    "display_name": _required_text(
                        raw_person.get("display_name"), f"people[{index}].display_name"
                    ),
                    "source_url": _required_text(
                        raw_person.get("source_url"), f"people[{index}].source_url"
                    ),
                    "metadata": raw_person.get("metadata")
                    if isinstance(raw_person.get("metadata"), Mapping)
                    else {},
                    "references": references,
                }
            )
        return out

    @staticmethod
    def _pack_summary(c: sqlite3.Connection, pack_id: int, *, imported: bool) -> dict[str, Any]:
        row = c.execute(
            "SELECT p.*, COUNT(DISTINCT fp.id) AS people_count, "
            "COUNT(fe.id) AS embedding_count FROM face_reference_packs p "
            "LEFT JOIN face_reference_people fp ON fp.pack_id=p.id "
            "LEFT JOIN face_reference_embeddings fe ON fe.person_id=fp.id "
            "WHERE p.id=? GROUP BY p.id",
            (pack_id,),
        ).fetchone()
        result = {key: row[key] for key in row.keys()}
        result["metadata"] = json_loads(result.pop("metadata_json"), {})
        result["imported"] = imported
        return result

    def list_packs(self) -> list[dict[str, Any]]:
        rows = rows_to_dicts(
            self._query(
                "SELECT p.*, COUNT(DISTINCT fp.id) AS people_count, "
                "COUNT(fe.id) AS embedding_count FROM face_reference_packs p "
                "LEFT JOIN face_reference_people fp ON fp.pack_id=p.id "
                "LEFT JOIN face_reference_embeddings fe ON fe.person_id=fp.id "
                "GROUP BY p.id ORDER BY p.name, p.version"
            )
        )
        for row in rows:
            row["metadata"] = json_loads(row.pop("metadata_json"), {})
        return rows

    def delete_pack(self, pack_id: int) -> bool:
        return bool(
            self._write(
                lambda c: c.execute("DELETE FROM face_reference_packs WHERE id=?", (pack_id,)).rowcount,
                None,
                label="delete_face_reference_pack",
            )
        )

    def reference_vectors(self, pack_id: int) -> tuple[np.ndarray, list[dict[str, Any]], dict[str, Any]]:
        pack = row_to_dict(self._one("SELECT * FROM face_reference_packs WHERE id=?", (pack_id,)))
        if pack is None:
            raise NotFoundError(f"face reference pack {pack_id} does not exist")
        rows = self._query(
            "SELECT fe.vector, fe.norm, fe.id AS embedding_id, fp.id AS person_id, "
            "fp.display_name FROM face_reference_embeddings fe "
            "JOIN face_reference_people fp ON fp.id=fe.person_id "
            "WHERE fp.pack_id=? AND fe.dim=? ORDER BY fp.id, fe.id",
            (pack_id, int(pack["embedding_dim"])),
        )
        vectors = [unpack_vector(row["vector"]) for row in rows]
        matrix = (
            np.vstack(vectors).astype("float32", copy=False)
            if vectors
            else np.zeros((0, int(pack["embedding_dim"])), dtype="float32")
        )
        meta = [
            {
                "embedding_id": int(row["embedding_id"]),
                "person_id": int(row["person_id"]),
                "display_name": str(row["display_name"]),
            }
            for row in rows
        ]
        return matrix, meta, pack

    def candidate_vectors(self, *, model_id: str, dim: int, limit: int) -> tuple[np.ndarray, list[dict[str, Any]]]:
        rows = self._query(
            "SELECT e.vector, e.id AS embedding_id, e.region_id, r.asset_id "
            "FROM embeddings e JOIN producers p ON p.id=e.producer_id "
            "JOIN regions r ON r.id=e.region_id "
            "LEFT JOIN region_identity ri ON ri.region_id=e.region_id AND ri.confirmed=1 "
            "WHERE e.kind='vision.face' AND e.dim=? AND instr(p.model_id, ?) > 0 "
            "AND ri.region_id IS NULL ORDER BY e.id LIMIT ?",
            (dim, model_id, max(1, min(int(limit), 1_000_000))),
        )
        vectors = [unpack_vector(row["vector"]) for row in rows]
        matrix = np.vstack(vectors).astype("float32", copy=False) if vectors else np.zeros((0, dim), dtype="float32")
        meta = [
            {
                "embedding_id": int(row["embedding_id"]),
                "region_id": int(row["region_id"]),
                "asset_id": int(row["asset_id"]),
            }
            for row in rows
        ]
        return matrix, meta

    def save_suggestion(
        self,
        *,
        region_id: int,
        pack_id: int,
        person_id: int,
        confidence: float,
        second_confidence: float | None,
        margin: float,
        model_id: str,
    ) -> bool:
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                "INSERT INTO face_match_suggestions("
                "region_id,pack_id,person_id,confidence,second_confidence,margin,model_id,status,created_at"
                ") VALUES (?,?,?,?,?,?,?,'pending',?) "
                "ON CONFLICT(region_id,pack_id) DO UPDATE SET "
                "person_id=excluded.person_id, confidence=excluded.confidence, "
                "second_confidence=excluded.second_confidence, margin=excluded.margin, "
                "model_id=excluded.model_id, created_at=excluded.created_at "
                "WHERE face_match_suggestions.status='pending'",
                (
                    region_id, pack_id, person_id, confidence, second_confidence,
                    margin, model_id, now,
                ),
            )
            return cur.rowcount > 0

        return self._write(_op, None, label="save_face_match_suggestion")

    def list_suggestions(self, *, status: str = "pending", limit: int = 200) -> list[dict[str, Any]]:
        if status not in {"pending", "accepted", "rejected"}:
            raise ValueError("status must be pending, accepted, or rejected")
        return rows_to_dicts(
            self._query(
                "SELECT s.*, fp.display_name, fp.external_id, p.name AS pack_name, "
                "r.asset_id, r.frame_time, r.x, r.y, r.w, r.h "
                "FROM face_match_suggestions s "
                "JOIN face_reference_people fp ON fp.id=s.person_id "
                "JOIN face_reference_packs p ON p.id=s.pack_id "
                "JOIN regions r ON r.id=s.region_id WHERE s.status=? "
                "ORDER BY s.confidence DESC LIMIT ?",
                (status, max(1, min(int(limit), 10_000))),
            )
        )

    def review(self, suggestion_id: int, *, accept: bool) -> dict[str, Any]:
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute(
                "SELECT s.*, fp.display_name, fp.identity_id, p.name AS pack_name "
                "FROM face_match_suggestions s JOIN face_reference_people fp ON fp.id=s.person_id "
                "JOIN face_reference_packs p ON p.id=s.pack_id WHERE s.id=?",
                (suggestion_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"face match suggestion {suggestion_id} does not exist")
            if str(row["status"]) != "pending":
                raise ValueError(f"suggestion {suggestion_id} has already been reviewed")
            region_id = int(row["region_id"])
            if accept:
                current = c.execute(
                    "SELECT identity_id FROM region_identity WHERE region_id=? AND confirmed=1",
                    (region_id,),
                ).fetchone()
                if current is not None:
                    raise ImmutableUserDataError(f"region {region_id} already has a confirmed decision")
                identity_id = row["identity_id"]
                if identity_id is None:
                    existing = c.execute(
                        "SELECT id FROM identities WHERE display_name=?", (row["display_name"],)
                    ).fetchone()
                    identity_id = int(existing["id"]) if existing else self._insert(
                        c,
                        "identities",
                        {
                            "display_name": row["display_name"],
                            "created_at": now,
                            "updated_at": now,
                            "notes": f"Accepted from local reference pack: {row['pack_name']}",
                        },
                    )
                    c.execute(
                        "UPDATE face_reference_people SET identity_id=? WHERE id=?",
                        (identity_id, int(row["person_id"])),
                    )
                self._upsert(
                    c,
                    "region_identity",
                    {
                        "region_id": region_id,
                        "identity_id": int(identity_id),
                        "cluster_id": None,
                        "confidence": 1.0,
                        "source": "user",
                        "confirmed": 1,
                        "updated_at": now,
                    },
                    ["region_id"],
                )
            c.execute(
                "UPDATE face_match_suggestions SET status=?, reviewed_at=? WHERE id=?",
                ("accepted" if accept else "rejected", now, suggestion_id),
            )
            return {
                "suggestion_id": suggestion_id,
                "region_id": region_id,
                "status": "accepted" if accept else "rejected",
                "display_name": str(row["display_name"]),
            }

        return self._write(_op, None, label="review_face_match_suggestion")

    def stats(self) -> dict[str, int]:
        return {
            "packs": int(self._scalar("SELECT COUNT(*) FROM face_reference_packs") or 0),
            "people": int(self._scalar("SELECT COUNT(*) FROM face_reference_people") or 0),
            "reference_embeddings": int(
                self._scalar("SELECT COUNT(*) FROM face_reference_embeddings") or 0
            ),
            "pending_suggestions": int(
                self._scalar("SELECT COUNT(*) FROM face_match_suggestions WHERE status='pending'") or 0
            ),
        }
