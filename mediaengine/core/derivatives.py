"""The derivative cache: thumbnails, keyframes, proxies, extracted audio.

Two rules govern everything in this module.

**Originals are never touched.** Every byte written goes under
``storage.derivatives_path``, which :meth:`Config.ensure_directories` refuses
to place inside a library root. Source files are opened read-only.

**The cache is content-addressed and disposable.** A derivative lives at
``<shard>/<content-hash>/<name>``, so two copies of the same photo share one
thumbnail, a moved file keeps its thumbnails, and every path is derivable from
the asset alone. Each one is also recorded in the ``derivatives`` table, which
is what makes "delete everything derived for this asset" a transaction rather
than a tree walk.

Writes are atomic: render to ``.tmp`` in the same directory, then rename. An
interrupted scan therefore leaves either no thumbnail or a complete one, never
a truncated WebP that the next run happily believes in.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..db.repositories import DerivativeRepository
from ..errors import SubprocessFailed, SubprocessTimeout
from ..types import MediaType
from .identity import hash_dirname
from .procs import have_binary, run_command

__all__ = ["DerivativeBuilder", "DerivativeResult", "render_thumbnails", "derivative_dir"]

_LOG = logging.getLogger(__name__)

#: ``[showinfo]`` writes one line per selected frame; this pulls the timestamp
#: out of it, which is how scene-detected keyframes learn what time they are.
_PTS_TIME = re.compile(r"pts_time:(\d+(?:\.\d+)?)")

_MIME_BY_FORMAT = {"webp": "image/webp", "jpeg": "image/jpeg", "png": "image/png"}


def derivative_dir(root: Path, content_hash: str) -> Path:
    """Where every derivative of one asset lives.

    Sharded on the first two hex characters so no directory holds more than
    roughly 1/256th of the library — some filesystems degrade badly past a few
    tens of thousands of entries in one directory.
    """
    shard, safe = hash_dirname(content_hash)
    return root / shard / safe


@dataclass(slots=True)
class DerivativeResult:
    """What one asset's derivative pass produced."""

    thumbnails: list[dict[str, Any]] = field(default_factory=list)
    keyframes: list[dict[str, Any]] = field(default_factory=list)
    proxy: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    reused: int = 0
    """Derivatives that already existed on disk and were left alone. On a
    re-scan this should equal the total, which is what makes re-running cheap."""

    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return (
            len(self.thumbnails)
            + len(self.keyframes)
            + (1 if self.proxy else 0)
            + (1 if self.audio else 0)
        )


