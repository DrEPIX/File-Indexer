"""Cosine matching against explicitly imported local face-reference packs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np

from ..db.repositories import Repositories

__all__ = ["FaceReferenceMatcher", "FaceReferenceMatchResult"]


@dataclass(frozen=True, slots=True)
class FaceReferenceMatchResult:
    pack_id: int
    candidates: int
    suggestions: int
    below_threshold: int
    ambiguous: int
    reviewed_skipped: int


def _normalized(matrix: np.ndarray) -> np.ndarray:
    if not len(matrix):
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return cast(np.ndarray, matrix / np.maximum(norms, np.finfo("float32").eps))


class FaceReferenceMatcher:
    """Produces reviewable suggestions; it never writes an identity link."""

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos

    def match(
        self,
        pack_id: int,
        *,
        threshold: float = 0.72,
        min_margin: float = 0.05,
        limit: int = 100_000,
    ) -> FaceReferenceMatchResult:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if not 0.0 <= min_margin <= 1.0:
            raise ValueError("min_margin must be between 0 and 1")
        references, reference_meta, pack = self.repos.reference_faces.reference_vectors(pack_id)
        candidates, candidate_meta = self.repos.reference_faces.candidate_vectors(
            model_id=str(pack["model_id"]),
            dim=int(pack["embedding_dim"]),
            limit=limit,
        )
        if not len(references) or not len(candidates):
            return FaceReferenceMatchResult(pack_id, len(candidates), 0, len(candidates), 0, 0)

        similarities = _normalized(candidates) @ _normalized(references).T
        person_columns: dict[int, list[int]] = {}
        for column, meta in enumerate(reference_meta):
            person_columns.setdefault(int(meta["person_id"]), []).append(column)
        person_ids = sorted(person_columns)
        per_person = np.stack(
            [similarities[:, person_columns[person_id]].max(axis=1) for person_id in person_ids],
            axis=1,
        )
        suggested = below = ambiguous = reviewed_skipped = 0
        for row_index, candidate in enumerate(candidate_meta):
            order = np.argsort(per_person[row_index])[::-1]
            best = float(per_person[row_index, order[0]])
            second = float(per_person[row_index, order[1]]) if len(order) > 1 else None
            margin = best - second if second is not None else best
            if best < threshold:
                below += 1
                continue
            if margin < min_margin:
                ambiguous += 1
                continue
            saved = self.repos.reference_faces.save_suggestion(
                region_id=int(candidate["region_id"]),
                pack_id=pack_id,
                person_id=person_ids[int(order[0])],
                confidence=max(0.0, min(1.0, best)),
                second_confidence=None if second is None else max(0.0, min(1.0, second)),
                margin=max(0.0, margin),
                model_id=str(pack["model_id"]),
            )
            if saved:
                suggested += 1
            else:
                reviewed_skipped += 1
        return FaceReferenceMatchResult(
            pack_id, len(candidates), suggested, below, ambiguous, reviewed_skipped
        )
