"""Content identity: hashing, media-type detection, perceptual hashing.

An asset *is* its bytes. Everything in this module exists to answer "which
asset is this file" without reference to where the file happens to live, which
is what makes a moved or renamed file the same asset (principle 2).

Three separate questions, deliberately not conflated:

**What are these bytes?** :func:`hash_file` — BLAKE3 when available, SHA-256
otherwise. The digest is stored prefixed (``b3:…`` / ``sha256:…``) so it is
self-describing on the wire and in the database, and so two algorithms can
never collide in the ``assets.content_hash`` UNIQUE index.

**What kind of file is it?** :func:`detect_media_type` — magic bytes first,
always. The extension is consulted only as a *tiebreaker inside a container
family the signature already confirmed*: a `.CR2` and a `.NEF` are both TIFF,
a `.docx` and an `.epub` are both ZIP. Typing a file from its extension alone
is how a renamed `.txt` ends up in the photo grid.

**What does it look like?** :func:`perceptual_hash` — a 64-bit dhash, for
near-duplicate grouping. Cheap, rotation-sensitive, and good enough to catch
the same photo saved twice at different quality.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..errors import CorruptMedia
from ..types import MediaType
from ..util import long_path

__all__ = [
    "HashResult",
    "Detection",
    "hash_file",
    "hash_bytes",
    "detect_media_type",
    "sniff_bytes",
    "perceptual_hash",
    "perceptual_hash_image",
    "hamming_distance",
    "blake3_available",
    "RAW_EXTENSIONS",
    "SIDECAR_EXTENSIONS",
    "MOTION_VIDEO_EXTENSIONS",
    "HEAD_BYTES",
]

#: How much of a file the sniffer reads. Every signature below lives well
#: inside this, and one 4 KiB read is a single disk seek.
HEAD_BYTES: Final[int] = 4096

#: Algorithm name -> the prefix written into ``assets.content_hash``.
_HASH_PREFIX: Final[dict[str, str]] = {"blake3": "b3", "sha256": "sha256"}

#: Extensions that denote a camera raw file. Used by the sniffer to pick a
#: flavour once the TIFF/other signature is confirmed, and by
#: :mod:`mediaengine.core.sidecars` to find the RAW half of a RAW+JPEG pair.
RAW_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        "cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "raf",
        "orf", "rw2", "raw", "pef", "ptx", "srw", "x3f", "erf", "mef", "mos",
        "iiq", "3fr", "fff", "dcr", "kdc", "mrw", "rwl",
    }
)

#: Metadata companions that are never assets in their own right.
SIDECAR_EXTENSIONS: Final[frozenset[str]] = frozenset({"xmp", "aae", "thm", "pp3", "on1", "dop"})

#: The video half of a paired motion photo.
MOTION_VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset({"mov", "mp4", "m4v"})

# ── magic-byte signature table ───────────────────────────────────────────────
# (offset, signature, mime, media_type). Checked in order, so put the specific
# entries above the general ones they would otherwise be shadowed by.

_Signature = tuple[int, bytes, str, MediaType]

_SIGNATURES: Final[tuple[_Signature, ...]] = (
    # Images
    (0, b"\xff\xd8\xff", "image/jpeg", MediaType.IMAGE),
    (0, b"\x89PNG\r\n\x1a\n", "image/png", MediaType.IMAGE),
    (0, b"GIF87a", "image/gif", MediaType.IMAGE),
    (0, b"GIF89a", "image/gif", MediaType.IMAGE),
    (0, b"BM", "image/bmp", MediaType.IMAGE),
    (0, b"\x00\x00\x01\x00", "image/x-icon", MediaType.IMAGE),
    (0, b"\x00\x00\x02\x00", "image/x-icon", MediaType.IMAGE),
    (0, b"8BPS", "image/vnd.adobe.photoshop", MediaType.IMAGE),
    (0, b"\x00\x00\x00\x0cjP  ", "image/jp2", MediaType.IMAGE),
    (0, b"\xff\x4f\xff\x51", "image/j2c", MediaType.IMAGE),
    (0, b"FUJIFILMCCD-RAW", "image/x-fuji-raf", MediaType.IMAGE),
    (0, b"FOVb", "image/x-sigma-x3f", MediaType.IMAGE),
    (0, b"IIU\x00", "image/x-panasonic-rw2", MediaType.IMAGE),
    (0, b"P1", "image/x-portable-bitmap", MediaType.IMAGE),
    (0, b"P4", "image/x-portable-bitmap", MediaType.IMAGE),
    (0, b"P5", "image/x-portable-graymap", MediaType.IMAGE),
    (0, b"P6", "image/x-portable-pixmap", MediaType.IMAGE),
    (0, b"qoif", "image/qoi", MediaType.IMAGE),
    (0, b"\x76\x2f\x31\x01", "image/x-exr", MediaType.IMAGE),
    (0, b"#?RADIANCE", "image/vnd.radiance", MediaType.IMAGE),
    (0, b"DDS ", "image/vnd-ms.dds", MediaType.IMAGE),
    (0, b"\xff\x0a", "image/jxl", MediaType.IMAGE),
    (0, b"\x00\x00\x00\x0cJXL \r\n\x87\n", "image/jxl", MediaType.IMAGE),
    # Video
    (0, b"\x1a\x45\xdf\xa3", "video/x-matroska", MediaType.VIDEO),  # also .webm
    (0, b"FLV\x01", "video/x-flv", MediaType.VIDEO),
    (0, b"\x00\x00\x01\xba", "video/mpeg", MediaType.VIDEO),
    (0, b"\x00\x00\x01\xb3", "video/mpeg", MediaType.VIDEO),
    (0, b"\x30\x26\xb2\x75\x8e\x66\xcf\x11", "video/x-ms-asf", MediaType.VIDEO),
    (4, b"moov", "video/quicktime", MediaType.VIDEO),
    (4, b"mdat", "video/quicktime", MediaType.VIDEO),
    # Audio
    (0, b"fLaC", "audio/flac", MediaType.AUDIO),
    (0, b"ID3", "audio/mpeg", MediaType.AUDIO),
    (0, b"\xff\xfb", "audio/mpeg", MediaType.AUDIO),
    (0, b"\xff\xf3", "audio/mpeg", MediaType.AUDIO),
    (0, b"\xff\xf2", "audio/mpeg", MediaType.AUDIO),
    (0, b"MAC ", "audio/x-ape", MediaType.AUDIO),
    (0, b"MThd", "audio/midi", MediaType.AUDIO),
    (0, b"\x2e\x73\x6e\x64", "audio/basic", MediaType.AUDIO),
    (0, b"wvpk", "audio/x-wavpack", MediaType.AUDIO),
    # Documents
    (0, b"%PDF-", "application/pdf", MediaType.DOCUMENT),
    (0, b"{\\rtf", "application/rtf", MediaType.DOCUMENT),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage", MediaType.DOCUMENT),
    (0, b"%!PS", "application/postscript", MediaType.DOCUMENT),
    (0, b"\x38\x42\x50\x53", "image/vnd.adobe.photoshop", MediaType.IMAGE),
)

#: ``ftyp`` brands, read at offset 8. The brand is the only thing separating a
#: HEIC photo from an MP4 video — both are ISO base media containers.
_FTYP_BRANDS: Final[dict[bytes, tuple[str, MediaType]]] = {
    b"heic": ("image/heic", MediaType.IMAGE),
    b"heix": ("image/heic", MediaType.IMAGE),
    b"heim": ("image/heic", MediaType.IMAGE),
    b"heis": ("image/heic", MediaType.IMAGE),
    b"hevc": ("image/heic-sequence", MediaType.IMAGE),
    b"hevm": ("image/heic-sequence", MediaType.IMAGE),
    b"hevs": ("image/heic-sequence", MediaType.IMAGE),
    b"mif1": ("image/heif", MediaType.IMAGE),
    b"msf1": ("image/heif-sequence", MediaType.IMAGE),
    b"avif": ("image/avif", MediaType.IMAGE),
    b"avis": ("image/avif-sequence", MediaType.IMAGE),
    b"crx ": ("image/x-canon-cr3", MediaType.IMAGE),
    b"qt  ": ("video/quicktime", MediaType.VIDEO),
    b"M4A ": ("audio/mp4", MediaType.AUDIO),
    b"M4B ": ("audio/mp4", MediaType.AUDIO),
    b"M4P ": ("audio/mp4", MediaType.AUDIO),
    b"M4V ": ("video/x-m4v", MediaType.VIDEO),
    b"f4v ": ("video/mp4", MediaType.VIDEO),
}

#: RIFF sub-types, read at offset 8.
_RIFF_FORMS: Final[dict[bytes, tuple[str, MediaType]]] = {
    b"WEBP": ("image/webp", MediaType.IMAGE),
    b"WAVE": ("audio/wav", MediaType.AUDIO),
    b"AVI ": ("video/x-msvideo", MediaType.VIDEO),
    b"ACON": ("image/x-cursor", MediaType.IMAGE),
}

#: Interesting members of a ZIP container, longest-prefix first.
_ZIP_MEMBERS: Final[tuple[tuple[str, str, MediaType], ...]] = (
    ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", MediaType.DOCUMENT),
    ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", MediaType.DOCUMENT),
    ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation", MediaType.DOCUMENT),
    ("META-INF/container.xml", "application/epub+zip", MediaType.DOCUMENT),
    ("content.xml", "application/vnd.oasis.opendocument.text", MediaType.DOCUMENT),
)

#: Extension -> mime for raw files, once the container signature is confirmed.
_RAW_MIME: Final[dict[str, str]] = {
    "cr2": "image/x-canon-cr2",
    "cr3": "image/x-canon-cr3",
    "crw": "image/x-canon-crw",
    "nef": "image/x-nikon-nef",
    "nrw": "image/x-nikon-nrw",
    "arw": "image/x-sony-arw",
    "sr2": "image/x-sony-sr2",
    "srf": "image/x-sony-srf",
    "dng": "image/x-adobe-dng",
    "raf": "image/x-fuji-raf",
    "orf": "image/x-olympus-orf",
    "rw2": "image/x-panasonic-rw2",
    "pef": "image/x-pentax-pef",
    "srw": "image/x-samsung-srw",
    "x3f": "image/x-sigma-x3f",
    "erf": "image/x-epson-erf",
    "mrw": "image/x-minolta-mrw",
    "3fr": "image/x-hasselblad-3fr",
    "iiq": "image/x-phaseone-iiq",
    "rwl": "image/x-leica-rwl",
}

#: Extension -> mime for the OLE2 (pre-2007 Office) family, which shares one
#: signature across Word, Excel and PowerPoint.
_OLE_MIME: Final[dict[str, str]] = {
    "doc": "application/msword",
    "xls": "application/vnd.ms-excel",
    "ppt": "application/vnd.ms-powerpoint",
    "msg": "application/vnd.ms-outlook",
}

#: Text-ish extensions the sniffer will accept once the bytes look like text.
_TEXT_MIME: Final[dict[str, str]] = {
    "txt": "text/plain",
    "md": "text/markdown",
    "rst": "text/x-rst",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "json": "application/json",
    "xml": "text/xml",
    "html": "text/html",
    "htm": "text/html",
    "yaml": "text/yaml",
    "yml": "text/yaml",
    "log": "text/plain",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "tex": "text/x-tex",
}


def blake3_available() -> bool:
    """Whether the optional BLAKE3 extra is installed."""
    try:
        import blake3  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class HashResult:
    """A file's content identity."""

    content_hash: str
    """Prefixed digest, e.g. ``b3:9f2c…``. This is what goes in ``assets``."""

    algorithm: str
    """``blake3`` or ``sha256`` — the bare name, for ``assets.hash_algorithm``."""

    size_bytes: int
    """Bytes actually read. Compared against ``stat`` to catch a file being
    rewritten underneath the scan."""

    head: bytes
    """First :data:`HEAD_BYTES` of the file, so the sniffer needs no second
    read of a file already streamed through the hasher."""


