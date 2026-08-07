"""Exception hierarchy.

Failures are split into two families because the retry policy differs:

* :class:`TransientError` — worth retrying with exponential backoff (database
  lock, subprocess timeout, remote plugin 503).
* :class:`PermanentError` — retrying will produce the same result (corrupt
  file, unsupported codec, plugin contract violation). Fail fast, record to
  the ``errors`` table, mark the task ``failed``.

:mod:`mediaengine.workers.scheduler` branches on these two base classes and
nothing else, so a new error type only needs to pick the right parent.
"""

from __future__ import annotations

__all__ = [
    "MediaEngineError",
    "TransientError",
    "PermanentError",
    "ConfigError",
    "MigrationError",
    "WriterClosedError",
    "DatabaseLockedError",
    "ExtractionError",
    "SubprocessTimeout",
    "SubprocessFailed",
    "UnsupportedMedia",
    "CorruptMedia",
    "PluginError",
    "PluginLoadError",
    "PluginContractError",
    "PluginExecutionError",
    "PluginTimeout",
    "PluginUnavailable",
    "CapabilityDenied",
    "DependencyUnsatisfied",
    "SearchError",
    "QueryError",
    "NotFoundError",
    "ImmutableUserDataError",
    "OperationCancelled",
]


class MediaEngineError(Exception):
    """Base class for everything this package raises deliberately."""


class TransientError(MediaEngineError):
    """A failure that may succeed on retry."""


class PermanentError(MediaEngineError):
    """A failure that will recur identically on retry."""


# ── Configuration and schema ─────────────────────────────────────────────────


class ConfigError(PermanentError):
    """The configuration file is missing, malformed, or internally invalid."""


class MigrationError(PermanentError):
    """A schema migration could not be applied."""


# ── Database ─────────────────────────────────────────────────────────────────


class WriterClosedError(PermanentError):
    """A write was submitted after the writer thread began shutting down."""


class DatabaseLockedError(TransientError):
    """SQLite reported ``database is locked`` past ``busy_timeout``."""


# ── Extraction ───────────────────────────────────────────────────────────────


class ExtractionError(MediaEngineError):
    """Base for metadata-extraction failures."""


class SubprocessTimeout(TransientError, ExtractionError):
    """A helper process (ffprobe/ffmpeg/exiftool) exceeded its hard timeout."""


class SubprocessFailed(PermanentError, ExtractionError):
    """A helper process exited non-zero for a non-timeout reason."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class UnsupportedMedia(PermanentError, ExtractionError):
    """The file is a recognised type the engine has no extractor for."""


class CorruptMedia(PermanentError, ExtractionError):
    """The file is truncated, zero-byte, or otherwise undecodable."""


# ── Plugins ──────────────────────────────────────────────────────────────────


class PluginError(MediaEngineError):
    """Base for plugin-related failures."""

    def __init__(self, message: str, *, plugin_id: str | None = None) -> None:
        super().__init__(message)
        self.plugin_id = plugin_id


class PluginLoadError(PermanentError, PluginError):
    """A plugin could not be imported, or its manifest is invalid."""


class PluginContractError(PermanentError, PluginError):
    """A plugin returned something the contract does not permit."""


class PluginExecutionError(PermanentError, PluginError):
    """A plugin raised during ``analyze``. The traceback is preserved."""


class PluginTimeout(TransientError, PluginError):
    """A plugin exceeded its per-asset wall-clock budget."""


class PluginUnavailable(TransientError, PluginError):
    """An out-of-process or HTTP plugin is not reachable right now."""


class CapabilityDenied(PermanentError, PluginError):
    """A plugin requested a capability the operator has not granted."""


class DependencyUnsatisfied(PermanentError, PluginError):
    """A declared ``depends_on`` plugin is missing, disabled, or cyclic."""


# ── Search ───────────────────────────────────────────────────────────────────


class SearchError(MediaEngineError):
    """Base for query-planning and execution failures."""


class QueryError(PermanentError, SearchError):
    """A query is malformed or references an unknown field."""


# ── Domain ───────────────────────────────────────────────────────────────────


class NotFoundError(PermanentError):
    """A referenced row does not exist."""


class ImmutableUserDataError(PermanentError):
    """A plugin attempted to overwrite a user-confirmed value.

    Principle 4 of the design: user data outranks machine data, always.
    """


class OperationCancelled(MediaEngineError):
    """A cancel token was tripped. Not an error condition; unwinds cleanly."""
