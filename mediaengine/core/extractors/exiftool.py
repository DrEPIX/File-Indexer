"""ExifTool in ``-stay_open`` daemon mode.

ExifTool is Perl. Starting it costs roughly 150–200 ms of interpreter and
module load, which on a 200k-image library is over eight hours spent entirely
on process startup. Its ``-stay_open True -@ -`` mode keeps one interpreter
resident and accepts argument blocks on stdin, turning that into a single
startup for the whole scan — the order-of-magnitude difference the milestone
brief calls for.

The cost is that a resident process must be supervised. Three failure modes are
handled here because all three occur in practice:

* **desync** — a reply is matched to its request by a numbered ``{readyN}``
  sentinel, so a partial read can never be mistaken for the next file's answer;
* **hangs** — one pathological file can wedge ExifTool, so every exchange has a
  deadline and a wedged daemon is killed and respawned;
* **death** — if the process exits, the next call transparently restarts it and
  retries once.

If ExifTool is not installed at all, :attr:`ExifToolDaemon.available` is False
and callers fall back to :mod:`mediaengine.core.extractors.images`, which reads
EXIF through Pillow. That fallback is not a stub: on a machine without ExifTool
it is the only path that ever runs.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from ...errors import SubprocessTimeout
from ..procs import find_binary, kill_process_tree, popen

__all__ = ["ExifToolDaemon", "exiftool_available"]

_LOG = logging.getLogger(__name__)

#: Arguments sent with every block. ``-n`` gives raw numeric values, which is
#: what a database wants; ``-G0`` prefixes each key with its metadata group so
#: ``EXIF:Make`` and ``XMP:Make`` stay distinguishable.
_BASE_ARGS: tuple[str, ...] = (
    "-j",
    "-G0",
    "-n",
    "-charset",
    "utf8",
    "-api",
    "largefilesupport=1",
    "-fast2",
)


def exiftool_available() -> bool:
    """Whether the ExifTool binary can be found."""
    return find_binary("exiftool") is not None


class ExifToolDaemon:
    """A supervised, resident ExifTool process.

    Thread-safe: one lock serialises exchanges, because the daemon has a single
    stdin/stdout pair and interleaving two requests would corrupt both. Callers
    wanting parallelism should batch instead — :meth:`read_many` handles dozens
    of files per exchange, which is far cheaper than several daemons.
    """

    def __init__(self, *, timeout_s: float = 60.0, batch_size: int = 64) -> None:
        self._timeout = timeout_s
        self._batch_size = max(1, batch_size)
        self._lock = threading.Lock()
        self._process: Any = None
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._sequence = 0
        self._binary = find_binary("exiftool")
        self._restarts = 0
        self._closed = False

    @property
    def available(self) -> bool:
        """False when ExifTool is not installed. Callers must check this."""
        return self._binary is not None

    @property
    def restarts(self) -> int:
        """How many times the daemon had to be respawned. Surfaced in stats;
        a climbing number means a file in the library reliably wedges it."""
        return self._restarts

    # ── lifecycle ───────────────────────────────────────────────────────────

    def _spawn(self) -> None:
        if self._binary is None:
            raise RuntimeError("exiftool is not installed")
        _LOG.debug("starting exiftool daemon")
        self._process = popen(
            ["exiftool", "-stay_open", "True", "-@", "-"],
            bufsize=0,
        )
        self._stdout_lines = queue.Queue()
        self._reader = threading.Thread(
            target=self._pump, args=(self._process.stdout,), name="me-exiftool", daemon=True
        )
        self._reader.start()

    def _pump(self, stream: IO[bytes]) -> None:
        """Move stdout into a queue so reads can have deadlines.

        A blocking ``readline`` on a wedged child is unkillable from the
        calling thread; a queue with a timeout is not.
        """
        try:
            for raw in iter(stream.readline, b""):
                self._stdout_lines.put(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        except (OSError, ValueError):  # pipe closed under us
            pass
        finally:
            self._stdout_lines.put(None)

    def _ensure_running(self) -> None:
        if self._closed:
            raise RuntimeError("exiftool daemon is closed")
        if self._process is None or self._process.poll() is not None:
            if self._process is not None:
                self._restarts += 1
                _LOG.warning("exiftool daemon exited; restarting (restart #%d)", self._restarts)
                self._teardown()
            self._spawn()

    def _teardown(self) -> None:
        process, self._process = self._process, None
        self._reader = None
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write(b"-stay_open\nFalse\n")
                process.stdin.flush()
                process.wait(timeout=3.0)
        except (OSError, ValueError, Exception):  # noqa: BLE001 - shutting down regardless
            pass
        kill_process_tree(process)

    def close(self) -> None:
        """Shut the daemon down. Idempotent; safe from any thread."""
        with self._lock:
            self._closed = True
            self._teardown()

    def __enter__(self) -> "ExifToolDaemon":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── the exchange ────────────────────────────────────────────────────────

    def _exchange(self, args: Sequence[str]) -> str:
        """Send one argument block and collect stdout up to its sentinel."""
        self._sequence += 1
        token = str(self._sequence)
        process = self._process
        assert process is not None and process.stdin is not None  # noqa: S101 - invariant

        payload = "\n".join([*args, f"-execute{token}", ""]).encode("utf-8")
        process.stdin.write(payload)
        process.stdin.flush()

        sentinel = f"{{ready{token}}}"
        collected: list[str] = []
        import time

        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SubprocessTimeout(f"exiftool did not answer within {self._timeout:.0f}s")
            try:
                line = self._stdout_lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                raise SubprocessTimeout("exiftool daemon closed its output stream")
            if line.strip() == sentinel:
                break
            # A late reply from a previous, timed-out exchange. Discard rather
            # than mixing it into this file's metadata.
            if line.strip().startswith("{ready"):
                _LOG.debug("discarding stale exiftool sentinel %s", line.strip())
                collected.clear()
                continue
            collected.append(line)
        return "\n".join(collected)

    def read_many(self, paths: Sequence[Path | str]) -> dict[str, dict[str, Any]]:
        """Metadata for several files at once, keyed by the path given.

        Returns ``{}`` for files ExifTool could not read rather than raising:
        one unreadable file in a batch of sixty must not cost the other
        fifty-nine.
        """
        if not paths or not self.available:
            return {}

        results: dict[str, dict[str, Any]] = {}
        for start in range(0, len(paths), self._batch_size):
            chunk = [str(p) for p in paths[start : start + self._batch_size]]
            results.update(self._read_chunk(chunk))
        return results

    def _read_chunk(self, chunk: list[str]) -> dict[str, dict[str, Any]]:
        with self._lock:
            for attempt in (0, 1):
                try:
                    self._ensure_running()
                    output = self._exchange([*_BASE_ARGS, *chunk])
                    break
                except (SubprocessTimeout, OSError, ValueError, BrokenPipeError) as exc:
                    self._restarts += 1
                    _LOG.warning(
                        "exiftool exchange failed (%s); %s",
                        exc,
                        "retrying with a fresh daemon" if attempt == 0 else "giving up on this batch",
                    )
                    self._teardown()
                    if attempt == 1:
                        return {}
            else:  # pragma: no cover - loop always breaks or returns
                return {}

        return self._parse(output, chunk)

    @staticmethod
    def _parse(output: str, requested: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Map ExifTool's JSON array back onto the paths that were asked for."""
        text = output.strip()
        if not text:
            return {}
        try:
            records = json.loads(text)
        except json.JSONDecodeError as exc:
            _LOG.warning("exiftool returned unparsable JSON: %s", exc)
            return {}
        if not isinstance(records, list):
            return {}

        # ExifTool echoes SourceFile with forward slashes even on Windows, so
        # match on the normalised form rather than string equality.
        wanted = {str(Path(p)).replace("\\", "/").lower(): str(p) for p in requested}
        out: dict[str, dict[str, Any]] = {}
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            source = str(record.get("SourceFile", "")).replace("\\", "/").lower()
            key = wanted.get(source)
            if key is None and index < len(requested):
                key = str(requested[index])
            if key is not None:
                out[key] = record
        return out

    def read(self, path: Path | str) -> dict[str, Any]:
        """Metadata for a single file. ``{}`` if it could not be read."""
        return self.read_many([path]).get(str(path), {})
