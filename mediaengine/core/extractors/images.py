"""Still-image metadata.

Two sources, in priority order:

1. **Pillow** — always available, gives dimensions, mode, animation and a
   decode check that catches a truncated file before a plugin trips over it.
   It also reads EXIF, including the GPS IFD, which makes it a *complete*
   fallback rather than a degraded one. On a machine without ExifTool this is
   the only path that runs, so it is written to be sufficient on its own.
2. **ExifTool** — richer by a wide margin (maker notes, XMP, IPTC, lens
   databases, formats Pillow cannot open at all such as CR3 and ARW). Merged
   on top of Pillow's findings, filling gaps rather than overwriting.

The EXIF rational-to-decimal conversions live here because getting them wrong
is quiet and expensive: a sign error on ``GPSLatitudeRef`` puts a photo in the
wrong hemisphere, and nothing downstream can detect that.
"""

from __future__ import annotations

import logging
from fractions import Fraction
from pathlib import Path
from typing import Any

from ...errors import CorruptMedia
from ...util import coerce_float, coerce_int, parse_datetime, to_iso
from .base import Extracted, GpsFix

__all__ = ["extract_image", "extract_with_pillow", "from_exiftool", "decode_exif_gps"]

_LOG = logging.getLogger(__name__)

# EXIF tag numbers. Named constants because `0x829a` at a call site is
# unreadable and mistyping one silently loses a field.
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_ORIENTATION = 0x0112
_TAG_SOFTWARE = 0x0131
_TAG_DATETIME = 0x0132
_TAG_EXIF_IFD = 0x8769
_TAG_GPS_IFD = 0x8825
_TAG_EXPOSURE_TIME = 0x829A
_TAG_F_NUMBER = 0x829D
_TAG_ISO = 0x8827
_TAG_ISO_SENSITIVITY = 0x8833
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004
_TAG_OFFSET_TIME = 0x9010
_TAG_OFFSET_TIME_ORIGINAL = 0x9011
_TAG_FOCAL_LENGTH = 0x920A
_TAG_COLOR_SPACE = 0xA001
_TAG_PIXEL_X = 0xA002
_TAG_PIXEL_Y = 0xA003
_TAG_LENS_MODEL = 0xA434

_GPS_LAT_REF = 1
_GPS_LAT = 2
_GPS_LON_REF = 3
_GPS_LON = 4
_GPS_ALT_REF = 5
_GPS_ALT = 6
_GPS_TIMESTAMP = 7
_GPS_IMG_DIRECTION = 17
_GPS_DATESTAMP = 29
_GPS_HPOS_ERROR = 31

#: EXIF ColorSpace is an enum, not a name.
_COLOR_SPACE_NAMES = {1: "sRGB", 2: "Adobe RGB", 0xFFFF: "Uncalibrated"}


