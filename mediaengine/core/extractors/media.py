"""Video and audio metadata, via ``ffprobe``.

One invocation returns format, streams and chapters as JSON, which is
everything the engine needs; there is no reason to call ffprobe twice.

Two conversions here are worth pointing at:

* **Rotation.** A phone records landscape and stores a 90° rotation matrix.
  ``width``/``height`` from the stream are the *stored* dimensions, so a
  portrait video reports 1920×1080 and every thumbnail comes out sideways
  unless the rotation is applied. It is folded into ``orientation`` using the
  same numbering EXIF uses, so one code path handles stills and video.
* **ISO 6709.** QuickTime writes location as ``+37.7858-122.4064+010.000/``,
  a fixed-format string rather than separate fields. Parsing it is how iPhone
  videos get onto the map at all.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ...errors import SubprocessFailed
from ...types import MediaType
from ...util import coerce_float, coerce_int, parse_datetime, to_iso
from ..procs import have_binary, run_command
from .base import Extracted, GpsFix

__all__ = ["extract_media", "ffprobe_json", "parse_iso6709", "ffprobe_available"]

_LOG = logging.getLogger(__name__)

#: ``+37.7858-122.4064+010.000/`` — signed, fixed-width, no separators.
_ISO6709 = re.compile(
    r"^(?P<lat>[+-]\d{2,6}(?:\.\d+)?)(?P<lon>[+-]\d{3,7}(?:\.\d+)?)"
    r"(?P<alt>[+-]\d+(?:\.\d+)?)?/?$"
)

#: Clockwise rotation in degrees -> the EXIF orientation meaning the same thing.
_ROTATION_TO_ORIENTATION = {0: 1, 90: 6, 180: 3, 270: 8}


def ffprobe_available() -> bool:
    """Whether ffprobe can be found. Video extraction degrades without it."""
    return have_binary("ffprobe")


def parse_iso6709(value: str | None) -> GpsFix | None:
    """Decode a QuickTime ISO 6709 location string."""
    if not value:
        return None
    match = _ISO6709.match(value.strip())
    if match is None:
        return None
    try:
        latitude = float(match.group("lat"))
        longitude = float(match.group("lon"))
    except (TypeError, ValueError):
        return None
    altitude = coerce_float(match.group("alt"))
    fix = GpsFix(latitude=latitude, longitude=longitude, altitude_m=altitude)
    return fix if fix.valid() else None


def ffprobe_json(path: Path | str, *, timeout: float = 60.0) -> dict[str, Any]:
    """Run ffprobe and return its parsed JSON.

    ``-v quiet`` because ffprobe's warnings about a slightly unusual file are
    noise at scan scale; genuine failures still show as a non-zero exit and an
    empty document.
    """
    result = run_command(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ],
        timeout=timeout,
    )
    text = result.text().strip()
    if not text:
        raise SubprocessFailed(
            f"ffprobe produced no output for {path}",
            returncode=result.returncode,
            stderr=result.stderr_text(),
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SubprocessFailed(f"ffprobe returned unparsable JSON for {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SubprocessFailed(f"ffprobe returned a {type(parsed).__name__} for {path}")
    return parsed


def _frame_rate(stream: dict[str, Any]) -> float | None:
    """``"30000/1001"`` -> 29.97. Prefers the average over the nominal rate."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = str(stream.get(key) or "")
        if "/" not in raw:
            continue
        numerator, _, denominator = raw.partition("/")
        try:
            den = float(denominator)
            if den:
                rate = float(numerator) / den
                if rate > 0:
                    return round(rate, 4)
        except (TypeError, ValueError):
            continue
    return None


def _rotation(stream: dict[str, Any]) -> int:
    """Rotation in degrees clockwise, from either of the two places it hides."""
    tags = stream.get("tags") or {}
    raw = coerce_int(tags.get("rotate"))
    if raw is None:
        for side_data in stream.get("side_data_list") or []:
            if isinstance(side_data, dict) and "rotation" in side_data:
                raw = coerce_int(side_data.get("rotation"))
                break
    if raw is None:
        return 0
    # ffmpeg reports the display matrix rotation as negative-clockwise.
    return int(-raw % 360) if raw < 0 else int(raw % 360)


