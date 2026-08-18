"""Local face grouping for a review-first “name this person” workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, cast

import numpy as np

from ..db.repositories import Repositories

__all__ = ["FaceClusterResult", "FaceClusterer"]


@dataclass(frozen=True, slots=True)
class FaceClusterResult:
    producers: int
    candidate_faces: int
    clusters: int
    clustered_faces: int
    singletons_held_back: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize(matrix: np.ndarray) -> np.ndarray:
    if not len(matrix):
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return cast(np.ndarray, matrix / np.maximum(norms, np.finfo("float32").eps))


class FaceClusterer:
    """Greedy centroid clustering that never crosses model-producer boundaries."""

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos

    @staticmethod
    def _cluster_group(
        matrix: np.ndarray,
        meta: list[dict[str, Any]],
        *,
        threshold: float,
        min_cluster_size: int,
    ) -> tuple[list[tuple[list[float], list[int]]], int]:
        vectors = _normalize(matrix)
        sums: list[np.ndarray] = []
        members: list[list[int]] = []
        for vector, item in zip(vectors, meta, strict=True):
            if not sums:
                sums.append(vector.copy())
                members.append([int(item["region_id"])])
                continue
            centroids = _normalize(np.vstack(sums).astype("float32", copy=False))
            scores = centroids @ vector
            best_index = int(np.argmax(scores))
            if float(scores[best_index]) >= threshold:
                sums[best_index] += vector
                members[best_index].append(int(item["region_id"]))
            else:
                sums.append(vector.copy())
                members.append([int(item["region_id"])])

        accepted: list[tuple[list[float], list[int]]] = []
        held_back = 0
        for vector_sum, region_ids in zip(sums, members, strict=True):
            if len(region_ids) < min_cluster_size:
                held_back += len(region_ids)
                continue
            centroid = _normalize(vector_sum.reshape(1, -1))[0].astype("float32").tolist()
            accepted.append((centroid, region_ids))
        return accepted, held_back

    def run(
        self,
        *,
        threshold: float = 0.72,
        min_cluster_size: int = 2,
        limit_per_model: int = 250_000,
    ) -> FaceClusterResult:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if not 2 <= min_cluster_size <= 100:
            raise ValueError("min_cluster_size must be between 2 and 100")
        groups = self.repos.identities.face_vector_groups(limit_per_group=limit_per_model)
        candidate_faces = clustered_faces = cluster_count = held_back = 0
        for producer_id, _dim, matrix, meta in groups:
            candidate_faces += len(meta)
            clusters, skipped = self._cluster_group(
                matrix,
                meta,
                threshold=threshold,
                min_cluster_size=min_cluster_size,
            )
            self.repos.identities.replace_unnamed_clusters(producer_id, clusters)
            cluster_count += len(clusters)
            clustered_faces += sum(len(region_ids) for _, region_ids in clusters)
            held_back += skipped
        return FaceClusterResult(
            producers=len(groups),
            candidate_faces=candidate_faces,
            clusters=cluster_count,
            clustered_faces=clustered_faces,
            singletons_held_back=held_back,
        )
