"""Companion-file detection: sidecars, RAW+JPEG pairs, motion photos.

A camera does not produce one file per photograph. It produces a JPEG and a
RAW, or a HEIC and a MOV, or a JPEG with a video welded onto the end of it. An
indexer that treats those as unrelated assets shows the user the same moment
three times and lets them delete the wrong copy.

Pairing is done by **stem within a directory**, not by content: ``IMG_4821.CR2``
and ``IMG_4821.JPG`` sitting side by side are one photograph, and nothing in
the bytes says so. That makes it a heuristic, so it is deliberately narrow —
same directory, same stem, complementary types — because a false pair is worse
than a missed one.

Direction is fixed by convention, set in :class:`RelationKind`: **the JPEG is
the parent and the RAW is the child**, because the JPEG is what renders in a
grid. Same for a motion photo: the still is the parent.

Memory is bounded. The walker's traversal is depth-first, so once it leaves a
directory it never returns; the linker resolves and discards each directory's
candidates at that point rather than holding the whole library.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db.repositories import AssetRepository
from ..types import MediaType, RelationKind
from .identity import MOTION_VIDEO_EXTENSIONS, RAW_EXTENSIONS, SIDECAR_EXTENSIONS

__all__ = ["SidecarCandidate", "SidecarLinker", "find_embedded_motion", "LinkStats"]

_LOG = logging.getLogger(__name__)

#: Extensions that count as the "rendered" half of a RAW+JPEG pair.
_RENDERED_EXTENSIONS = frozenset({"jpg", "jpeg", "heic", "heif", "png", "tif", "tiff", "webp"})

#: ``GCamera:MicroVideoOffset="4194304"`` — Google's motion-photo marker,
#: counted in bytes back from the end of the file.
_MICRO_VIDEO_OFFSET = re.compile(rb"MicroVideoOffset\s*=\s*[\"'](\d+)[\"']")
_MOTION_PHOTO_FLAG = re.compile(rb"(?:GCamera:)?MotionPhoto\s*=\s*[\"']1[\"']")
#: Samsung writes the trailer length instead.
_MOTION_PHOTO_LENGTH = re.compile(rb"MotionPhotoVideoLength\s*=\s*[\"'](\d+)[\"']")

#: How far back from EOF to look for an ISO-BMFF box when the XMP is silent.
_TAIL_SCAN_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class SidecarCandidate:
    """One ingested file, remembered until its directory is resolved."""

    path: Path
    asset_id: int
    media_type: MediaType
    is_raw: bool = False
    extension: str = ""

    @property
    def stem_key(self) -> str:
        """Lowercased stem, with a second suffix stripped for ``x.jpg.xmp``.

        Darktable writes ``IMG_1.jpg.xmp`` while Lightroom writes ``IMG_1.xmp``;
        both must resolve to the same key or half of a library pairs and half
        does not.
        """
        stem = self.path.stem
        if self.extension in SIDECAR_EXTENSIONS and "." in stem:
            inner = Path(stem)
            if inner.suffix.lower().lstrip(".") not in ("", stem.lower()):
                stem = inner.stem
        return stem.lower()


@dataclass(slots=True)
class LinkStats:
    """What the linker found. Reported at the end of a scan."""

    raw_pairs: int = 0
    motion_pairs: int = 0
    sidecars: int = 0
    embedded_motion: int = 0
    directories_resolved: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "raw_pairs": self.raw_pairs,
            "motion_pairs": self.motion_pairs,
            "sidecars": self.sidecars,
            "embedded_motion": self.embedded_motion,
            "directories_resolved": self.directories_resolved,
        }


def find_embedded_motion(
    path: Path | str, *, xmp: bytes | None = None, file_size: int | None = None
) -> int | None:
    """Byte offset of a video embedded in a still image, if there is one.

    Android motion photos are a JPEG with an MP4 appended. Two ways to find the
    join, tried in order of reliability:

    1. **The XMP says so.** ``MicroVideoOffset`` (and Samsung's
       ``MotionPhotoVideoLength``) give the trailer length in bytes from EOF.
       This is exact and costs nothing beyond a metadata read.
    2. **Scan the tail for an ISO-BMFF box.** A four-byte big-endian length
       followed by ``ftyp`` at a plausible position. Only used when the XMP is
       absent or lying, and bounded to the last 16 MB so a large file does not
       turn into a large read.

    Returns ``None`` when there is no embedded video, which is the common case.
    """
    target = Path(path)
    try:
        size = file_size if file_size is not None else target.stat().st_size
    except OSError:
        return None
    if size < 1024:
        return None

    if xmp:
        match = _MICRO_VIDEO_OFFSET.search(xmp) or _MOTION_PHOTO_LENGTH.search(xmp)
        if match is not None:
            try:
                trailer = int(match.group(1))
            except ValueError:
                trailer = 0
            if 0 < trailer < size:
                return size - trailer
        if _MOTION_PHOTO_FLAG.search(xmp) is None and _MICRO_VIDEO_OFFSET.search(xmp) is None:
            # XMP present and says nothing about motion: believe it.
            return None

    try:
        with target.open("rb") as handle:
            start = max(0, size - _TAIL_SCAN_BYTES)
            handle.seek(start)
            tail = handle.read()
    except OSError:
        return None

    position = 0
    while True:
        found = tail.find(b"ftyp", position)
        if found < 4:
            if found < 0:
                return None
            position = found + 4
            continue
        box_start = found - 4
        box_length = int.from_bytes(tail[box_start : box_start + 4], "big")
        # A real ftyp box declares a small, sane length and sits after the
        # JPEG's own data rather than inside its entropy-coded stream.
        if 8 <= box_length <= 1024 and (start + box_start) > 1024:
            return start + box_start
        position = found + 4


class SidecarLinker:
    """Accumulates ingested files and links companions per directory."""

    def __init__(self, repository: AssetRepository, *, enabled: bool = True,
                 detect_motion: bool = True) -> None:
        self.repository = repository
        self.enabled = enabled
        self.detect_motion = detect_motion
        self.stats = LinkStats()
        self._pending: dict[str, list[SidecarCandidate]] = {}
        self._current: str | None = None

    def add(self, candidate: SidecarCandidate) -> None:
        """Remember a file. Resolves any directory the walk has now left."""
        if not self.enabled:
            return
        directory = str(candidate.path.parent)
        if directory != self._current:
            # Depth-first traversal never revisits a directory, so everything
            # except the one now being filled is complete and can be released.
            self._resolve_all_except(directory)
            self._current = directory
        self._pending.setdefault(directory, []).append(candidate)

    def add_many(self, candidates: Iterable[SidecarCandidate]) -> None:
        for candidate in candidates:
            self.add(candidate)

    def finish(self) -> LinkStats:
        """Resolve every remaining directory. Call once at end of scan."""
        self._resolve_all_except(None)
        self._current = None
        return self.stats

    def _resolve_all_except(self, keep: str | None) -> None:
        for directory in [d for d in self._pending if d != keep]:
            self._resolve(self._pending.pop(directory))
            self.stats.directories_resolved += 1

    def _resolve(self, candidates: list[SidecarCandidate]) -> None:
        """Pair up one directory's worth of files."""
        if len(candidates) < 2:
            return
        by_stem: dict[str, list[SidecarCandidate]] = {}
        for candidate in candidates:
            by_stem.setdefault(candidate.stem_key, []).append(candidate)

        for group in by_stem.values():
            if len(group) < 2:
                continue
            self._link_group(group)

    def _link_group(self, group: list[SidecarCandidate]) -> None:
        """Link one stem's files. A stem may carry several relations at once —
        a HEIC with both a MOV and an XMP is entirely normal."""
        rendered = [
            c for c in group
            if c.media_type is MediaType.IMAGE and not c.is_raw
            and c.extension in _RENDERED_EXTENSIONS
        ]
        raws = [c for c in group if c.is_raw]
        videos = [
            c for c in group
            if c.media_type is MediaType.VIDEO and c.extension in MOTION_VIDEO_EXTENSIONS
        ]
        sidecars = [c for c in group if c.extension in SIDECAR_EXTENSIONS]

        # The rendered still is the parent of everything else in the group. If
        # there is no JPEG — a RAW-only import — the RAW itself becomes the
        # anchor so its sidecar still has somewhere to attach.
        parent = rendered[0] if rendered else (raws[0] if raws else None)
        if parent is None:
            return

        for raw in raws:
            if raw.asset_id == parent.asset_id:
                continue
            self.repository.add_relation(parent.asset_id, raw.asset_id, RelationKind.RAW_OF)
            self.stats.raw_pairs += 1

        if self.detect_motion:
            for video in videos:
                if video.asset_id == parent.asset_id:
                    continue
                self.repository.add_relation(
                    parent.asset_id, video.asset_id, RelationKind.MOTION_OF
                )
                self.stats.motion_pairs += 1

        for sidecar in sidecars:
            if sidecar.asset_id == parent.asset_id:
                continue
            self.repository.add_relation(
                parent.asset_id, sidecar.asset_id, RelationKind.SIDECAR_OF
            )
            self.stats.sidecars += 1

    # ── embedded motion ─────────────────────────────────────────────────────

    def note_embedded_motion(self, asset_id: int, offset: int) -> dict[str, Any]:
        """Record that a still carries a video trailer.

        Stored in technical metadata rather than as a relation: there is no
        second asset to point at, only a byte range inside this one. A plugin
        that wants the clip reads the offset and slices the file.
        """
        self.stats.embedded_motion += 1
        return {"motion_photo": {"embedded": True, "video_offset": offset}}


def is_sidecar_extension(extension: str) -> bool:
    """Whether an extension denotes a metadata companion."""
    return extension.lower().lstrip(".") in SIDECAR_EXTENSIONS


def is_raw_extension(extension: str) -> bool:
    """Whether an extension denotes a camera raw file."""
    return extension.lower().lstrip(".") in RAW_EXTENSIONS
