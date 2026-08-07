"""Filesystem traversal: the front of the ingest pipeline.

Three properties matter more than speed here, though it is also fast:

**It never materialises the library.** A 200k-file tree is emitted as batches
through a bounded queue. When the consumer falls behind, the walker blocks.
The alternative — a list of 200k paths plus their stat results — is hundreds of
megabytes held for the duration of the scan, and it converts a slow scan into
an out-of-memory crash.

**It is resumable.** Traversal order is fully deterministic: depth-first,
children sorted by name. That makes "the last directory I finished" a valid
resume point. On restart the walker replays the traversal, emitting nothing,
until it passes that directory. Replaying is cheap — it is ``scandir`` calls
with no file opens — and it is correct in a way that a path-prefix comparison
is not, because a directory's subtree is visited *after* the directory itself.

**It cannot be trapped.** Symlink loops are broken by device/inode tracking,
descent stops at ``max_depth``, and a directory the process cannot read is
counted and skipped rather than aborting the scan.
"""

from __future__ import annotations

import logging
import os
import queue
import stat as stat_module
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ..config import LibraryConfig
from .control import CancelToken
from .globs import GlobMatcher, to_relative_posix

__all__ = ["WalkEntry", "WalkBatch", "WalkStats", "Walker", "BatchProducer"]

_LOG = logging.getLogger(__name__)

_IS_WINDOWS: Final[bool] = os.name == "nt"

#: Reported when a path cannot be walked. ``(path, exception)``.
WalkErrorHandler = Callable[[str, OSError], None]


@dataclass(frozen=True, slots=True)
class WalkEntry:
    """One candidate file, with the stat the walker already paid for.

    ``scandir`` caches stat data on Windows and on Linux for most filesystems,
    so carrying it forward saves the triage stage a second syscall per file.
    """

    path: Path
    stat: os.stat_result
    root: Path

    @property
    def size(self) -> int:
        return self.stat.st_size

    @property
    def key(self) -> str:
        """The exact string stored in ``files.path``."""
        return str(self.path)


@dataclass(frozen=True, slots=True)
class WalkBatch:
    """A batch of candidates plus the resume point it makes safe."""

    entries: list[WalkEntry]

    cursor: str | None
    """The last directory *fully emitted* at or before the end of this batch.

    Persisting this after the batch is processed is what makes a scan resumable.
    It is deliberately conservative: a directory whose files straddle a batch
    boundary is not reported complete until the batch containing its last file
    has been yielded.
    """

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(slots=True)
class WalkStats:
    """Counters an operator actually asks about after a scan."""

    directories: int = 0
    files_seen: int = 0
    files_matched: int = 0
    bytes_matched: int = 0
    skipped_excluded: int = 0
    skipped_hidden: int = 0
    skipped_small: int = 0
    skipped_symlink: int = 0
    skipped_resume: int = 0
    errors: int = 0
    error_paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "directories": self.directories,
            "files_seen": self.files_seen,
            "files_matched": self.files_matched,
            "bytes_matched": self.bytes_matched,
            "skipped_excluded": self.skipped_excluded,
            "skipped_hidden": self.skipped_hidden,
            "skipped_small": self.skipped_small,
            "skipped_symlink": self.skipped_symlink,
            "skipped_resume": self.skipped_resume,
            "errors": self.errors,
        }


def _is_hidden(entry: os.DirEntry[str], name: str) -> bool:
    """Dotfile on every platform; also the hidden attribute on Windows.

    A user who has hidden a folder in Explorer means it, and NTFS records that
    as an attribute rather than a leading dot.
    """
    if name.startswith("."):
        return True
    if not _IS_WINDOWS:
        return False
    try:
        attributes = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    return bool(attributes & stat_module.FILE_ATTRIBUTE_HIDDEN)  # type: ignore[attr-defined]


