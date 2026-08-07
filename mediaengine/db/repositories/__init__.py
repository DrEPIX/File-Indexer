"""Repositories: the only code that writes SQL.

Everything above this layer speaks in dicts and dataclasses. Grouping the SQL
here means a schema change has one blast radius, and it keeps the single-writer
discipline enforceable — no other module ever touches a connection.
"""

from __future__ import annotations

from ..connection import Database
from .annotations import AnnotationRepository, CommitResult, PendingAnnotation, PendingRegion
from .assets import AssetRepository, TriageResult
from .base import Repository
from .identities import IdentityRepository, pack_vector, unpack_vector
from .places import PlaceRepository
from .tags import DerivativeRepository, SearchDocRepository, TagRepository
from .tasks import TaskRepository

__all__ = [
    "Repository",
    "AssetRepository",
    "TriageResult",
    "AnnotationRepository",
    "PendingAnnotation",
    "PendingRegion",
    "CommitResult",
    "TaskRepository",
    "PlaceRepository",
    "TagRepository",
    "DerivativeRepository",
    "SearchDocRepository",
    "IdentityRepository",
    "pack_vector",
    "unpack_vector",
    "Repositories",
]


class Repositories:
    """One handle holding every repository, sharing a single Database.

    Passed around instead of the raw Database so that call sites read as
    `repos.assets.upsert_asset(...)` rather than growing ad-hoc SQL.
    """

    __slots__ = ("db", "assets", "annotations", "tasks", "places", "tags",
                 "derivatives", "search_docs", "identities")

    def __init__(self, db: Database) -> None:
        self.db = db
        self.assets = AssetRepository(db)
        self.annotations = AnnotationRepository(db)
        self.tasks = TaskRepository(db)
        self.places = PlaceRepository(db)
        self.tags = TagRepository(db)
        self.derivatives = DerivativeRepository(db)
        self.search_docs = SearchDocRepository(db)
        self.identities = IdentityRepository(db)

    def stats(self) -> dict[str, object]:
        """Aggregate counters for `/api/admin/stats` and `mediaengine stat`."""
        return {
            "database": self.db.stats(),
            "assets_by_type": self.assets.counts_by_type(),
            "annotations": self.annotations.stats(),
            "tasks": self.tasks.counts_by_state(),
            "identities": self.identities.stats(),
            "derivatives": {
                "count_by_kind": self.derivatives.counts_by_kind(),
                "total_bytes": self.derivatives.total_bytes(),
            },
            "located_assets": self.places.located_count(),
            "indexed_documents": self.search_docs.indexed_count(),
            "writer": self.db.writer.stats.snapshot(),
        }