def _rational(value: Any) -> float | None:
    """Collapse the several shapes Pillow returns for a rational to a float.

    There are four, and missing any one of them is a silent wrong answer.
    Pillow hands back a plain float for some tags, a ``(numerator,
    denominator)`` tuple for others, a :class:`fractions.Fraction` occasionally,
    and — most commonly, for anything read out of a real EXIF IFD — a
    ``PIL.TiffImagePlugin.IFDRational``. That last one is a ``numbers.Rational``
    but *not* a ``Fraction`` and not a ``float``, so an isinstance chain that
    only knows the first three quietly returns ``None`` for every coordinate in
    the GPS block and every photograph lands on null island.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    if numerator is not None and denominator is not None:
        try:
            return float(numerator) / float(denominator) if float(denominator) else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    if isinstance(value, tuple) and len(value) == 2:
        try:
            return float(value[0]) / float(value[1]) if float(value[1]) else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return coerce_float(value)


def _dms_to_degrees(dms: Any, ref: Any) -> float | None:
    """Convert ``(degrees, minutes, seconds)`` plus an N/S/E/W ref to decimal.

    The reference letter is the *only* thing carrying the sign in EXIF; the
    three rationals are always positive. Dropping it silently mirrors every
    southern and western photo onto the wrong side of the planet.
    """
    if dms is None:
        return None
    try:
        parts = list(dms)
    except TypeError:
        return None
    if len(parts) < 2:
        return None
    degrees = _rational(parts[0]) or 0.0
    minutes = _rational(parts[1]) or 0.0
    seconds = _rational(parts[2]) if len(parts) > 2 else 0.0
    value = degrees + minutes / 60.0 + (seconds or 0.0) / 3600.0
    marker = str(ref or "").strip().upper()[:1]
    if marker in ("S", "W"):
        value = -value
    return value


def decode_exif_gps(gps: dict[int, Any]) -> GpsFix | None:
    """Turn a raw GPS IFD into a validated :class:`GpsFix`.

    Returns ``None`` for a camera that wrote the tags but had no fix, which is
    common enough that treating 0,0 as a real location would put a visible
    cluster of photos in the Atlantic.
    """
    if not gps:
        return None
    latitude = _dms_to_degrees(gps.get(_GPS_LAT), gps.get(_GPS_LAT_REF))
    longitude = _dms_to_degrees(gps.get(_GPS_LON), gps.get(_GPS_LON_REF))
    if latitude is None or longitude is None:
        return None

    altitude = _rational(gps.get(_GPS_ALT))
    if altitude is not None:
        # GPSAltitudeRef 1 means "below sea level"; the value itself is
        # unsigned, exactly like latitude.
        try:
            below = int(gps.get(_GPS_ALT_REF) or 0) == 1
        except (TypeError, ValueError):
            below = False
        if below:
            altitude = -altitude

    recorded_at: str | None = None
    date_stamp = gps.get(_GPS_DATESTAMP)
    time_stamp = gps.get(_GPS_TIMESTAMP)
    if date_stamp and time_stamp:
        try:
            hours = int(_rational(list(time_stamp)[0]) or 0)
            minutes = int(_rational(list(time_stamp)[1]) or 0)
            seconds = int(_rational(list(time_stamp)[2]) or 0)
            parsed, _ = parse_datetime(f"{date_stamp} {hours:02d}:{minutes:02d}:{seconds:02d}")
            recorded_at = to_iso(parsed)
        except (TypeError, ValueError, IndexError):
            recorded_at = None

    fix = GpsFix(
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude,
        accuracy_m=_rational(gps.get(_GPS_HPOS_ERROR)),
        heading_deg=_rational(gps.get(_GPS_IMG_DIRECTION)),
        recorded_at=recorded_at,
    )
    return fix if fix.valid() else None


def extract_with_pillow(path: Path | str, *, decode_check: bool = True) -> Extracted:
    """Dimensions, EXIF and a decode check, using only the core dependency.

    :param decode_check: actually decode the pixels. Catches truncation, which
        a header read does not. Costs real time on large images, so the
        pipeline turns it off for formats where the container already tells us
        the file is intact.
    :raises CorruptMedia: the file could not be opened as an image at all.
    """
    from PIL import Image, ImageFile, UnidentifiedImageError

    # Partial pixel data is still worth having: a truncated JPEG should yield a
    # thumbnail and a perceptual hash for the part that arrived, not nothing.
    ImageFile.LOAD_TRUNCATED_IMAGES = True

    result = Extracted(extractor="pillow")
    target = Path(path)
    try:
        with Image.open(target) as image:
            result.payload["width"] = image.width
            result.payload["height"] = image.height
            result.payload["container"] = (image.format or "").lower() or None
            result.payload["has_alpha"] = int(
                image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            )
            frames = int(getattr(image, "n_frames", 1) or 1)
            result.payload["is_animated"] = int(bool(getattr(image, "is_animated", False)))
            if frames > 1:
                result.raw.setdefault("pillow", {})["n_frames"] = frames
            bands = len(image.getbands()) or 1
            depth = {"1": 1, "L": 8, "P": 8, "RGB": 8, "RGBA": 8, "I;16": 16, "I": 32, "F": 32}
            result.payload["bit_depth"] = depth.get(image.mode)
            result.raw.setdefault("pillow", {}).update(
                {"mode": image.mode, "format": image.format, "bands": bands}
            )
            if image.info.get("icc_profile"):
                result.payload["color_space"] = "icc"

            _read_pillow_exif(image, result)

            if decode_check:
                try:
                    image.load()
                except OSError as exc:
                    result.warnings.append(f"truncated or damaged image data: {exc}")
    except UnidentifiedImageError as exc:
        raise CorruptMedia(f"{target}: not a decodable image: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise CorruptMedia(f"{target}: image could not be read: {exc}") from exc

    return result


def _read_pillow_exif(image: Any, result: Extracted) -> None:
    """Pull EXIF, the Exif sub-IFD and the GPS IFD out of an open image."""
    try:
        exif = image.getexif()
    except (AttributeError, OSError, ValueError, SyntaxError):
        return
    if not exif:
        return

    raw_exif: dict[str, Any] = {}
    for tag, value in exif.items():
        raw_exif[f"0x{tag:04x}"] = _jsonable(value)

    payload = result.payload
    payload["camera_make"] = _clean_str(exif.get(_TAG_MAKE))
    payload["camera_model"] = _clean_str(exif.get(_TAG_MODEL))
    payload["orientation"] = coerce_int(exif.get(_TAG_ORIENTATION))

    ifd: dict[int, Any] = {}
    try:
        ifd = dict(exif.get_ifd(_TAG_EXIF_IFD) or {})
    except (AttributeError, KeyError, OSError, ValueError):
        ifd = {}
    for tag, value in ifd.items():
        raw_exif[f"exif:0x{tag:04x}"] = _jsonable(value)

    payload["iso"] = coerce_int(ifd.get(_TAG_ISO) or ifd.get(_TAG_ISO_SENSITIVITY))
    payload["f_number"] = _rational(ifd.get(_TAG_F_NUMBER))
    payload["exposure_time"] = _rational(ifd.get(_TAG_EXPOSURE_TIME))
    payload["focal_length"] = _rational(ifd.get(_TAG_FOCAL_LENGTH))
    payload["lens_model"] = _clean_str(ifd.get(_TAG_LENS_MODEL))
    colour = coerce_int(ifd.get(_TAG_COLOR_SPACE))
    if colour is not None and payload.get("color_space") in (None, "icc"):
        payload["color_space"] = _COLOR_SPACE_NAMES.get(colour, str(colour))
    # PixelXDimension is the real image size for some formats where the TIFF
    # header lies (notably rotated HEIC), so prefer it when present.
    for source, key in ((_TAG_PIXEL_X, "width"), (_TAG_PIXEL_Y, "height")):
        pixels = coerce_int(ifd.get(source))
        if pixels and not payload.get(key):
            payload[key] = pixels

    # Capture time: the original beats the digitised time beats the file's own
    # DateTime, which many editors rewrite on save.
    offset = _clean_str(ifd.get(_TAG_OFFSET_TIME_ORIGINAL) or ifd.get(_TAG_OFFSET_TIME))
    for candidate in (
        ifd.get(_TAG_DATETIME_ORIGINAL),
        ifd.get(_TAG_DATETIME_DIGITIZED),
        exif.get(_TAG_DATETIME),
    ):
        parsed, tz = parse_datetime(_clean_str(candidate))
        if parsed is not None:
            result.captured_at = to_iso(parsed)
            result.captured_at_tz = tz or offset
            result.captured_at_source = "exif"
            break

    try:
        gps_ifd = dict(exif.get_ifd(_TAG_GPS_IFD) or {})
    except (AttributeError, KeyError, OSError, ValueError):
        gps_ifd = {}
    if gps_ifd:
        raw_exif["gps"] = {str(k): _jsonable(v) for k, v in gps_ifd.items()}
        result.gps = decode_exif_gps(gps_ifd)

    software = _clean_str(exif.get(_TAG_SOFTWARE))
    if software:
        raw_exif["software"] = software
    result.raw["exif"] = raw_exif


def _clean_str(value: Any) -> str | None:
    """Trim, drop NULs, and treat an empty result as absent.

    Camera firmware pads strings with NULs and spaces; storing ``"Canon\\x00"``
    makes an exact-match filter on camera make fail for no visible reason.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).replace("\x00", "").strip()
    return text or None


