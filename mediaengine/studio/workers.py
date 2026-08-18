"""Qt thread-pool bridge for blocking MediaEngine operations."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    finished = Signal()


class EngineWorker(QRunnable):
    """Run one callable without ever touching a widget from the worker thread."""

    def __init__(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self.function(*self.args, **self.kwargs)
        except Exception:
            self._emit(self.signals.error, traceback.format_exc())
        else:
            self._emit(self.signals.result, result)
        finally:
            self._emit(self.signals.finished)

    @staticmethod
    def _emit(signal: Any, *payload: Any) -> None:
        """Deliver a result unless the window that wanted it is already gone.

        Closing Studio while a scan or a store refresh is in flight destroys
        the receiving widgets first; Qt then raises "Signal source has been
        deleted" from the worker thread, where nothing can catch it and the
        user sees a traceback on the way out of an application they just
        closed successfully.
        """
        try:
            signal.emit(*payload)
        except RuntimeError:
            pass