@dataclass(frozen=True, slots=True)
class Detection:
    """The outcome of magic-byte sniffing."""

    media_type: MediaType
    mime_type: str | None
    is_raw: bool = False
    """A camera raw still. Relevant to RAW+JPEG pairing, not to storage."""

    container: str | None = None
    """The outer container when it differs from the mime, e.g. ``iso-bmff``
    for HEIC or ``zip`` for docx. Recorded in technical metadata."""


def _new_hasher(algorithm: str) -> tuple[str, Any]:
    """Return ``(effective_algorithm, hasher)``, degrading blake3 → sha256.

    The fallback is silent by design: every extra in this project has a working
    substitute, and a library without blake3 installed should still index.
    """
    if algorithm == "blake3":
        try:
            from blake3 import blake3 as _blake3
        except ImportError:
            return "sha256", hashlib.sha256()
        return "blake3", _blake3()
    return "sha256", hashlib.sha256()


def hash_bytes(data: bytes, *, algorithm: str = "blake3") -> str:
    """Hash an in-memory buffer. Used by tests and by inline transfers."""
    effective, hasher = _new_hasher(algorithm)
    hasher.update(data)
    return f"{_HASH_PREFIX[effective]}:{hasher.hexdigest()}"


def hash_file(
    path: Path | str,
    *,
    algorithm: str = "blake3",
    chunk_size: int = 4 * 1024 * 1024,
    progress: Callable[[int], None] | None = None,
) -> HashResult:
    """Stream a file through the hasher, capturing its head on the way.

    Reads in ``chunk_size`` blocks so a 40 GB video costs no more memory than a
    thumbnail. The first chunk is retained as :attr:`HashResult.head` because
    the caller invariably wants to sniff the file it just hashed, and a second
    open is a second seek on a spinning disk.

    :raises CorruptMedia: the file vanished or became unreadable mid-read. The
        scan treats that as a per-file error rather than aborting.
    """
    target = Path(path)
    effective, hasher = _new_hasher(algorithm)
    total = 0
    head = b""
    try:
        with open(long_path(target), "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                if not head:
                    head = chunk[:HEAD_BYTES]
                hasher.update(chunk)
                total += len(chunk)
                if progress is not None:
                    progress(total)
    except OSError as exc:
        raise CorruptMedia(f"{target}: unreadable during hashing: {exc}") from exc
    return HashResult(
        content_hash=f"{_HASH_PREFIX[effective]}:{hasher.hexdigest()}",
        algorithm=effective,
        size_bytes=total,
        head=head,
    )


def _looks_like_text(head: bytes) -> bool:
    """Heuristic: decodable, no NULs, mostly printable.

    Deliberately conservative. A false positive here files a binary blob as a
    searchable document and pollutes the full-text index.
    """
    if not head:
        return False
    if b"\x00" in head:
        # UTF-16 is full of NULs, so check for its BOM before giving up.
        return head.startswith((b"\xff\xfe", b"\xfe\xff"))
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = head.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t\f\v")
    return printable / max(1, len(text)) > 0.92


def _sniff_zip(path: Path | None) -> Detection | None:
    """Look inside a ZIP to tell docx from xlsx from epub.

    An OOXML file and an EPUB are both ZIPs; only the member list distinguishes
    them, and the member list is authoritative in a way the extension is not.
    """
    if path is None:
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "mimetype" in names:
                try:
                    declared = archive.read("mimetype").decode("ascii", "ignore").strip()
                except (KeyError, OSError):
                    declared = ""
                if declared == "application/epub+zip":
                    return Detection(MediaType.DOCUMENT, declared, container="zip")
            for member, mime, media in _ZIP_MEMBERS:
                if member in names:
                    return Detection(media, mime, container="zip")
    except (zipfile.BadZipFile, OSError):
        return None
    return Detection(MediaType.OTHER, "application/zip", container="zip")


def sniff_bytes(
    head: bytes, *, extension: str = "", path: Path | None = None
) -> Detection:
    """Classify from a file's leading bytes.

    ``extension`` and ``path`` are refinements, never the primary decision:
    ``extension`` selects a flavour within a container family the signature has
    already confirmed, and ``path`` lets the ZIP and TIFF branches read a
    little further into the file.
    """
    ext = extension.lower().lstrip(".")

    if not head:
        return Detection(MediaType.OTHER, None)

    # ISO base media format: brand at offset 8 decides image vs video vs audio.
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        known = _FTYP_BRANDS.get(brand)
        if known is not None:
            mime, media = known
            return Detection(media, mime, is_raw=brand == b"crx ", container="iso-bmff")
        if brand[:3] == b"3gp":
            return Detection(MediaType.VIDEO, "video/3gpp", container="iso-bmff")
        # isom/mp41/mp42/avc1/dash/… — a plain MP4. An audio-only MP4 is
        # indistinguishable here; ffprobe corrects it during extraction.
        return Detection(MediaType.VIDEO, "video/mp4", container="iso-bmff")

    if len(head) >= 12 and head[0:4] == b"RIFF":
        form = _RIFF_FORMS.get(head[8:12])
        if form is not None:
            mime, media = form
            return Detection(media, mime, container="riff")
        return Detection(MediaType.OTHER, "application/x-riff", container="riff")

    if head.startswith(b"OggS"):
        # Ogg carries Vorbis/Opus/FLAC audio far more often than Theora video.
        mime = "video/ogg" if ext in {"ogv", "ogx"} else "audio/ogg"
        media = MediaType.VIDEO if mime.startswith("video") else MediaType.AUDIO
        return Detection(media, mime, container="ogg")

    if head.startswith(b"FORM") and head[8:12] in (b"AIFF", b"AIFC"):
        return Detection(MediaType.AUDIO, "audio/aiff", container="iff")

    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        # TIFF, and every TIFF-based raw format. CR2 stamps 'CR' at offset 8;
        # the rest are told apart only by extension, which is legitimate here
        # because the container is already proven.
        if head[8:10] == b"CR":
            return Detection(MediaType.IMAGE, "image/x-canon-cr2", is_raw=True, container="tiff")
        if ext in RAW_EXTENSIONS:
            return Detection(
                MediaType.IMAGE,
                _RAW_MIME.get(ext, f"image/x-raw-{ext}"),
                is_raw=True,
                container="tiff",
            )
        return Detection(MediaType.IMAGE, "image/tiff", container="tiff")

    if head.startswith(b"PK\x03\x04"):
        detected = _sniff_zip(path)
        if detected is not None:
            return detected
        return Detection(MediaType.OTHER, "application/zip", container="zip")

    for offset, signature, mime, media in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            if mime == "application/x-ole-storage":
                return Detection(
                    MediaType.DOCUMENT,
                    _OLE_MIME.get(ext, "application/x-ole-storage"),
                    container="ole2",
                )
            if mime == "video/x-matroska" and ext == "webm":
                return Detection(MediaType.VIDEO, "video/webm", container="matroska")
            return Detection(media, mime, is_raw=ext in RAW_EXTENSIONS)

    # MPEG transport stream: 0x47 sync byte every 188 bytes. Checking three in
    # a row is enough to rule out a coincidence.
    if len(head) >= 565 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47:
        return Detection(MediaType.VIDEO, "video/mp2t", container="mpeg-ts")

    if head.lstrip()[:5].lower() in (b"<?xml", b"<svg"):
        stripped = head.lstrip().lower()
        if b"<svg" in stripped[:512]:
            return Detection(MediaType.IMAGE, "image/svg+xml")

    # `filetype` covers formats the table above does not. It is consulted last
    # so the engine's own answers stay stable when the optional extra is absent.
    try:
        import filetype
    except ImportError:
        pass
    else:
        guess = filetype.guess(head)
        if guess is not None:
            mime = str(guess.mime)
            family = mime.split("/", 1)[0]
            media = {
                "image": MediaType.IMAGE,
                "video": MediaType.VIDEO,
                "audio": MediaType.AUDIO,
            }.get(family, MediaType.OTHER)
            if media is MediaType.OTHER and mime == "application/pdf":
                media = MediaType.DOCUMENT
            return Detection(media, mime, is_raw=ext in RAW_EXTENSIONS)

    if _looks_like_text(head):
        return Detection(MediaType.DOCUMENT, _TEXT_MIME.get(ext, "text/plain"))

    return Detection(MediaType.OTHER, None)


def detect_media_type(
    path: Path | str, *, head: bytes | None = None
) -> Detection:
    """Classify a file on disk. Reads its head if not already supplied."""
    target = Path(path)
    if head is None:
        try:
            with open(long_path(target), "rb") as handle:
                head = handle.read(HEAD_BYTES)
        except OSError as exc:
            raise CorruptMedia(f"{target}: unreadable during type detection: {exc}") from exc
    return sniff_bytes(head, extension=target.suffix, path=target)


# ── perceptual hashing ───────────────────────────────────────────────────────


def perceptual_hash_image(image: Any) -> str:
    """dhash of an already-open :class:`PIL.Image.Image`.

    Downsamples to 9×8 greyscale and records, for each row, whether each pixel
    is brighter than the one to its right. 64 comparisons, 64 bits, rendered as
    16 hex characters — the width ``assets.perceptual_hash`` expects.

    Gradient comparison rather than absolute values is what makes this survive
    re-encoding and brightness shifts, which is exactly the near-duplicate case
    it exists to catch.
    """
    from PIL import Image

    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits <<= 1
            if pixels[base + col] > pixels[base + col + 1]:
                bits |= 1
    return f"{bits:016x}"


def perceptual_hash(path: Path | str) -> str | None:
    """dhash of an image file, or ``None`` if it cannot be decoded.

    Returning ``None`` rather than raising is deliberate: a thumbnail that
    fails to generate must not fail the whole ingest of an otherwise perfectly
    good asset.
    """
    try:
        from PIL import Image, ImageFile

        # A truncated JPEG still has usable pixels in the part that arrived,
        # and a perceptual hash of most of an image is more useful than none.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(path) as image:
            image.load()
            return perceptual_hash_image(image)
    except Exception:  # noqa: BLE001 - any decode failure means "no phash"
        return None


def hamming_distance(left: str, right: str) -> int:
    """Bit distance between two hex perceptual hashes.

    ``64`` — the maximum — is returned for malformed input, so a corrupt hash
    can never masquerade as a near-duplicate match.
    """
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def is_sidecar(path: Path | str) -> bool:
    """Whether this path is a metadata companion rather than an asset."""
    return Path(path).suffix.lower().lstrip(".") in SIDECAR_EXTENSIONS


def hash_dirname(content_hash: str) -> tuple[str, str]:
    """Split a content hash into ``(shard, safe_name)`` for the cache layout.

    The ``:`` in ``b3:9f2c…`` is illegal in a Windows filename, so it becomes
    ``_``. Sharding on the first two hex characters keeps any one directory to
    roughly 1/256th of the library, which matters once a cache holds a few
    hundred thousand entries.
    """
    digest = content_hash.split(":", 1)[-1]
    return digest[:2] or "00", content_hash.replace(":", "_")


def nearest_by_phash(
    target: str, candidates: Sequence[tuple[int, str]], *, max_distance: int = 8
) -> list[tuple[int, int]]:
    """``(asset_id, distance)`` for candidates within ``max_distance``, closest first."""
    scored = [
        (asset_id, hamming_distance(target, phash))
        for asset_id, phash in candidates
        if phash
    ]
    return sorted(
        ((a, d) for a, d in scored if d <= max_distance), key=lambda item: item[1]
    )
