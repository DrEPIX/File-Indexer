"""Shared value types used across every layer of the engine.

This module must not import from any other ``mediaengine`` module. It sits at
the bottom of the dependency graph so that the DB layer, the plugin contract
and the API schemas can all agree on vocabulary without importing each other.
"""

from __future__ import annotations

import enum
from typing import Final, Literal

__all__ = [
    "MediaType",
    "AnnotationSource",
    "TaskState",
    "ScanState",
    "FileStatus",
    "RelationKind",
    "LocationSource",
    "MEDIA_TYPE_VALUES",
    "USER_SOURCE",
]


class MediaType(str, enum.Enum):
    """Top-level classification of an asset.

    Determined from magic bytes (never from the file extension alone) during
    the *identify* stage. Plugins declare which of these they ``accept``.
    """

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    OTHER = "other"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class AnnotationSource(str, enum.Enum):
    """Where an annotation's claim originated.

    Ordering matters: :data:`USER` outranks everything else. The write path in
    :mod:`mediaengine.db.repositories.annotations` refuses to supersede a
    ``user`` annotation with a ``derived`` one.
    """

    EMBEDDED = "embedded"
    """Read out of the file itself (EXIF/IPTC/XMP/container metadata)."""

    DERIVED = "derived"
    """Produced by an analyzer plugin from pixels/audio/text."""

    USER = "user"
    """Entered or confirmed by a human. Immutable to plugins."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class TaskState(str, enum.Enum):
    """Lifecycle of a row in ``analysis_tasks``."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ScanState(str, enum.Enum):
    """Lifecycle of a row in ``scan_sessions``."""

    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class FileStatus(str, enum.Enum):
    """Whether a known path is currently readable on disk."""

    PRESENT = "present"
    MISSING = "missing"
    ERROR = "error"
    EXCLUDED = "excluded"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class RelationKind(str, enum.Enum):
    """Edges in ``asset_relations``.

    The parent is always the *primary* asset a user would expect to see in a
    grid view; the child is the companion. For a RAW+JPEG pair the JPEG is the
    parent and the RAW is the child, because that is what renders.
    """

    RAW_OF = "raw_of"
    MOTION_OF = "motion_of"
    SIDECAR_OF = "sidecar_of"
    DERIVATIVE_OF = "derivative_of"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class LocationSource(str, enum.Enum):
    """Provenance of a row in ``asset_locations``."""

    EXIF = "exif"
    USER = "user"
    DERIVED = "derived"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


MEDIA_TYPE_VALUES: Final[frozenset[str]] = frozenset(m.value for m in MediaType)

USER_SOURCE: Final[str] = AnnotationSource.USER.value

SortDirection = Literal["asc", "desc"]