def _jsonable(value: Any) -> Any:
    """Coerce an EXIF value into something ``json.dumps`` will accept."""
    if isinstance(value, bytes):
        # Maker notes are megabytes of binary. Record the size, not the bytes.
        return f"<{len(value)} bytes>" if len(value) > 256 else value.hex()
    if isinstance(value, Fraction) or (
        hasattr(value, "numerator") and hasattr(value, "denominator")
    ):
        return _rational(value)
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ── ExifTool mapping ─────────────────────────────────────────────────────────

#: ExifTool key (without group prefix) -> promoted column. Keys are tried in
#: order, first hit wins, so the more specific tag precedes the generic one.
_EXIFTOOL_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ImageWidth", "ExifImageWidth"), "width"),
    (("ImageHeight", "ExifImageHeight"), "height"),
    (("Make",), "camera_make"),
    (("Model",), "camera_model"),
    (("LensModel", "LensID", "Lens"), "lens_model"),
    (("ISO", "ISOSpeed", "PhotographicSensitivity"), "iso"),
    (("FNumber", "Aperture"), "f_number"),
    (("ExposureTime", "ShutterSpeed"), "exposure_time"),
    (("FocalLength",), "focal_length"),
    (("Orientation",), "orientation"),
    (("ColorSpace",), "color_space"),
    (("BitsPerSample", "BitDepth"), "bit_depth"),
    (("Duration",), "duration_s"),
    (("VideoFrameRate", "FrameRate"), "frame_rate"),
    (("PageCount", "PageNumber"), "page_count"),
    (("WordCount", "Words"), "word_count"),
)

