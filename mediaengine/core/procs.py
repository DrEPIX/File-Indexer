"""Running helper binaries safely.

Every external tool the engine shells out to — ``ffprobe``, ``ffmpeg``,
``exiftool`` — goes through here, for three reasons:

**Nothing is ever passed to a shell.** Arguments are a list, ``shell=False``,
always. A filename containing ``;`` or ``$(…)`` is a filename, not an
injection. Library paths are user data and must be treated as hostile.

**Every process has a hard deadline, and the deadline kills the tree.**
``Popen.kill()` only kills the direct child; ffmpeg spawning a worker leaves
the worker running. On POSIX that means a new session and ``killpg``; on
Windows, ``taskkill /T``. Without this, a scan of a library containing one
malformed video leaks a process per restart until the machine dies.

**Output is bounded.** A tool that decides to emit gigabytes of warnings must
not take the engine's memory with it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict

from ..errors import SubprocessFailed, SubprocessTimeout

__all__ = [
    "CommandResult",
    "run_command",
    "find_binary",
    "have_binary",
    "kill_process_tree",
    "popen",
    "MAX_CAPTURE_BYTES",
]

_LOG = logging.getLogger(__name__)

#: Cap on captured stdout/stderr per invocation. ffprobe JSON for a pathological
#: file with thousands of chapters is still far below this.
MAX_CAPTURE_BYTES: Final[int] = 32 * 1024 * 1024

_IS_WINDOWS: Final[bool] = sys.platform == "win32"

# Resolved binary locations, cached because shutil.which() hits the filesystem
# once per PATH entry and the pipeline asks for ffprobe once per video.
_WHICH_CACHE: dict[str, str | None] = {}
_WHICH_LOCK = threading.Lock()

#: Extra directories probed when a tool is not on PATH. Windows installers
#: routinely skip the PATH entry, and an engine that says "ffprobe not found"
#: on a machine that plainly has ffmpeg installed is an unhelpful engine.
_EXTRA_SEARCH_DIRS: Final[tuple[str, ...]] = (
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files\ExifTool",
    r"C:\Program Files (x86)\ExifTool",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/usr/bin",
)


def find_binary(name: str) -> str | None:
    """Locate a helper binary, or ``None``. Result is cached per process."""
    with _WHICH_LOCK:
        if name in _WHICH_CACHE:
            return _WHICH_CACHE[name]
    found = shutil.which(name)
    if found is None:
        for directory in _EXTRA_SEARCH_DIRS:
            candidate = Path(directory) / (f"{name}.exe" if _IS_WINDOWS else name)
            if candidate.is_file():
                found = str(candidate)
                break
    with _WHICH_LOCK:
        _WHICH_CACHE[name] = found
    if found is None:
        _LOG.debug("helper binary not found: %s", name)
    return found


def have_binary(name: str) -> bool:
    """Whether a helper binary is callable. Drives graceful degradation."""
    return find_binary(name) is not None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of one helper invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self, encoding: str = "utf-8") -> str:
        """stdout as text, replacing undecodable bytes rather than raising.

        Helper tools emit metadata copied verbatim out of files, which is
        frequently mis-encoded. Losing one character beats losing the asset.
        """
        return self.stdout.decode(encoding, errors="replace")

    def stderr_text(self, encoding: str = "utf-8", *, limit: int = 4000) -> str:
        return self.stderr.decode(encoding, errors="replace")[:limit]


class _SpawnFlags(TypedDict, total=False):
    """The platform-specific half of a ``Popen`` call.

    Declared as a TypedDict rather than ``dict[str, object]`` so that
    ``Popen(**_spawn_kwargs())`` still resolves against ``Popen``'s overloads
    instead of collapsing to "some mapping" and failing every one of them.
    """

    creationflags: int
    start_new_session: bool


def _spawn_kwargs() -> _SpawnFlags:
    """Platform flags that make the child killable as a group."""
    if _IS_WINDOWS:
        # A new process group is what lets taskkill /T find the descendants.
        # CREATE_NO_WINDOW stops a console flashing up per ffprobe call, which
        # on a 200k-file library is 200k window creations.
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        }
    return {"start_new_session": True}


def kill_process_tree(process: subprocess.Popen[bytes], *, grace_s: float = 3.0) -> None:
    """Terminate a child and everything it spawned.

    Politely first, then not. The grace period matters for ffmpeg, which
    flushes and closes its output file on SIGTERM — killing it outright leaves
    a truncated derivative that looks valid to the next run.
    """
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=grace_s + 5.0,
            )
        else:
            os.killpg(os.getpgid(process.pid), 15)  # SIGTERM
    except (OSError, subprocess.SubprocessError) as exc:
        _LOG.debug("tree kill of pid %d failed: %s", process.pid, exc)

    try:
        process.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), 9)  # SIGKILL
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
        _LOG.error("process %d survived SIGKILL", process.pid)


def popen(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin: int | None = subprocess.PIPE,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.PIPE,
    bufsize: int = 0,
) -> subprocess.Popen[bytes]:
    """Start a long-lived child (the exiftool daemon, a subprocess plugin).

    Same safety flags as :func:`run_command` but the caller owns the lifetime
    and must call :func:`kill_process_tree` when done.
    """
    if not args:
        raise ValueError("empty command")
    resolved = find_binary(args[0]) or args[0]
    full_env = {**os.environ, **dict(env)} if env else None
    return subprocess.Popen(  # noqa: S603 - list args, shell=False, never interpolated
        [resolved, *[str(a) for a in args[1:]]],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        bufsize=bufsize,
        shell=False,
        **_spawn_kwargs(),
    )


def run_command(
    args: Sequence[str],
    *,
    timeout: float = 60.0,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    check: bool = False,
    max_output: int = MAX_CAPTURE_BYTES,
) -> CommandResult:
    """Run a helper to completion under a hard deadline.

    :param check: raise :class:`SubprocessFailed` on a non-zero exit. Left off
        by default because several tools (ffprobe on a partially readable file)
        exit non-zero while still producing usable output, and the caller is
        better placed to judge.
    :raises SubprocessTimeout: the deadline expired. Transient by
        classification, so the scheduler retries it.
    :raises SubprocessFailed: the binary is missing, or ``check`` and non-zero.
    """
    import time

    if not args:
        raise ValueError("empty command")
    resolved = find_binary(args[0])
    if resolved is None:
        raise SubprocessFailed(f"{args[0]} not found on PATH")

    command = [resolved, *[str(a) for a in args[1:]]]
    full_env = {**os.environ, **dict(env)} if env else None
    started = time.monotonic()

    process = subprocess.Popen(  # noqa: S603 - list args, shell=False
        command,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        **_spawn_kwargs(),
    )
    try:
        stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(process)
        # Drain the pipes after the kill or the file descriptors leak, and on
        # Windows a still-open pipe keeps the process object alive.
        try:
            stdout, stderr = process.communicate(timeout=5.0)
        except subprocess.SubprocessError:
            stdout, stderr = b"", b""
        raise SubprocessTimeout(
            f"{Path(command[0]).name} exceeded {timeout:.1f}s: {' '.join(command[1:])[:300]}"
        )
    except OSError as exc:
        kill_process_tree(process)
        raise SubprocessFailed(f"{Path(command[0]).name} failed to run: {exc}") from exc

    duration = time.monotonic() - started
    result = CommandResult(
        returncode=process.returncode,
        stdout=(stdout or b"")[:max_output],
        stderr=(stderr or b"")[:max_output],
        duration_s=duration,
    )
    if check and not result.ok:
        raise SubprocessFailed(
            f"{Path(command[0]).name} exited {result.returncode}: {result.stderr_text()}",
            returncode=result.returncode,
            stderr=result.stderr_text(),
        )
    return result