class Walker:
    """Produces candidate files for one or more library roots."""

    def __init__(
        self,
        config: LibraryConfig,
        *,
        on_error: WalkErrorHandler | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self.config = config
        self.matcher = GlobMatcher(config.include, config.exclude)
        self.stats = WalkStats()
        self._on_error = on_error
        self._cancel = CancelToken() if cancel is None else cancel
        self._last_completed: str | None = None

    # ── traversal ───────────────────────────────────────────────────────────

    def iter_entries(self, root: Path | str, *, resume_after: str | None = None) -> Iterator[WalkEntry]:
        """Yield every matching file under ``root`` in deterministic order.

        Directory boundaries are signalled out of band via
        :attr:`last_completed_directory`; use :meth:`iter_batches` if you need
        the resume cursor, which almost every caller does.
        """
        for entry, _ in self._walk(Path(root), resume_after=resume_after):
            yield entry

    def iter_batches(
        self,
        root: Path | str,
        *,
        batch_size: int = 256,
        resume_after: str | None = None,
    ) -> Iterator[WalkBatch]:
        """Yield batches of candidates, each carrying a safe resume cursor."""
        buffer: list[WalkEntry] = []
        completed: str | None = None
        emitted: str | None = None
        for entry, finished_dir in self._walk(Path(root), resume_after=resume_after):
            buffer.append(entry)
            if finished_dir is not None:
                completed = finished_dir
            if len(buffer) >= batch_size:
                yield WalkBatch(entries=buffer, cursor=completed)
                emitted = completed
                buffer = []
        # Directories emptied by the filters, and the last directory of the
        # tree, finish with no entry left to carry their cursor. A trailing
        # empty batch is how the final resume point reaches the caller — worth
        # it so that re-running a completed scan resumes at the end rather than
        # re-walking the whole tree.
        tail = self._last_completed or completed
        if buffer:
            yield WalkBatch(entries=buffer, cursor=tail)
        elif tail is not None and tail != emitted:
            yield WalkBatch(entries=[], cursor=tail)

    def _walk(
        self, root: Path, *, resume_after: str | None
    ) -> Iterator[tuple[WalkEntry, str | None]]:
        """Depth-first traversal yielding ``(entry, directory_just_completed)``.

        ``directory_just_completed`` is set on the *last* entry emitted from a
        directory, which is what lets the batcher attach a conservative cursor.
        """
        root = root.resolve()
        if not root.is_dir():
            self._error(str(root), NotADirectoryError(f"{root} is not a directory"))
            return

        try:
            root_device = root.stat().st_dev
        except OSError as exc:
            self._error(str(root), exc)
            return

        skipping = resume_after is not None
        seen_dirs: set[tuple[int, int]] = set()
        # (path, depth); a stack rather than recursion so a pathologically deep
        # tree costs memory instead of a RecursionError.
        stack: list[tuple[Path, int]] = [(root, 0)]
        self._last_completed = None

        while stack:
            self._cancel.raise_if_cancelled()
            directory, depth = stack.pop()

            children = self._scan_directory(directory, root, root_device, depth, seen_dirs)
            if children is None:
                continue
            subdirs, files = children
            self.stats.directories += 1

            if skipping:
                self.stats.skipped_resume += len(files)
                if str(directory) == resume_after:
                    skipping = False
                    _LOG.info("resuming scan after %s", directory)
            else:
                for index, entry in enumerate(files):
                    is_last = index == len(files) - 1
                    self._last_completed = str(directory)
                    yield entry, (str(directory) if is_last else None)
                if not files:
                    # An empty (or fully filtered) directory is still complete,
                    # and skipping it on resume is valid.
                    self._last_completed = str(directory)

            # Reversed so the sorted children pop in order, keeping traversal
            # deterministic — the whole resume mechanism depends on that.
            stack.extend((child, depth + 1) for child in reversed(subdirs))

        if skipping:
            # The cursor directory no longer exists. Rather than silently index
            # nothing, start over: a re-scan is idempotent and cheap thanks to
            # the triage fast path, whereas a scan that finds zero files looks
            # like success and is not.
            _LOG.warning(
                "resume cursor %s no longer exists; restarting walk of %s", resume_after, root
            )
            self.stats.skipped_resume = 0
            yield from self._walk(root, resume_after=None)

    def _scan_directory(
        self,
        directory: Path,
        root: Path,
        root_device: int,
        depth: int,
        seen_dirs: set[tuple[int, int]],
    ) -> tuple[list[Path], list[WalkEntry]] | None:
        """One directory's children, split into descendable dirs and candidates."""
        try:
            with os.scandir(directory) as scanner:
                raw = sorted(scanner, key=lambda e: e.name)
        except OSError as exc:
            self._error(str(directory), exc)
            return None

        subdirs: list[Path] = []
        files: list[WalkEntry] = []
        max_depth = self.config.max_depth

        for entry in raw:
            self._cancel.raise_if_cancelled()
            child = Path(entry.path)
            try:
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=self.config.follow_symlinks)
            except OSError as exc:
                self._error(entry.path, exc)
                continue

            if is_symlink and not self.config.follow_symlinks:
                self.stats.skipped_symlink += 1
                continue

            if not self.config.include_hidden and _is_hidden(entry, entry.name):
                self.stats.skipped_hidden += 1
                continue

            relative = to_relative_posix(child, root)

            if is_dir:
                if max_depth is not None and depth >= max_depth:
                    continue
                if self.matcher.excludes_dir(relative):
                    self.stats.skipped_excluded += 1
                    continue
                if not self._may_descend(entry, child, root_device, seen_dirs):
                    continue
                subdirs.append(child)
                continue

            try:
                if not entry.is_file(follow_symlinks=self.config.follow_symlinks):
                    continue
                info = entry.stat(follow_symlinks=self.config.follow_symlinks)
            except OSError as exc:
                self._error(entry.path, exc)
                continue

            self.stats.files_seen += 1

            if not self.matcher.matches_file(relative):
                self.stats.skipped_excluded += 1
                continue
            if info.st_size < self.config.min_file_size:
                self.stats.skipped_small += 1
                continue

            self.stats.files_matched += 1
            self.stats.bytes_matched += info.st_size
            files.append(WalkEntry(path=child, stat=info, root=root))

        return subdirs, files

    def _may_descend(
        self,
        entry: os.DirEntry[str],
        child: Path,
        root_device: int,
        seen_dirs: set[tuple[int, int]],
    ) -> bool:
        """Filesystem-boundary and symlink-loop checks."""
        try:
            info = entry.stat(follow_symlinks=True)
        except OSError as exc:
            self._error(entry.path, exc)
            return False

        if not self.config.cross_filesystem and info.st_dev != root_device:
            _LOG.debug("not crossing filesystem boundary at %s", child)
            return False

        if self.config.follow_symlinks:
            # Only needed when following links: without them a tree cannot
            # contain a cycle. st_ino is 0 on some Windows filesystems, in
            # which case fall back to the resolved path.
            identity = (info.st_dev, info.st_ino) if info.st_ino else (0, hash(str(child.resolve())))
            if identity in seen_dirs:
                _LOG.warning("symlink loop detected at %s; not descending", child)
                return False
            seen_dirs.add(identity)
        return True

    def _error(self, path: str, exc: OSError) -> None:
        self.stats.errors += 1
        if len(self.stats.error_paths) < 100:
            self.stats.error_paths.append(path)
        _LOG.warning("walk error at %s: %s", path, exc)
        if self._on_error is not None:
            self._on_error(path, exc)


