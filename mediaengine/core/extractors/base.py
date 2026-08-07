"""What an extractor returns, and the vocabulary they share.

Extractors read technical metadata out of a file. They never touch the
database, never modify the source, and never raise for a merely unusual file —
a camera that writes a nonsense focal length should cost that one field, not
the asset.

The split between :attr:`Extracted.payload` and :attr:`Extracted.raw` matters:
``payload`` keys map exactly onto the promoted columns of
``technical_metadata`` and are therefore filterable in SQL, while ``raw`` is
the untruncated tool output, stored as JSON so that a question nobody
anticipated can still be answered later without a re-scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Extracted", "GpsFix", "PROMOTED_COLUMNS", "merge_extracted"]

#: The subset of extractor output that becomes SQL columns. Mirrors
#: ``technical_metadata`` in ``0001_initial.sql``; anything not in here stays in
#: ``raw_json`` rather than being silently dropped.
PROMOTED_COLUMNS: frozenset[str] = frozenset(
    {
        "width", "height", "duration_s", "frame_rate", "video_codec", "audio_codec",
        "audio_channels", "sample_rate", "bit_rate", "bit_depth", "orientation",
        "page_count", "word_count", "camera_make", "camera_model", "lens_model",
        "iso", "f_number", "exposure_time", "focal_length", "color_space",
        "container", "has_alpha", "is_animated",
    }
)


@dataclass(frozen=True, slots=True)
class GpsFix:
    """A decoded position.

    EXIF stores latitude as three unsigned rationals plus a separate N/S
    reference, and altitude as a positive number plus a below-sea-level flag.
    Getting either sign wrong puts the photo in the wrong hemisphere, so the
    conversion happens once, here, and everything downstream sees decimal
    degrees.
    """

    latitude: float
    longitude: float
    altitude_m: float | None = None
    accuracy_m: float | None = None
    heading_deg: float | None = None
    recorded_at: str | None = None

    def valid(self) -> bool:
        """Reject the 0,0 null island and out-of-range junk.

        A camera with no fix frequently writes zeros rather than omitting the
        tags, and a library with a thousand photos in the Gulf of Guinea is a
        familiar symptom of trusting them.
        """
        if not (-90.0 <= self.latitude <= 90.0 and -180.0 <= self.longitude <= 180.0):
            return False
        return not (abs(self.latitude) < 1e-9 and abs(self.longitude) < 1e-9)


@dataclass(slots=True)
class Extracted:
    """One extractor's findings about one file."""

    payload: dict[str, Any] = field(default_factory=dict)
    """Promoted columns. Keys outside :data:`PROMOTED_COLUMNS` are ignored by
    the repository, so filtering here is a courtesy rather than a requirement."""

    raw: dict[str, Any] = field(default_factory=dict)
    """Full tool output, namespaced by extractor name."""

    extractor: str = ""
    """Which tool produced this, recorded in ``technical_metadata.extractor``."""

    captured_at: str | None = None
    captured_at_tz: str | None = None
    captured_at_source: str | None = None
    """``exif`` | ``container`` | ``filesystem`` — how much to trust the date."""

    gps: GpsFix | None = None

    text: str | None = None
    """Extracted document text, if this file has any."""

    text_truncated: bool = False
    ocr_eligible: bool = False
    """A PDF or scan with no text layer. Flagged, never OCR'd inline — OCR is a
    plugin's job and costs orders of magnitude more than extraction."""

    warnings: list[str] = field(default_factory=list)

    # Extractors deliberately produce *no annotations*. Turning ``EXIF:Make``
    # into a searchable ``camera.make`` label is a taxonomy decision, and the
    # core does not make those — the builtin ``core.exif-entities`` plugin reads
    # :attr:`raw` back out of ``technical_metadata`` and emits the annotations.
    # Keeping that boundary is what lets someone replace the shipped EXIF
    # taxonomy with their own without touching the ingest path.

    def promoted(self) -> dict[str, Any]:
        """The payload filtered to real columns, dropping ``None`` values.

        Dropping ``None`` is what lets a second extractor fill a gap the first
        left without overwriting a value it did find.
        """
        return {
            key: value
            for key, value in self.payload.items()
            if key in PROMOTED_COLUMNS and value is not None
        }


def merge_extracted(base: Extracted, other: Extracted) -> Extracted:
    """Fold a second extractor's result into the first.

    First writer wins per field. The pipeline runs the cheap, reliable
    extractor first (Pillow for dimensions, ffprobe for streams) and the
    richer, slower one second, so a value already established by the source of
    truth is never replaced by a tool guessing at the same field.
    """
    for key, value in other.payload.items():
        if value is not None and base.payload.get(key) is None:
            base.payload[key] = value
    base.raw.update(other.raw)
    if base.captured_at is None and other.captured_at is not None:
        base.captured_at = other.captured_at
        base.captured_at_tz = other.captured_at_tz
        base.captured_at_source = other.captured_at_source
    if base.gps is None and other.gps is not None:
        base.gps = other.gps
    if base.text is None and other.text is not None:
        base.text = other.text
        base.text_truncated = other.text_truncated
    base.ocr_eligible = base.ocr_eligible or other.ocr_eligible
    base.warnings.extend(other.warnings)
    if other.extractor and other.extractor not in base.extractor:
        base.extractor = f"{base.extractor}+{other.extractor}" if base.extractor else other.extractor
    return base