def extract_media(
    path: Path | str, *, timeout: float = 60.0, declared: MediaType | None = None
) -> Extracted:
    """Technical metadata for a video or audio file.

    Also corrects the media type: an ``.mp4`` holding only an audio track
    sniffs as video from its container brand, and only the stream list can tell
    the difference. The corrected value is returned in
    ``payload['media_type']`` for the pipeline to apply.
    """
    result = Extracted(extractor="ffprobe")
    if not ffprobe_available():
        result.warnings.append("ffprobe not installed; container metadata unavailable")
        return result

    document = ffprobe_json(path, timeout=timeout)
    result.raw["ffprobe"] = document

    fmt: dict[str, Any] = document.get("format") or {}
    streams: list[dict[str, Any]] = [s for s in document.get("streams") or [] if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    payload = result.payload
    payload["duration_s"] = coerce_float(fmt.get("duration"))
    payload["bit_rate"] = coerce_int(fmt.get("bit_rate"))
    payload["container"] = str(fmt.get("format_name") or "").split(",")[0] or None

    if video is not None:
        payload["width"] = coerce_int(video.get("width"))
        payload["height"] = coerce_int(video.get("height"))
        payload["video_codec"] = str(video.get("codec_name") or "") or None
        payload["frame_rate"] = _frame_rate(video)
        payload["bit_depth"] = coerce_int(
            video.get("bits_per_raw_sample") or video.get("bits_per_sample")
        )
        if payload["duration_s"] is None:
            payload["duration_s"] = coerce_float(video.get("duration"))
        degrees = _rotation(video)
        payload["orientation"] = _ROTATION_TO_ORIENTATION.get(degrees, 1)
        if degrees in (90, 270):
            # Record the display dimensions too; a UI laying out a grid needs
            # them and should not have to know about rotation matrices.
            result.raw.setdefault("display", {}).update(
                {"width": payload["height"], "height": payload["width"], "rotation": degrees}
            )

    if audio is not None:
        payload["audio_codec"] = str(audio.get("codec_name") or "") or None
        payload["audio_channels"] = coerce_int(audio.get("channels"))
        payload["sample_rate"] = coerce_int(audio.get("sample_rate"))
        if payload.get("bit_depth") is None:
            payload["bit_depth"] = coerce_int(audio.get("bits_per_sample"))
        if payload["duration_s"] is None:
            payload["duration_s"] = coerce_float(audio.get("duration"))

    # A container with no video stream is audio, whatever its brand claimed.
    # An "image" that ffprobe sees as a single-frame video stream stays an
    # image — declared wins there, because the sniffer had the magic bytes.
    if declared in (MediaType.VIDEO, MediaType.AUDIO, None):
        if video is None and audio is not None:
            payload["media_type"] = MediaType.AUDIO.value
        elif video is not None:
            frames = coerce_int(video.get("nb_frames")) or 0
            duration = payload.get("duration_s") or 0.0
            still = frames == 1 and duration <= 0.05
            payload["media_type"] = (
                MediaType.IMAGE.value if still and declared is None else MediaType.VIDEO.value
            )

    tags: dict[str, Any] = {}
    for source in (fmt.get("tags") or {}, (video or {}).get("tags") or {}):
        if isinstance(source, dict):
            for key, value in source.items():
                tags.setdefault(str(key).lower(), value)

    for key in ("creation_time", "com.apple.quicktime.creationdate", "date"):
        parsed, tz = parse_datetime(str(tags.get(key)) if tags.get(key) else None)
        if parsed is not None:
            result.captured_at = to_iso(parsed)
            result.captured_at_tz = tz
            result.captured_at_source = "container"
            break

    for key in ("com.apple.quicktime.location.iso6709", "location", "location-eng"):
        fix = parse_iso6709(str(tags.get(key)) if tags.get(key) else None)
        if fix is not None:
            result.gps = fix
            break

    make = tags.get("com.apple.quicktime.make") or tags.get("make")
    model = tags.get("com.apple.quicktime.model") or tags.get("model")
    if make:
        payload["camera_make"] = str(make).strip() or None
    if model:
        payload["camera_model"] = str(model).strip() or None

    chapters = document.get("chapters") or []
    if chapters:
        result.raw.setdefault("ffprobe", {})["chapter_count"] = len(chapters)

    return result
