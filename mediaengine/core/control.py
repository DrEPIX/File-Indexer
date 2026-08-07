"""Cancellation and progress plumbing shared by every long-running operation.

A scan of a large library runs for hours. Two things must therefore be true of
every stage: it can be stopped promptly from another thread, and it can say how
far along it is without the caller polling the database.

:class:`CancelToken` is checked between units of work, never mid-write — a
cancelled scan leaves a consistent database, just an incomplete one, which is
fine because the next scan resumes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import OperationCancelled

__all__ = ["CancelToken", "ProgressEvent", "ProgressReporter", "ProgressCallback"]


class CancelToken:
    """A thread-safe "stop what you are doing" flag.

    Cooperative, not pre-emptive: workers check it at boundaries. That is a
    deliberate limitation — killing a thread mid-transaction is how databases
    get corrupted, and every stage here is short enough that the latency
    between ``cancel()`` and actual stop is under a second.
    """

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""

    def cancel(self, reason: str = "cancelled by caller") -> None:
        self._reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def raise_if_cancelled(self) -> None:
        """Unwind the current operation if cancellation was requested."""
        if self._event.is_set():
            raise OperationCancelled(self._reason)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled. Returns True if it was."""
        return self._event.wait(timeout)

    # Deliberately no __bool__. Defining it as "is cancelled" reads nicely but
    # makes a live, uncancelled token *falsy*, so the natural
    # `cancel or CancelToken()` default silently throws the caller's token away
    # and substitutes one nothing can ever trip. Callers use `is None` instead.

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        state = f"cancelled: {self._reason}" if self._event.is_set() else "active"
        return f"<CancelToken {state}>"


@dataclass(slots=True)
class ProgressEvent:
    """One progress tick, shaped for both the CLI and the WebSocket feed."""

    stage: str
    """``walk`` | ``triage`` | ``identify`` | ``extract`` | ``derive`` | ``index``."""

    done: int = 0
    total: int | None = None
    """``None`` while the total is genuinely unknown — a walk in progress does
    not know how many files it will find, and a fabricated denominator makes a
    progress bar lie."""

    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(1.0, self.done / self.total)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "fraction": self.fraction,
            "message": self.message,
            "detail": self.detail,
            "at": self.at,
        }


ProgressCallback = Callable[[ProgressEvent], None]


class ProgressReporter:
    """Rate-limited progress emission.

    A scan touching 200k files would otherwise emit 200k events, which costs
    more than the work being reported on. Ticks are coalesced to at most one
    every ``min_interval_s``; the final ``flush`` always emits so a consumer
    never misses the completion event.
    """

    __slots__ = ("_callback", "_min_interval", "_last_emit", "_lock", "_counts")

    def __init__(
        self, callback: ProgressCallback | None = None, *, min_interval_s: float = 0.25
    ) -> None:
        self._callback = callback
        self._min_interval = min_interval_s
        self._last_emit = 0.0
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def advance(
        self,
        stage: str,
        *,
        by: int = 1,
        total: int | None = None,
        message: str = "",
        **detail: Any,
    ) -> None:
        """Record work done, emitting only if the rate limit allows."""
        with self._lock:
            done = self._counts.get(stage, 0) + by
            self._counts[stage] = done
            now = time.monotonic()
            if self._callback is None or now - self._last_emit < self._min_interval:
                return
            self._last_emit = now
            callback = self._callback
        callback(ProgressEvent(stage=stage, done=done, total=total, message=message, detail=detail))

    def flush(self, stage: str, *, total: int | None = None, message: str = "", **detail: Any) -> None:
        """Emit unconditionally. Use for stage transitions and completion."""
        with self._lock:
            done = self._counts.get(stage, 0)
            self._last_emit = time.monotonic()
            callback = self._callback
        if callback is not None:
            callback(
                ProgressEvent(stage=stage, done=done, total=total, message=message, detail=detail)
            )

    def count(self, stage: str) -> int:
        with self._lock:
            return self._counts.get(stage, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