def _atomic_write_path(destination: Path) -> Path:
    """A temp path in the destination's directory, so rename stays atomic."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(handle)
    return Path(name)


def _finalise(temp: Path, destination: Path) -> int:
    """Rename a completed temp file into place; returns its size."""
    size = temp.stat().st_size
    os.replace(temp, destination)
    return size


def render_thumbnails(
    source: str,
    out_dir: str,
    sizes: list[int],
    *,
    fmt: str = "webp",
    quality: int = 82,
    overwrite: bool = False,
) -> list[dict[str, Any]]:
    """Render one image to several thumbnail sizes.

    A module-level function of plain data on purpose: it is picklable, so the
    pool this runs in can be swapped from threads to processes by changing one
    line in :class:`DerivativeBuilder`, with no other code moving.

    Sizes larger than the source are skipped rather than upscaled — an
    upscaled thumbnail is bytes spent to make an image worse — except that the
    smallest requested size is always produced so every asset has at least one
    thumbnail to show in a grid.
    """
    from PIL import Image, ImageFile, ImageOps

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    produced: list[dict[str, Any]] = []
    directory = Path(out_dir)
    extension = "jpg" if fmt == "jpeg" else fmt

    with Image.open(source) as opened:
        # Honour the EXIF rotation once, here. Every downstream consumer then
        # gets upright pixels and none of them needs to know about orientation.
        image = ImageOps.exif_transpose(opened) or opened
        if image.mode in ("P", "PA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        elif image.mode in ("CMYK", "YCbCr", "LAB", "HSV", "I", "F", "I;16"):
            image = image.convert("RGB")
        if fmt == "jpeg" and image.mode in ("RGBA", "LA"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            image = background

        longest = max(image.width, image.height)
        ordered = sorted(set(sizes))
        for index, size in enumerate(ordered):
            if size > longest and index > 0:
                continue
            destination = directory / f"thumb_{size}.{extension}"
            if destination.exists() and not overwrite:
                try:
                    with Image.open(destination) as existing:
                        produced.append(
                            {
                                "variant": str(size),
                                "rel_name": destination.name,
                                "width": existing.width,
                                "height": existing.height,
                                "size_bytes": destination.stat().st_size,
                                "reused": True,
                            }
                        )
                        continue
                except OSError:
                    destination.unlink(missing_ok=True)  # half-written; redo it

            thumb = image.copy()
            thumb.thumbnail((size, size), Image.Resampling.LANCZOS)
            temp = _atomic_write_path(destination)
            try:
                save_options: dict[str, Any] = {"quality": quality}
                if fmt == "webp":
                    save_options["method"] = 4
                elif fmt == "jpeg":
                    save_options["optimize"] = True
                    save_options["progressive"] = True
                thumb.save(temp, format=fmt.upper(), **save_options)
                written = _finalise(temp, destination)
            finally:
                temp.unlink(missing_ok=True)
            produced.append(
                {
                    "variant": str(size),
                    "rel_name": destination.name,
                    "width": thumb.width,
                    "height": thumb.height,
                    "size_bytes": written,
                    "reused": False,
                }
            )
    return produced


class DerivativeBuilder:
    """Generates and records every derivative for an asset."""

    def __init__(self, config: Config, repository: DerivativeRepository) -> None:
        self.config = config
        self.repository = repository
        self.root = Path(config.storage.derivatives_path)
        self._have_ffmpeg = have_binary("ffmpeg")

    # ── entry point ─────────────────────────────────────────────────────────

    def build(
        self,
        *,
        asset_id: int,
        content_hash: str,
        source: Path,
        media_type: MediaType,
        mime_type: str | None = None,
        duration_s: float | None = None,
        is_raw: bool = False,
        overwrite: bool = False,
    ) -> DerivativeResult:
        """Produce and record everything this asset needs.

        Failures are collected as warnings rather than raised: a video whose
        proxy will not transcode still deserves its thumbnails, and an asset
        with no thumbnail at all is still an indexed, searchable asset.
        """
        result = DerivativeResult()
        target = derivative_dir(self.root, content_hash)
        target.mkdir(parents=True, exist_ok=True)

        try:
            if media_type is MediaType.IMAGE:
                self._image_thumbnails(
                    asset_id, target, source, result, overwrite=overwrite, is_raw=is_raw
                )
            elif media_type is MediaType.VIDEO:
                self._video(
                    asset_id, target, source, result, duration_s=duration_s, overwrite=overwrite
                )
            elif media_type is MediaType.AUDIO:
                self._audio_artwork(asset_id, target, source, result, overwrite=overwrite)
            elif media_type is MediaType.DOCUMENT:
                self._document_preview(
                    asset_id, target, source, result, mime_type=mime_type, overwrite=overwrite
                )
        except (SubprocessTimeout, SubprocessFailed) as exc:
            result.warnings.append(f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop a scan
            _LOG.warning("derivative generation failed for %s: %s", source, exc)
            result.warnings.append(f"derivative generation failed: {exc}")

        return result

    # ── images ──────────────────────────────────────────────────────────────

    def _image_thumbnails(
        self,
        asset_id: int,
        target: Path,
        source: Path,
        result: DerivativeResult,
        *,
        overwrite: bool,
        is_raw: bool = False,
    ) -> None:
        storage = self.config.storage
        renderable = source
        if is_raw:
            # Pillow cannot decode most camera raw formats, and every raw file
            # in a library would otherwise log a failure. Every raw file does
            # however embed a full-size JPEG preview, which is both extractable
            # and exactly what a grid should show.
            preview = self._raw_preview(source, target, overwrite=overwrite)
            if preview is None:
                result.warnings.append(
                    "raw preview unavailable (install exiftool for raw thumbnails)"
                )
                return
            renderable = preview
        rendered = render_thumbnails(
            str(renderable),
            str(target),
            list(storage.thumbnail_sizes),
            fmt=storage.thumbnail_format,
            quality=storage.thumbnail_quality,
            overwrite=overwrite,
        )
        for item in rendered:
            self._record(asset_id, "thumb", str(item["variant"]), target, item, result)

    def _raw_preview(self, source: Path, target: Path, *, overwrite: bool) -> Path | None:
        """Pull the embedded JPEG preview out of a camera raw file.

        ExifTool is the only reliable way to do this across the dozen raw
        formats in circulation. Returns ``None`` when it is not installed, and
        the caller degrades to no thumbnail rather than to a broken one.
        """
        preview = target / "raw_preview.jpg"
        if preview.exists() and not overwrite:
            return preview
        if not have_binary("exiftool"):
            return None
        for tag in ("-PreviewImage", "-JpgFromRaw", "-OtherImage", "-ThumbnailImage"):
            temp = _atomic_write_path(preview)
            try:
                result = run_command(
                    ["exiftool", "-b", tag, str(source)],
                    timeout=self.config.workers.subprocess_timeout_s,
                )
                if not result.stdout.startswith(b"\xff\xd8\xff"):
                    continue
                temp.write_bytes(result.stdout)
                _finalise(temp, preview)
                return preview
            except (SubprocessTimeout, SubprocessFailed, OSError):
                continue
            finally:
                temp.unlink(missing_ok=True)
        return None

    # ── video ───────────────────────────────────────────────────────────────

    def _video(
        self,
        asset_id: int,
        target: Path,
        source: Path,
        result: DerivativeResult,
        *,
        duration_s: float | None,
        overwrite: bool,
    ) -> None:
        if not self._have_ffmpeg:
            result.warnings.append("ffmpeg not installed; no video derivatives")
            return

        times = self._keyframe_times(source, duration_s)
        poster: Path | None = None
        for moment in times:
            frame = self._grab_frame(source, target, moment, overwrite=overwrite)
            if frame is None:
                continue
            if poster is None:
                poster = frame
            variant = f"{moment:.2f}"
            item: dict[str, Any] = {
                "variant": variant,
                "rel_name": frame.name,
                "size_bytes": frame.stat().st_size,
                "width": None,
                "height": None,
            }
            self._record(asset_id, "keyframe", variant, target, item, result, bucket="keyframes")

        # The poster frame is what a grid shows, so it goes through the same
        # thumbnail sizes as a still image and lands under the same 'thumb'
        # kind. A UI asking for thumb/512 then needs no per-type branching.
        if poster is not None:
            storage = self.config.storage
            for item in render_thumbnails(
                str(poster),
                str(target),
                list(storage.thumbnail_sizes),
                fmt=storage.thumbnail_format,
                quality=storage.thumbnail_quality,
                overwrite=overwrite,
            ):
                self._record(asset_id, "thumb", str(item["variant"]), target, item, result)

        if self.config.storage.video_proxy:
            self._proxy(asset_id, target, source, result, overwrite=overwrite)

    def _keyframe_times(self, source: Path, duration_s: float | None) -> list[float]:
        """Fixed-interval samples, plus scene changes when enabled.

        Interval sampling guarantees even coverage of a long, static video;
        scene detection catches the cuts that interval sampling walks straight
        past. Neither alone is enough for a plugin trying to describe a video
        from a handful of frames.
        """
        scan = self.config.scan
        moments: list[float] = []
        if duration_s and duration_s > 0:
            step = scan.video_keyframe_interval_s
            # Offset by half a step: the frame at t=0 is very often a black
            # fade-in and tells a classifier nothing.
            moment = min(step / 2.0, duration_s / 2.0)
            while moment < duration_s and len(moments) < scan.video_keyframe_max:
                moments.append(round(moment, 2))
                moment += step
        if not moments:
            moments = [0.0]

        if scan.video_scene_detection and duration_s and duration_s > 0:
            for moment in self._scene_times(source, scan.video_scene_threshold):
                if len(moments) >= scan.video_keyframe_max:
                    break
                # Skip anything within a second of a frame already scheduled.
                if all(abs(moment - existing) > 1.0 for existing in moments):
                    moments.append(round(moment, 2))

        return sorted(moments)[: scan.video_keyframe_max]

    def _scene_times(self, source: Path, threshold: float) -> list[float]:
        """Timestamps of scene changes, read out of ffmpeg's showinfo output.

        Decodes to null — no frames are written — so this costs one pass of
        decode and no disk. Failure is silent: interval keyframes alone are a
        perfectly usable result.
        """
        try:
            result = run_command(
                [
                    "ffmpeg",
                    "-v", "info",
                    "-nostdin",
                    "-i", str(source),
                    "-vf", f"select='gt(scene,{threshold})',showinfo",
                    "-fps_mode", "vfr",
                    "-f", "null",
                    "-",
                ],
                timeout=self.config.workers.subprocess_timeout_s,
            )
        except (SubprocessTimeout, SubprocessFailed) as exc:
            _LOG.debug("scene detection failed for %s: %s", source, exc)
            return []
        return [float(m) for m in _PTS_TIME.findall(result.stderr.decode("utf-8", "replace"))]

    def _grab_frame(
        self, source: Path, target: Path, moment: float, *, overwrite: bool
    ) -> Path | None:
        """Write one frame as an image. ``-ss`` before ``-i`` for a fast seek."""
        extension = "jpg" if self.config.storage.thumbnail_format == "jpeg" else self.config.storage.thumbnail_format
        # Centiseconds in the filename so lexical and numeric order agree.
        destination = target / f"kf_{int(round(moment * 100)):08d}.{extension}"
        if destination.exists() and not overwrite:
            return destination

        temp = _atomic_write_path(destination)
        try:
            run_command(
                [
                    "ffmpeg",
                    "-v", "error",
                    "-nostdin",
                    "-y",
                    "-ss", f"{moment:.3f}",
                    "-i", str(source),
                    "-frames:v", "1",
                    "-vf", f"scale='min({max(self.config.storage.thumbnail_sizes)},iw)':-2",
                    "-f", "image2",
                    "-c:v", "libwebp" if extension == "webp" else "mjpeg",
                    str(temp),
                ],
                timeout=self.config.workers.subprocess_timeout_s,
            )
            if temp.stat().st_size == 0:
                return None
            _finalise(temp, destination)
        except (SubprocessTimeout, SubprocessFailed, OSError) as exc:
            _LOG.debug("keyframe at %.2fs failed for %s: %s", moment, source, exc)
            return None
        finally:
            temp.unlink(missing_ok=True)
        return destination

    def _proxy(
        self,
        asset_id: int,
        target: Path,
        source: Path,
        result: DerivativeResult,
        *,
        overwrite: bool,
    ) -> None:
        """A small H.264 copy for scrubbing in a browser."""
        height = self.config.storage.video_proxy_height
        destination = target / f"proxy_{height}.mp4"
        if destination.exists() and not overwrite:
            result.reused += 1
            self._record_existing(asset_id, "proxy", str(height), target, destination, result, "proxy")
            return

        temp = _atomic_write_path(destination.with_suffix(".mp4"))
        try:
            run_command(
                [
                    "ffmpeg",
                    "-v", "error",
                    "-nostdin",
                    "-y",
                    "-i", str(source),
                    # -2 keeps the width even, which H.264 requires.
                    "-vf", f"scale=-2:'min({height},ih)'",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "26",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-movflags", "+faststart",
                    "-f", "mp4",
                    str(temp),
                ],
                # Transcoding is minutes, not seconds; the per-call subprocess
                # budget is far too small for it.
                timeout=max(self.config.workers.subprocess_timeout_s * 10, 600.0),
            )
            size = _finalise(temp, destination)
        except (SubprocessTimeout, SubprocessFailed, OSError) as exc:
            result.warnings.append(f"proxy transcode failed: {exc}")
            return
        finally:
            temp.unlink(missing_ok=True)

        item = {"variant": str(height), "rel_name": destination.name, "size_bytes": size,
                "width": None, "height": height}
        self._record(asset_id, "proxy", str(height), target, item, result, bucket="proxy")

    def extract_audio(
        self, *, asset_id: int, content_hash: str, source: Path, overwrite: bool = False
    ) -> dict[str, Any] | None:
        """Mono 16 kHz WAV, for speech and audio-analysis plugins.

        Not produced during a scan — it is only useful when such a plugin is
        installed, and writing one per video would double the cache for
        nothing. The plugin runner calls this on demand via
        ``AnalysisContext.audio_path()``.
        """
        if not self._have_ffmpeg:
            return None
        target = derivative_dir(self.root, content_hash)
        destination = target / "audio.wav"
        if destination.exists() and not overwrite:
            return {"kind": "audio", "variant": "wav", "path": str(destination),
                    "size_bytes": destination.stat().st_size}

        temp = _atomic_write_path(destination)
        try:
            run_command(
                [
                    "ffmpeg", "-v", "error", "-nostdin", "-y",
                    "-i", str(source),
                    "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le",
                    "-f", "wav",
                    str(temp),
                ],
                timeout=max(self.config.workers.subprocess_timeout_s * 5, 300.0),
            )
            size = _finalise(temp, destination)
        except (SubprocessTimeout, SubprocessFailed, OSError) as exc:
            _LOG.debug("audio extraction failed for %s: %s", source, exc)
            return None
        finally:
            temp.unlink(missing_ok=True)

        self.repository.record(
            asset_id, "audio", "wav", str(destination.relative_to(self.root)), size_bytes=size
        )
        return {"kind": "audio", "variant": "wav", "path": str(destination), "size_bytes": size}

    # ── audio and documents ─────────────────────────────────────────────────

    def _audio_artwork(
        self,
        asset_id: int,
        target: Path,
        source: Path,
        result: DerivativeResult,
        *,
        overwrite: bool,
    ) -> None:
        """Embedded cover art, if the file has any. Most music does."""
        if not self._have_ffmpeg:
            return
        cover = target / "cover.jpg"
        if not cover.exists() or overwrite:
            temp = _atomic_write_path(cover)
            try:
                run_command(
                    ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
                     "-an", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", str(temp)],
                    timeout=self.config.workers.subprocess_timeout_s,
                )
                if temp.stat().st_size == 0:
                    return
                _finalise(temp, cover)
            except (SubprocessTimeout, SubprocessFailed, OSError):
                return  # no embedded artwork; entirely normal
            finally:
                temp.unlink(missing_ok=True)

        storage = self.config.storage
        for item in render_thumbnails(
            str(cover), str(target), list(storage.thumbnail_sizes),
            fmt=storage.thumbnail_format, quality=storage.thumbnail_quality, overwrite=overwrite,
        ):
            self._record(asset_id, "thumb", str(item["variant"]), target, item, result)

    def _document_preview(
        self,
        asset_id: int,
        target: Path,
        source: Path,
        result: DerivativeResult,
        *,
        mime_type: str | None,
        overwrite: bool,
    ) -> None:
        """First page of a PDF as a thumbnail. Other documents get none."""
        if mime_type != "application/pdf":
            return
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:
            try:
                import fitz as pymupdf  # type: ignore[import-not-found, no-redef]
            except ImportError:
                return

        page_image = target / "page_1.png"
        if not page_image.exists() or overwrite:
            temp = _atomic_write_path(page_image)
            try:
                with pymupdf.open(str(source)) as document:
                    if not document.page_count:
                        return
                    largest = max(self.config.storage.thumbnail_sizes)
                    page = document.load_page(0)
                    # 72 dpi is the PDF unit; scale so the long edge lands near
                    # the largest thumbnail rather than rendering at full size.
                    zoom = min(4.0, largest / max(page.rect.width, page.rect.height, 1.0))
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                    pixmap.save(str(temp))
                _finalise(temp, page_image)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"PDF preview failed: {exc}")
                return
            finally:
                temp.unlink(missing_ok=True)

        storage = self.config.storage
        for item in render_thumbnails(
            str(page_image), str(target), list(storage.thumbnail_sizes),
            fmt=storage.thumbnail_format, quality=storage.thumbnail_quality, overwrite=overwrite,
        ):
            self._record(asset_id, "thumb", str(item["variant"]), target, item, result)

    # ── bookkeeping ─────────────────────────────────────────────────────────

    def _record(
        self,
        asset_id: int,
        kind: str,
        variant: str,
        target: Path,
        item: dict[str, Any],
        result: DerivativeResult,
        bucket: str = "thumbnails",
    ) -> None:
        """Index one derivative and file it into the result."""
        path = target / str(item["rel_name"])
        relative = str(path.relative_to(self.root))
        self.repository.record(
            asset_id,
            kind,
            variant,
            relative,
            size_bytes=item.get("size_bytes"),
            width=item.get("width"),
            height=item.get("height"),
        )
        entry = {**item, "kind": kind, "path": str(path), "rel_path": relative}
        if item.get("reused"):
            result.reused += 1
        else:
            result.bytes_written += int(item.get("size_bytes") or 0)
        if bucket == "thumbnails":
            result.thumbnails.append(entry)
        elif bucket == "keyframes":
            result.keyframes.append(entry)
        elif bucket == "proxy":
            result.proxy = entry
        elif bucket == "audio":
            result.audio = entry

    def _record_existing(
        self,
        asset_id: int,
        kind: str,
        variant: str,
        target: Path,
        path: Path,
        result: DerivativeResult,
        bucket: str,
    ) -> None:
        self._record(
            asset_id,
            kind,
            variant,
            target,
            {"rel_name": path.name, "size_bytes": path.stat().st_size, "reused": True,
             "variant": variant, "width": None, "height": None},
            result,
            bucket=bucket,
        )

    # ── purge ───────────────────────────────────────────────────────────────

    def purge_asset(self, asset_id: int, content_hash: str | None = None) -> int:
        """Delete an asset's derivatives from disk and from the index.

        The database rows go first, inside a transaction; the files follow.
        Getting that order wrong leaves rows pointing at nothing, which is a
        far more confusing failure than an orphaned file the sweeper collects.
        """
        recorded = self.repository.delete_for_asset(asset_id)
        removed = 0
        for relative in recorded:
            path = self.root / relative
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                _LOG.warning("could not delete derivative %s: %s", path, exc)
        if content_hash:
            directory = derivative_dir(self.root, content_hash)
            shutil.rmtree(directory, ignore_errors=True)
        return removed

    def paths_for(self, content_hash: str) -> dict[str, Any]:
        """Every derivative path for an asset, in the shape §2 of the plugin
        contract expects — this is what fills a work item's ``derivatives``."""
        directory = derivative_dir(self.root, content_hash)
        if not directory.is_dir():
            return {}
        thumbnails: dict[str, dict[str, str]] = {}
        keyframes: list[dict[str, Any]] = []
        payload: dict[str, Any] = {}
        for entry in sorted(directory.iterdir()):
            name = entry.name
            if name.startswith("thumb_"):
                size = name.split("_", 1)[1].split(".", 1)[0]
                thumbnails[size] = {"path": str(entry)}
            elif name.startswith("kf_"):
                centiseconds = name.split("_", 1)[1].split(".", 1)[0]
                keyframes.append({"time": int(centiseconds) / 100.0, "path": str(entry)})
            elif name.startswith("proxy_"):
                payload["proxy"] = {"path": str(entry)}
            elif name == "audio.wav":
                payload["audio"] = {"path": str(entry)}
        if thumbnails:
            payload["thumbnails"] = thumbnails
        if keyframes:
            payload["keyframes"] = keyframes
        return payload