class BatchProducer:
    """Runs a :class:`Walker` on its own thread, feeding a bounded queue.

    This is the backpressure boundary of the whole ingest pipeline. The queue
    holds *batches*, not paths, so ``walk_queue_size`` is measured in units the
    operator can reason about; when the hashing and extraction stages fall
    behind, ``queue.put`` blocks and the walker simply stops reading
    directories. Memory use is therefore bounded by
    ``walk_queue_size × batch_size`` entries regardless of library size.
    """

    __slots__ = ("_walker", "_roots", "_batch_size", "_queue", "_thread", "_error", "_cancel", "_resume")

    def __init__(
        self,
        walker: Walker,
        roots: Sequence[Path | str],
        *,
        batch_size: int = 256,
        queue_size: int = 64,
        resume_after: str | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self._walker = walker
        self._roots = [Path(r) for r in roots]
        self._batch_size = batch_size
        self._queue: queue.Queue[WalkBatch | None] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._cancel = CancelToken() if cancel is None else cancel
        self._resume = resume_after

    def start(self) -> "BatchProducer":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="me-walker", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            for root in self._roots:
                for batch in self._walker.iter_batches(
                    root, batch_size=self._batch_size, resume_after=self._resume
                ):
                    if self._cancel.cancelled:
                        return
                    self._queue.put(batch)
                # The resume cursor belongs to the root it came from; later
                # roots start from the beginning.
                self._resume = None
        except BaseException as exc:  # noqa: BLE001 - re-raised in the consumer
            self._error = exc
        finally:
            self._queue.put(None)

    def __iter__(self) -> Iterator[WalkBatch]:
        """Consume batches until the walker finishes, re-raising its failure."""
        if self._thread is None:
            self.start()
        while True:
            batch = self._queue.get()
            if batch is None:
                break
            yield batch
        if self._error is not None:
            raise self._error

    def stop(self, *, timeout: float = 5.0) -> None:
        """Cancel the walk and join the thread. Safe to call twice."""
        self._cancel.cancel("walker stopped")
        thread = self._thread
        if thread is None:
            return
        # Drain so a walker blocked on a full queue can notice the cancel.
        while thread.is_alive():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        thread.join(timeout=timeout)
        self._thread = None

    @property
    def stats(self) -> WalkStats:
        return self._walker.stats
