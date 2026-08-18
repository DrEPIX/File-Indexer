"""The ingest pipeline: walk → triage → identify → extract → derive → index.

Each stage is separately resumable, and the pipeline is arranged so that the
expensive ones only ever see files that need them.

**Triage is the whole performance story.** A re-scan of an unchanged library
does one indexed query per batch of paths and then stops. No file is opened, no
byte is hashed. Everything below only runs for files whose
``(size, mtime_ns, inode)`` differs from what is on record, which on a settled
library is approximately none of them.

**Concurrency.** Reading and decoding happen on a bounded thread pool; every
database write goes to the single writer thread, one transaction per batch. A
batch is the unit of atomicity because it is also the unit of resumption — a
crash costs at most one batch of re-work, and re-work is a no-op thanks to the
content-hash identity.

*A deliberate deviation from the milestone sketch:* threads, not
``ProcessPoolExecutor``. Every stage here is either waiting on a helper process
(ffprobe, ffmpeg, exiftool) or inside a Pillow C loop, and Pillow releases the
GIL around decode and encode — so threads already overlap the work — while
Windows ``spawn`` would re-import the interpreter per worker and forbid sharing
the ExifTool daemon. :func:`render_thumbnails` is nonetheless written as a
module-level pure function of plain data, so the pool can be swapped for
processes by changing :meth:`IngestPipeline._make_pool` and nothing else. The
tradeoff is recorded in ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..db.repositories import Repositories
from ..errors import CorruptMedia, MediaEngineError, OperationCancelled
from ..search.text import expand_filename
from ..types import FileStatus, LocationSource, MediaType, ScanState
from ..util import json_dumps, utcnow_iso
from .control import CancelToken, ProgressCallback, ProgressReporter
from .derivatives import DerivativeBuilder
from .extractors import Extracted, MetadataExtractor
from .identity import HashResult, detect_media_type, hash_file, perceptual_hash
from .sidecars import SidecarCandidate, SidecarLinker, find_embedded_motion
from .walker import BatchProducer, WalkBatch, WalkEntry, Walker

__all__ = ["IngestPipeline", "ScanResult", "IngestItem"]

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestItem:
    """One file carried between the read stage and the write stage."""

    entry: WalkEntry
    hashed: HashResult
    media_type: MediaType
    mime_type: str | None
    is_raw: bool
    container: str | None
    extracted: Extracted
    perceptual: str | None = None
    known_file_id: int | None = None
    was_known_path: bool = False
    motion_offset: int | None = None

    # Filled in by the write stage.
    asset_id: int = 0
    asset_created: bool = False


@dataclass(slots=True)
class ScanResult:
    """Everything an operator wants to know after a scan of one root."""

    scan_id: int
    root: str
    started_at: str
    finished_at: str = ""
    files_seen: int = 0
    files_new: int = 0
    files_updated: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    assets_created: int = 0
    bytes_hashed: int = 0
    derivatives_written: int = 0
    derivative_bytes: int = 0
    missing_marked: int = 0
    duration_s: float = 0.0
    cancelled: bool = False
    walk: dict[str, int] = field(default_factory=dict)
    links: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "root": self.root,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "files_seen": self.files_seen,
            "files_new": self.files_new,
            "files_updated": self.files_updated,
            "files_skipped": self.files_skipped,
            "files_failed": self.files_failed,
            "assets_created": self.assets_created,
            "bytes_hashed": self.bytes_hashed,
            "derivatives_written": self.derivatives_written,
            "derivative_bytes": self.derivative_bytes,
            "missing_marked": self.missing_marked,
            "duration_s": round(self.duration_s, 3),
            "cancelled": self.cancelled,
            "walk": self.walk,
            "links": self.links,
            "warnings": self.warnings[:20],
        }


class IngestPipeline:
    """Runs a scan. One instance per scan; not reusable across roots in
    parallel, because it owns an ExifTool daemon and a thread pool."""

    def __init__(
        self,
        config: Config,
        repositories: Repositories,
        *,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self.config = config
        self.repos = repositories
        self.cancel = CancelToken() if cancel is None else cancel
        self.reporter = ProgressReporter(progress)
        self.extractor = MetadataExtractor(config)
        self.derivatives = DerivativeBuilder(config, repositories.derivatives)
        self.linker = SidecarLinker(
            repositories.assets,
            enabled=config.scan.detect_sidecars,
            detect_motion=config.scan.detect_motion_photos,
        )

    # ── pool ────────────────────────────────────────────────────────────────

    def _make_pool(self, workers: int, name: str) -> ThreadPoolExecutor:
        """The single place the executor type is chosen. See the module note."""
        return ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix=name)

    # ── entry points ────────────────────────────────────────────────────────

    def scan(
        self,
        roots: list[Path | str] | None = None,
        *,
        resume: bool = True,
        rehash: bool = False,
        generate_derivatives: bool = True,
    ) -> list[ScanResult]:
        """Scan every configured root, or the ones given."""
        targets = [Path(r) for r in (roots or self.config.library.roots)]
        if not targets:
            raise MediaEngineError(
                "no library roots configured; set library.roots or pass a path to scan"
            )
        results: list[ScanResult] = []
        try:
            for root in targets:
                results.append(
                    self.scan_root(
                        root,
                        resume=resume,
                        rehash=rehash,
                        generate_derivatives=generate_derivatives,
                    )
                )
                if self.cancel.cancelled:
                    break
        finally:
            self.close()
        return results

    def scan_root(
        self,
        root: Path | str,
        *,
        resume: bool = True,
        rehash: bool = False,
        generate_derivatives: bool = True,
    ) -> ScanResult:
        """Scan one directory tree end to end."""
        root = Path(root).resolve()
        started = time.monotonic()
        started_at = utcnow_iso()

        resume_cursor, scan_id = self._resolve_session(root, resume=resume)
        result = ScanResult(scan_id=scan_id, root=str(root), started_at=started_at)

        walker = Walker(
            self.config.library,
            cancel=self.cancel,
            on_error=lambda path, exc: self._record_error("scan", path, exc),
            # Our own files are never library content, even when an operator
            # has pointed the database at a folder inside a media root. The
            # database file and the cache directory, not the folder holding
            # them: someone may reasonably keep the index beside their photos.
            never_descend=(
                *(
                    Path(str(self.config.storage.db_path) + suffix)
                    for suffix in ("", "-wal", "-shm", "-journal")
                ),
                self.config.storage.derivatives_path,
            ),
        )
        producer = BatchProducer(
            walker,
            [root],
            batch_size=self.config.scan.batch_size,
            queue_size=self.config.workers.walk_queue_size,
            resume_after=resume_cursor,
            cancel=self.cancel,
        )

        read_pool = self._make_pool(self.config.workers.resolved_metadata_processes(), "me-read")
        derive_pool = self._make_pool(
            self.config.workers.resolved_derivative_workers(), "me-derive"
        )
        try:
            producer.start()
            for batch in producer:
                if self.cancel.cancelled:
                    result.cancelled = True
                    break
                self._process_batch(
                    batch,
                    result,
                    read_pool=read_pool,
                    derive_pool=derive_pool,
                    rehash=rehash,
                    generate_derivatives=generate_derivatives,
                )
                self._checkpoint(scan_id, batch, result)
        except OperationCancelled:
            result.cancelled = True
        finally:
            producer.stop()
            read_pool.shutdown(wait=True)
            derive_pool.shutdown(wait=True)
            self.linker.finish()

        if self.config.scan.rescan_missing and not result.cancelled:
            result.missing_marked = self.repos.assets.mark_missing_under(str(root), started_at)

        result.walk = walker.stats.as_dict()
        result.links = self.linker.stats.as_dict()
        result.duration_s = time.monotonic() - started
        result.finished_at = utcnow_iso()

        self.repos.tasks.update_scan(
            scan_id,
            state=ScanState.CANCELLED if result.cancelled else ScanState.FINISHED,
            finished=True,
        )
        self.reporter.flush(
            "scan",
            message=f"{result.files_new} new, {result.files_updated} updated, "
            f"{result.files_skipped} unchanged",
        )
        return result

    # ── session bookkeeping ─────────────────────────────────────────────────

    def _resolve_session(self, root: Path, *, resume: bool) -> tuple[str | None, int]:
        """Find a crashed session to continue, or open a fresh one.

        A session left in ``running`` means the process died mid-scan. Picking
        it up and continuing from its cursor is what turns ``kill -9`` from
        "start over" into "carry on".
        """
        if resume:
            for session in self.repos.tasks.resumable_scans():
                if str(session.get("root_path")) != str(root):
                    continue
                cursor = session.get("cursor")
                scan_id = int(session["id"])
                if cursor:
                    _LOG.info("resuming scan %d of %s after %s", scan_id, root, cursor)
                else:
                    _LOG.info("restarting interrupted scan %d of %s", scan_id, root)
                return (str(cursor) if cursor else None), scan_id

        scan_id = self.repos.tasks.create_scan(
            str(root),
            options={
                "include": self.config.library.include,
                "exclude": self.config.library.exclude,
                "hash_algorithm": self.config.scan.hash_algorithm,
                "follow_symlinks": self.config.library.follow_symlinks,
            },
        )
        return None, scan_id

    def _checkpoint(self, scan_id: int, batch: WalkBatch, result: ScanResult) -> None:
        """Persist progress and the resume cursor after a completed batch."""
        self.repos.tasks.update_scan(
            scan_id,
            files_seen=len(batch),
            cursor=batch.cursor,
        )

    # ── the batch ───────────────────────────────────────────────────────────

    def _process_batch(
        self,
        batch: WalkBatch,
        result: ScanResult,
        *,
        read_pool: ThreadPoolExecutor,
        derive_pool: ThreadPoolExecutor,
        rehash: bool,
        generate_derivatives: bool,
    ) -> None:
        if not batch.entries:
            return

        # Stage 1 — triage. One indexed query for the whole batch; this is the
        # query that has to keep a 200k-file re-scan in the seconds range.
        known = self.repos.assets.triage_batch([e.key for e in batch.entries])
        pending: list[WalkEntry] = []
        unchanged_ids: list[int] = []

        for entry in batch.entries:
            row = known.get(entry.key)
            if row is None:
                pending.append(entry)
                continue
            if not rehash and self._unchanged(row, entry):
                unchanged_ids.append(int(row["id"]))
                continue
            pending.append(entry)

        if unchanged_ids:
            self.repos.assets.touch_files(unchanged_ids)
            result.files_skipped += len(unchanged_ids)
            self.reporter.advance("triage", by=len(unchanged_ids))

        result.files_seen += len(batch.entries)
        if not pending:
            return

        # Stage 2 — identify and extract, in parallel. Pre-warm the ExifTool
        # daemon with the whole batch: one exchange for sixty files instead of
        # sixty spawns, which is the entire reason the daemon exists.
        exif_records = self._prefetch_exif(pending)

        futures: list[Future[IngestItem | None]] = [
            read_pool.submit(self._read_one, entry, exif_records.get(entry.key), known)
            for entry in pending
        ]
        items: list[IngestItem] = []
        for future in futures:
            try:
                item = future.result()
            except OperationCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - per-file isolation
                result.files_failed += 1
                _LOG.warning("ingest failed: %s", exc)
                continue
            if item is not None:
                items.append(item)
        self.reporter.advance("identify", by=len(items))

        if not items:
            return

        # Stage 3 — persist. One transaction for the batch: a crash costs at
        # most this batch, and re-doing it is a no-op because identity is the
        # content hash.
        self._persist_batch(items, result)

        # Stage 4 — companions. Cheap, and needs the asset ids from stage 3.
        for item in items:
            self.linker.add(
                SidecarCandidate(
                    path=item.entry.path,
                    asset_id=item.asset_id,
                    media_type=item.media_type,
                    is_raw=item.is_raw,
                    extension=item.entry.path.suffix.lower().lstrip("."),
                )
            )

        # Stage 5 — derivatives, in parallel, after the ids exist.
        if generate_derivatives:
            self._build_derivatives(items, result, derive_pool)

        # Stage 6 — search index.
        self._index_batch(items)
        self.reporter.advance("index", by=len(items))

    @staticmethod
    def _unchanged(row: sqlite3.Row, entry: WalkEntry) -> bool:
        """The triage comparison. Any difference means re-read the bytes."""
        if row["size_bytes"] != entry.stat.st_size:
            return False
        if row["mtime_ns"] != entry.stat.st_mtime_ns:
            return False
        recorded_inode = row["inode"]
        if recorded_inode not in (None, 0) and entry.stat.st_ino:
            if recorded_inode != entry.stat.st_ino:
                return False
        return str(row["status"]) == FileStatus.PRESENT.value

    def _prefetch_exif(self, entries: list[WalkEntry]) -> dict[str, dict[str, Any]]:
        """Batch ExifTool over the images in this batch, if it is installed."""
        images = [
            e.key
            for e in entries
            if e.path.suffix.lower().lstrip(".")
            not in ("mp4", "mov", "mkv", "avi", "wav", "mp3", "flac", "pdf", "txt")
        ]
        return self.extractor.prefetch_exif(images) if images else {}

    # ── per-file read work ──────────────────────────────────────────────────

    def _read_one(
        self,
        entry: WalkEntry,
        exif_record: dict[str, Any] | None,
        known: dict[str, sqlite3.Row],
    ) -> IngestItem | None:
        """Hash, type, perceptually hash and extract one file. No database."""
        self.cancel.raise_if_cancelled()
        try:
            hashed = hash_file(
                entry.path,
                algorithm=self.config.scan.hash_algorithm,
                chunk_size=self.config.scan.hash_chunk_size,
                sample_above_bytes=self.config.scan.hash_sample_above_bytes,
            )
        except CorruptMedia as exc:
            self._record_error("file", entry.key, exc)
            return None

        detection = detect_media_type(entry.path, head=hashed.head)
        extracted = self.extractor.extract(
            entry.path,
            detection,
            exif_record=exif_record,
            file_mtime=entry.stat.st_mtime,
        )

        # ffprobe is the authority on whether an ISO-BMFF file is video or
        # audio; the sniffer only saw the brand.
        corrected = extracted.payload.pop("media_type", None)
        media_type = MediaType(corrected) if corrected else detection.media_type

        perceptual: str | None = None
        if self.config.scan.compute_perceptual_hash and media_type is MediaType.IMAGE:
            perceptual = perceptual_hash(entry.path)

        motion_offset: int | None = None
        if self.config.scan.detect_motion_photos and media_type is MediaType.IMAGE:
            motion_offset = find_embedded_motion(
                entry.path, xmp=self._xmp_bytes(extracted), file_size=hashed.size_bytes
            )
            if motion_offset is not None:
                extracted.raw.update(self.linker.note_embedded_motion(0, motion_offset))

        row = known.get(entry.key)
        for warning in extracted.warnings:
            _LOG.debug("%s: %s", entry.key, warning)

        return IngestItem(
            entry=entry,
            hashed=hashed,
            media_type=media_type,
            mime_type=detection.mime_type,
            is_raw=detection.is_raw,
            container=detection.container,
            extracted=extracted,
            perceptual=perceptual,
            known_file_id=int(row["id"]) if row is not None else None,
            was_known_path=row is not None,
            motion_offset=motion_offset,
        )

    @staticmethod
    def _xmp_bytes(extracted: Extracted) -> bytes | None:
        """The raw XMP packet, when an extractor captured one."""
        raw = extracted.raw.get("exiftool") or {}
        for key in ("XMP:MotionPhoto", "XMP:MicroVideoOffset"):
            if key in raw:
                return json_dumps(raw).encode("utf-8")
        return None

    # ── writes ──────────────────────────────────────────────────────────────

    def _persist_batch(self, items: list[IngestItem], result: ScanResult) -> None:
        """Write a whole batch inside one transaction.

        Every repository call is passed the caller's ``conn``, which is the
        convention that lets independent repositories compose atomically —
        asset, file, technical metadata, location and document text either all
        land for a file or none of them do.
        """

        def _op(conn: sqlite3.Connection) -> None:
            for item in items:
                self._persist_one(conn, item, result)

        self.repos.db.writer.run(_op, label="ingest_batch")

    def _persist_one(
        self, conn: sqlite3.Connection, item: IngestItem, result: ScanResult
    ) -> None:
        extracted = item.extracted
        asset_id, created = self.repos.assets.upsert_asset(
            content_hash=item.hashed.content_hash,
            media_type=item.media_type,
            size_bytes=item.hashed.size_bytes,
            mime_type=item.mime_type,
            hash_algorithm=item.hashed.algorithm,
            perceptual_hash=item.perceptual,
            captured_at=extracted.captured_at,
            captured_at_tz=extracted.captured_at_tz,
            captured_at_source=extracted.captured_at_source,
            conn=conn,
        )
        item.asset_id = asset_id
        item.asset_created = created

        self.repos.assets.upsert_file(
            asset_id=asset_id,
            path=item.entry.key,
            stat=item.entry.stat,
            status=FileStatus.PRESENT,
            conn=conn,
        )

        promoted = extracted.promoted()
        if promoted or extracted.raw:
            self.repos.assets.set_technical_metadata(
                asset_id,
                promoted,
                raw=extracted.raw,
                extractor=extracted.extractor or None,
                conn=conn,
            )

        if extracted.gps is not None:
            self.repos.places.set_location(
                asset_id,
                extracted.gps.latitude,
                extracted.gps.longitude,
                source=LocationSource.EXIF,
                altitude_m=extracted.gps.altitude_m,
                accuracy_m=extracted.gps.accuracy_m,
                heading_deg=extracted.gps.heading_deg,
                recorded_at=extracted.gps.recorded_at,
                conn=conn,
            )

        if extracted.text is not None or extracted.ocr_eligible:
            self.repos.assets.set_document_text(
                asset_id,
                extracted.text or "",
                truncated=extracted.text_truncated,
                ocr_eligible=extracted.ocr_eligible,
                extractor=extracted.extractor or None,
                conn=conn,
            )

        if created:
            result.assets_created += 1
        if item.was_known_path:
            result.files_updated += 1
        else:
            result.files_new += 1
        result.bytes_hashed += item.hashed.size_bytes

    def _build_derivatives(
        self, items: list[IngestItem], result: ScanResult, pool: ThreadPoolExecutor
    ) -> None:
        futures = [
            pool.submit(
                self.derivatives.build,
                asset_id=item.asset_id,
                content_hash=item.hashed.content_hash,
                source=item.entry.path,
                media_type=item.media_type,
                mime_type=item.mime_type,
                duration_s=item.extracted.payload.get("duration_s"),
                is_raw=item.is_raw,
            )
            for item in items
            if item.asset_id
        ]
        for future in futures:
            try:
                built = future.result()
            except OperationCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("derivative pass failed: %s", exc)
                continue
            result.derivatives_written += built.count - built.reused
            result.derivative_bytes += built.bytes_written
            for warning in built.warnings[:3]:
                if warning not in result.warnings:
                    result.warnings.append(warning)
        self.reporter.advance("derive", by=len(futures))

    def _index_batch(self, items: list[IngestItem]) -> None:
        """Write the FTS content rows for a freshly ingested batch.

        Only filename and document text are known at scan time — labels, tags,
        people and place names arrive when plugins run, and
        :meth:`reindex_asset` folds them in then. Writing an empty column now
        and filling it later is correct because the FTS triggers rebuild the
        term index on every update.
        """

        def _op(conn: sqlite3.Connection) -> None:
            for item in items:
                if not item.asset_id:
                    continue
                text = item.extracted.text or ""
                self.repos.search_docs.upsert(
                    item.asset_id,
                    filename=expand_filename(item.entry.path.name),
                    doc_text=text[:200_000],
                    conn=conn,
                )
            self.repos.assets.mark_indexed(
                [i.asset_id for i in items if i.asset_id], conn=conn
            )

        self.repos.db.writer.run(_op, label="index_batch")

    # ── single-asset operations ─────────────────────────────────────────────

    def reindex_asset(self, asset_id: int) -> bool:
        """Rebuild one asset's search row from everything now known about it.

        Called after a plugin commits. Reads happen first, outside the write
        transaction, because a reader connection cannot see the writer's
        uncommitted rows.
        """
        asset = self.repos.assets.get_asset(asset_id)
        if asset is None:
            return False
        path = self.repos.assets.primary_path(asset_id)
        payload = {
            "filename": expand_filename(Path(path).name) if path else "",
            "tags": self.repos.tags.tag_names_for_asset(asset_id),
            "labels": self.repos.annotations.labels_for_asset(asset_id),
            "doc_text": (self.repos.assets.get_document_text(asset_id) or "")[:200_000],
            "place_names": self.repos.places.place_names_for_asset(asset_id),
            "people": self.repos.identities.people_for_asset(asset_id),
        }

        def _op(conn: sqlite3.Connection) -> None:
            self.repos.search_docs.upsert(asset_id, conn=conn, **payload)
            self.repos.assets.mark_indexed([asset_id], conn=conn)

        self.repos.db.writer.run(_op, label="reindex_asset")
        return True

    def rebuild_derivatives(self, asset_id: int, *, overwrite: bool = True) -> int:
        """Regenerate one asset's derivatives. Used after a cache purge."""
        asset = self.repos.assets.get_asset(asset_id)
        path = self.repos.assets.primary_path(asset_id)
        if asset is None or path is None:
            return 0
        technical = self.repos.assets.get_technical_metadata(asset_id) or {}
        built = self.derivatives.build(
            asset_id=asset_id,
            content_hash=str(asset["content_hash"]),
            source=Path(path),
            media_type=MediaType(str(asset["media_type"])),
            mime_type=asset.get("mime_type"),
            duration_s=technical.get("duration_s"),
            is_raw=str(asset.get("mime_type") or "").startswith("image/x-"),
            overwrite=overwrite,
        )
        return built.count

    # ── errors and teardown ─────────────────────────────────────────────────

    def _record_error(self, scope: str, reference: str, exc: BaseException) -> None:
        self.repos.tasks.record_error(
            type(exc).__name__,
            str(exc),
            scope=scope,
            ref_text=reference,
            traceback="".join(traceback.format_exception(exc))[:16000],
        )

    def close(self) -> None:
        """Release the ExifTool daemon. Idempotent."""
        self.extractor.close()

    def __enter__(self) -> "IngestPipeline":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def capabilities(self) -> dict[str, Any]:
        """Which optional tools this install can use."""
        from .identity import blake3_available

        caps: dict[str, Any] = dict(self.extractor.capabilities())
        caps["blake3"] = blake3_available()
        caps["cpu_count"] = os.cpu_count() or 1
        return caps
