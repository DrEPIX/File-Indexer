"""The single writer thread.

Every mutation in the engine is a callable of the form ``fn(conn) -> T``
handed to :meth:`Writer.submit`. The writer thread pulls those off a bounded
queue and runs them one at a time against the one connection permitted to
write. Callers get a :class:`concurrent.futures.Future`; blocking on it is
optional, which is what lets the ingest pipeline fire-and-forget most of its
writes and only wait where it needs the generated row id.

Two flavours of job:

* :meth:`submit` / :meth:`run` — wrapped in ``BEGIN IMMEDIATE`` … ``COMMIT``.
  Use for ordinary data changes.
* :meth:`submit_raw` / :meth:`run_raw` — no transaction wrapper. Use for
  statements that cannot run inside one (``VACUUM``, ``executescript``,
  ``PRAGMA wal_checkpoint``).

Bounded queue means backpressure: if writes fall behind, producers block
rather than the process growing until the OOM killer intervenes.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from ..errors import DatabaseLockedError, WriterClosedError

__all__ = ["Writer", "WriterStats"]

_LOG = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE = ("database is locked", "database table is locked", "cannot start a transaction")


@dataclass
class WriterStats:
    """Counters exposed for the ``/api/admin/stats`` endpoint and the CLI."""

    submitted: int = 0
    completed: int = 0
    failed: int = 0
    retried: int = 0
    queue_high_water: int = 0
    total_wait_s: float = 0.0
    total_exec_s: float = 0.0

    def snapshot(self) -> dict[str, float | int]:
        return {
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "retried": self.retried,
            "queue_high_water": self.queue_high_water,
            "total_wait_s": round(self.total_wait_s, 4),
            "total_exec_s": round(self.total_exec_s, 4),
        }


@dataclass
class _Job:
    fn: Callable[[sqlite3.Connection], Any]
    future: Future[Any]
    transactional: bool
    submitted_at: float = field(default_factory=time.monotonic)
    label: str = ""


class Writer:
    """Serialises all database writes onto one thread.

    :param conn: the sole read-write connection. The writer takes ownership of
        it for its lifetime but does not close it; :class:`Database` does.
    :param queue_size: bounded queue depth. Submitters block when full.
    :param max_retries: attempts for transient ``database is locked`` errors,
        which should be rare given there is only one writer, but can still
        occur when an external process (a backup tool, ``sqlite3`` CLI) holds
        the file.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        queue_size: int = 10_000,
        max_retries: int = 5,
        retry_base_delay_s: float = 0.05,
        db_path: Path | None = None,
    ) -> None:
        self._conn = conn
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._stopping = threading.Event()
        self._max_retries = max_retries
        self._retry_base = retry_base_delay_s
        self._db_path = db_path
        self.stats = WriterStats()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the writer thread. Idempotent."""
        if self._thread is not None:
            return
        self._accepting = True
        self._thread = threading.Thread(target=self._loop, name="me-writer", daemon=True)
        self._thread.start()

    def stop(self, *, drain: bool = True, checkpoint: bool = True, timeout: float = 30.0) -> None:
        """Stop accepting work and shut the thread down.

        With ``drain=True`` (the default) every already-queued job runs before
        the thread exits, so a graceful shutdown never loses a write that was
        accepted. With ``drain=False`` pending jobs have their futures failed
        with :class:`WriterClosedError`.
        """
        if self._thread is None:
            return
        self._accepting = False
        if not drain:
            self._stopping.set()
            self._fail_pending()
        self._queue.put(None)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():  # pragma: no cover - shutdown pathology
            _LOG.warning("writer thread did not exit within %.1fs", timeout)
        self._thread = None
        if checkpoint:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.Error as exc:
                _LOG.warning("WAL checkpoint on shutdown failed: %s", exc)

    def _fail_pending(self) -> None:
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            if job is None:
                continue
            if not job.future.done():
                job.future.set_exception(WriterClosedError("writer stopped before job ran"))
            self._queue.task_done()

    # ── submission ──────────────────────────────────────────────────────────

    def submit(
        self,
        fn: Callable[[sqlite3.Connection], T],
        *,
        label: str = "",
        timeout: float | None = None,
    ) -> Future[T]:
        """Queue ``fn`` to run inside a transaction. Returns immediately."""
        return self._enqueue(fn, transactional=True, label=label, timeout=timeout)

    def submit_raw(
        self,
        fn: Callable[[sqlite3.Connection], T],
        *,
        label: str = "",
        timeout: float | None = None,
    ) -> Future[T]:
        """Queue ``fn`` with no transaction wrapper."""
        return self._enqueue(fn, transactional=False, label=label, timeout=timeout)

    def run(self, fn: Callable[[sqlite3.Connection], T], *, label: str = "") -> T:
        """Queue ``fn`` in a transaction and block for its result."""
        return self.submit(fn, label=label).result()

    def run_raw(self, fn: Callable[[sqlite3.Connection], T], *, label: str = "") -> T:
        """Queue ``fn`` without a transaction and block for its result."""
        return self.submit_raw(fn, label=label).result()

    def _enqueue(
        self,
        fn: Callable[[sqlite3.Connection], T],
        *,
        transactional: bool,
        label: str,
        timeout: float | None,
    ) -> Future[T]:
        if not self._accepting:
            raise WriterClosedError("writer is not accepting work")
        future: Future[T] = Future()
        job = _Job(fn=fn, future=future, transactional=transactional, label=label)
        try:
            self._queue.put(job, timeout=timeout)
        except queue.Full as exc:
            raise WriterClosedError(f"writer queue full after {timeout}s") from exc
        self.stats.submitted += 1
        depth = self._queue.qsize()
        if depth > self.stats.queue_high_water:
            self.stats.queue_high_water = depth
        return future

    # ── batching helper ─────────────────────────────────────────────────────

    def run_batch(
        self, fns: Sequence[Callable[[sqlite3.Connection], Any]], *, label: str = "batch"
    ) -> list[Any]:
        """Run several callables inside **one** transaction.

        Either every callable commits or none does. Used by operations the
        spec requires to be atomic, such as ``purge_biometrics``.
        """

        def _all(conn: sqlite3.Connection) -> list[Any]:
            return [fn(conn) for fn in fns]

        return self.run(_all, label=label)

    # ── the loop ────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                self._queue.task_done()
                if not self._accepting:
                    return
                continue
            try:
                self._execute(job)
            finally:
                self._queue.task_done()

    def _execute(self, job: _Job) -> None:
        if job.future.set_running_or_notify_cancel() is False:
            return
        self.stats.total_wait_s += time.monotonic() - job.submitted_at
        started = time.monotonic()
        attempt = 0
        while True:
            try:
                result = self._run_once(job)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if attempt < self._max_retries and any(r in message for r in _RETRYABLE):
                    attempt += 1
                    self.stats.retried += 1
                    delay = self._retry_base * (2 ** (attempt - 1))
                    _LOG.debug("write retry %d after %s (sleeping %.3fs)", attempt, exc, delay)
                    time.sleep(delay)
                    continue
                self.stats.failed += 1
                self.stats.total_exec_s += time.monotonic() - started
                wrapped: BaseException = (
                    DatabaseLockedError(str(exc)) if "locked" in message else exc
                )
                _LOG.error("write job %r failed: %s", job.label or job.fn, exc)
                job.future.set_exception(wrapped)
                return
            except BaseException as exc:  # noqa: BLE001 - futures carry anything
                self.stats.failed += 1
                self.stats.total_exec_s += time.monotonic() - started
                _LOG.error("write job %r failed: %s", job.label or job.fn, exc, exc_info=True)
                job.future.set_exception(exc)
                return
            else:
                self.stats.completed += 1
                self.stats.total_exec_s += time.monotonic() - started
                job.future.set_result(result)
                return

    def _run_once(self, job: _Job) -> Any:
        if not job.transactional:
            return job.fn(self._conn)
        # BEGIN IMMEDIATE takes the write lock up front rather than on the
        # first write statement, which turns a mid-transaction SQLITE_BUSY
        # into an immediate, retryable failure.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = job.fn(self._conn)
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - rollback of a dead txn
                pass
            raise
        self._conn.execute("COMMIT")
        return result

    # ── introspection ───────────────────────────────────────────────────────

    @property
    def depth(self) -> int:
        """Jobs currently queued."""
        return self._queue.qsize()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until the queue drains. Returns False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.005)
        return True
