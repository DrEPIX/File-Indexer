"""Metadata extraction, dispatched by media type.

:class:`MetadataExtractor` is the single entry point. It owns the ExifTool
daemon for the lifetime of a scan — one resident process shared by every
worker thread, rather than one per worker, because the daemon is already
serialised internally and batching beats parallelism for it.

Nothing in here writes to the database or modifies a source file. Extractors
open files read-only and return data; the pipeline decides what to store.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...config import Config
from ...errors import CorruptMedia, ExtractionError
from ...types import MediaType
from ...util import to_iso
from ..identity import Detection
from .base import Extracted, GpsFix, merge_extracted
from .documents import extract_document
from .exiftool import ExifToolDaemon, exiftool_available
from .images import extract_image, from_exiftool
from .media import extract_media, ffprobe_available

__all__ = [
    "MetadataExtractor",
    "Extracted",
    "GpsFix",
    "merge_extracted",
    "extract_image",
    "extract_media",
    "extract_document",
    "ExifToolDaemon",
    "exiftool_available",
    "ffprobe_available",
]

_LOG = logging.getLogger(__name__)

#: Types ExifTool reads usefully. Video metadata comes from ffprobe, which is
#: both faster and more accurate for streams, so ExifTool is not asked.
_EXIFTOOL_TYPES = frozenset({MediaType.IMAGE})


class MetadataExtractor:
    """Owns extraction for one scan, including the ExifTool daemon."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._daemon: ExifToolDaemon | None = None
        self._daemon_wanted = config.workers.exiftool_daemon and exiftool_available()
        if config.workers.exiftool_daemon and not exiftool_available():
            _LOG.info(
                "exiftool not found; falling back to Pillow for EXIF. "
                "Install exiftool for maker notes, XMP/IPTC and raw formats."
            )

    # ── ExifTool ────────────────────────────────────────────────────────────

    @property
    def daemon(self) -> ExifToolDaemon | None:
        """The shared daemon, started lazily on first use."""
        if not self._daemon_wanted:
            return None
        if self._daemon is None:
            self._daemon = ExifToolDaemon(
                timeout_s=self.config.workers.subprocess_timeout_s,
                batch_size=self.config.workers.exiftool_batch_size,
            )
        return self._daemon

    def prefetch_exif(self, paths: Sequence[Path | str]) -> dict[str, dict[str, Any]]:
        """Read EXIF for a whole batch in one daemon exchange.

        This is where the daemon earns its keep: sixty files per exchange
        rather than sixty process spawns. Returns ``{}`` when ExifTool is
        absent, and callers must cope — the Pillow path does.
        """
        daemon = self.daemon
        if daemon is None or not paths:
            return {}
        try:
            return daemon.read_many(paths)
        except Exception as exc:  # noqa: BLE001 - never fail a scan on metadata
            _LOG.warning("exiftool prefetch failed for %d paths: %s", len(paths), exc)
            return {}

    # ── dispatch ────────────────────────────────────────────────────────────

    def extract(
        self,
        path: Path | str,
        detection: Detection,
        *,
        exif_record: dict[str, Any] | None = None,
        file_mtime: float | None = None,
    ) -> Extracted:
        """Extract everything known about one file.

        Never raises for an ordinary bad file: a corrupt or unsupported asset
        comes back with warnings attached and whatever was readable, because
        the asset row should still exist. Only a genuinely unexpected failure
        propagates.
        """
        target = Path(path)
        scan = self.config.scan
        result: Extracted

        try:
            if detection.media_type is MediaType.IMAGE:
                result = extract_image(
                    target,
                    exif_record=exif_record,
                    decode_check=True,
                )
            elif detection.media_type in (MediaType.VIDEO, MediaType.AUDIO):
                result = extract_media(
                    target,
                    timeout=self.config.workers.subprocess_timeout_s,
                    declared=detection.media_type,
                )
                if exif_record:
                    result = merge_extracted(result, from_exiftool(exif_record))
            elif detection.media_type is MediaType.DOCUMENT:
                if scan.extract_document_text:
                    result = extract_document(
                        target,
                        mime_type=detection.mime_type,
                        max_chars=scan.max_document_text_chars,
                    )
                else:
                    result = Extracted(extractor="skipped")
            else:
                result = Extracted(extractor="none")
                if exif_record:
                    result = merge_extracted(result, from_exiftool(exif_record))
        except CorruptMedia as exc:
            result = Extracted(extractor="none")
            result.warnings.append(str(exc))
        except ExtractionError as exc:
            result = Extracted(extractor="none")
            result.warnings.append(f"{type(exc).__name__}: {exc}")

        if detection.container and not result.payload.get("container"):
            result.payload["container"] = detection.container

        # Last resort for capture time. Filesystem mtime is a poor date — a
        # copy resets it — so it is recorded with source='filesystem' and the
        # UI can say so rather than presenting it as if it were EXIF.
        if result.captured_at is None and file_mtime is not None:
            from datetime import datetime, timezone

            result.captured_at = to_iso(datetime.fromtimestamp(file_mtime, tz=timezone.utc))
            result.captured_at_source = "filesystem"

        if not scan.extract_gps:
            result.gps = None

        return result

    def close(self) -> None:
        """Shut down the ExifTool daemon. Safe to call more than once."""
        if self._daemon is not None:
            self._daemon.close()
            self._daemon = None

    def __enter__(self) -> "MetadataExtractor":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def capabilities(self) -> dict[str, bool | int]:
        """What this install can actually do. Reported by ``mediaengine stat``."""
        return {
            "exiftool": exiftool_available(),
            "exiftool_daemon": self._daemon_wanted,
            "exiftool_restarts": self._daemon.restarts if self._daemon else 0,
            "ffprobe": ffprobe_available(),
        }