_NUMERIC_COLUMNS = frozenset(
    {"width", "height", "iso", "orientation", "bit_depth", "page_count", "word_count"}
)
_FLOAT_COLUMNS = frozenset({"f_number", "exposure_time", "focal_length", "duration_s", "frame_rate"})


def from_exiftool(record: dict[str, Any]) -> Extracted:
    """Map one ExifTool JSON record onto :class:`Extracted`.

    ExifTool is invoked with ``-G0``, so keys arrive group-prefixed
    (``EXIF:Make``, ``XMP:Subject``). The prefix is stripped for lookup but
    kept in :attr:`Extracted.raw`, because ``XMP:Rating`` and ``EXIF:Rating``
    genuinely differ and a plugin may care which one it got.
    """
    result = Extracted(extractor="exiftool")
    if not record:
        return result

    flat: dict[str, Any] = {}
    for key, value in record.items():
        bare = key.split(":", 1)[-1]
        flat.setdefault(bare, value)
    result.raw["exiftool"] = {k: _jsonable(v) for k, v in record.items()}

    for candidates, column in _EXIFTOOL_MAP:
        for candidate in candidates:
            if candidate not in flat:
                continue
            value = flat[candidate]
            if column in _NUMERIC_COLUMNS:
                result.payload[column] = coerce_int(value)
            elif column in _FLOAT_COLUMNS:
                result.payload[column] = coerce_float(value)
            else:
                result.payload[column] = _clean_str(value)
            break

    for key in ("DateTimeOriginal", "CreateDate", "MediaCreateDate", "ModifyDate"):
        parsed, tz = parse_datetime(_clean_str(flat.get(key)))
        if parsed is not None:
            result.captured_at = to_iso(parsed)
            result.captured_at_tz = tz or _clean_str(flat.get("OffsetTimeOriginal"))
            result.captured_at_source = "exif"
            break

    # With -n, ExifTool's Composite GPS tags are already signed decimals, so no
    # ref handling is needed on this path — unlike the Pillow one above.
    latitude = coerce_float(flat.get("GPSLatitude"))
    longitude = coerce_float(flat.get("GPSLongitude"))
    if latitude is not None and longitude is not None:
        fix = GpsFix(
            latitude=latitude,
            longitude=longitude,
            altitude_m=coerce_float(flat.get("GPSAltitude")),
            accuracy_m=coerce_float(flat.get("GPSHPositioningError")),
            heading_deg=coerce_float(flat.get("GPSImgDirection")),
            recorded_at=to_iso(parse_datetime(_clean_str(flat.get("GPSDateTime")))[0]),
        )
        if fix.valid():
            result.gps = fix

    return result


def extract_image(
    path: Path | str,
    *,
    exif_record: dict[str, Any] | None = None,
    decode_check: bool = True,
) -> Extracted:
    """Full still-image extraction, ExifTool optional.

    Pillow runs first because it is the source of truth for what the decoder
    will actually produce — the dimensions a renderer sees. ExifTool then fills
    everything Pillow does not know, and can carry the whole result on its own
    for raw formats Pillow cannot open.
    """
    from .base import merge_extracted

    try:
        result = extract_with_pillow(path, decode_check=decode_check)
    except CorruptMedia:
        if not exif_record:
            raise
        # A CR3 or ARW that Pillow cannot open is not corrupt — ExifTool read it
        # fine. Fall back to the metadata-only result rather than rejecting a
        # perfectly good raw file.
        result = Extracted(extractor="")
        result.warnings.append("pillow could not decode this image; metadata only")

    if exif_record:
        result = merge_extracted(result, from_exiftool(exif_record))
    return result
