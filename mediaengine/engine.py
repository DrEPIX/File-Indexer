"""The engine facade.

One object that owns the database, the repositories and the worker lifetime,
so that an embedding application — a PyQt gallery, the FastAPI service, the
CLI — talks to one thing instead of assembling five.

Everything here is thin. The facade's job is lifecycle and composition, not
logic: if a method does more than wire two components together it belongs in
the component.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config, load_config
from .core.control import CancelToken, ProgressCallback
from .core.pipeline import IngestPipeline, ScanResult
from .db.connection import Database
from .db.repositories import Repositories
from .errors import NotFoundError
from .util import setup_logging, utcnow, utcnow_iso

if TYPE_CHECKING:  # pragma: no cover - the facade stays cheap to import
    from .plugins import BackfillResult, PluginRegistry
    from .search import Query, SearchResult

__all__ = ["MediaEngine"]

_LOG = logging.getLogger(__name__)

#: A task whose heartbeat is older than this was left behind by a dead process.
#: Generous, because a slow plugin on a large video legitimately goes quiet for
#: a while and reclaiming its task would run it twice.
_STALE_TASK_AGE = timedelta(minutes=30)


class MediaEngine:
    """Owns the library: database, repositories, ingest and analysis."""

    def __init__(self, config: Config | None = None, *, configure_logging: bool = False) -> None:
        self.config = config or load_config()
        if configure_logging:
            log = self.config.logging
            setup_logging(
                level=log.level,
                fmt=log.format,
                file=log.file,
                max_bytes=log.max_bytes,
                backup_count=log.backup_count,
                console=log.console,
            )
        self._db: Database | None = None
        self._repos: Repositories | None = None
        self._lock = threading.Lock()
        self._started = False
        self._cancel = CancelToken()
        self._registry: "PluginRegistry | None" = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> "MediaEngine":
        """Open the database, migrate it, and reclaim abandoned work.

        Idempotent, so an embedding application can call it without tracking
        whether it already did.
        """
        with self._lock:
            if self._started:
                return self
            self.config.ensure_directories()
            self._db = Database(
                self.config.storage.db_path,
                writer_queue_size=self.config.workers.writer_queue_size,
            )
            self._db.migrate()
            self._repos = Repositories(self._db)
            # Anything still marked 'running' belongs to a process that died.
            # Without this, one hard kill strands that work forever.
            reclaimed = self._repos.tasks.requeue_stale(
                (utcnow() - _STALE_TASK_AGE).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            )
            if reclaimed:
                _LOG.info("requeued %d task(s) abandoned by a previous run", reclaimed)
            self._started = True
        return self

    def close(self) -> None:
        """Drain writes, checkpoint the WAL, close every connection."""
        with self._lock:
            if self._db is not None:
                self._db.close()
            self._db = None
            self._repos = None
            self._registry = None
            self._started = False

    def __enter__(self) -> "MediaEngine":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── accessors ───────────────────────────────────────────────────────────

    @property
    def db(self) -> Database:
        if self._db is None:
            raise RuntimeError("engine is not started; call start() first")
        return self._db

    @property
    def repos(self) -> Repositories:
        if self._repos is None:
            raise RuntimeError("engine is not started; call start() first")
        return self._repos

    @property
    def started(self) -> bool:
        return self._started

    # ── ingest ──────────────────────────────────────────────────────────────

    def scan(
        self,
        roots: Sequence[Path | str] | None = None,
        *,
        resume: bool = True,
        rehash: bool = False,
        generate_derivatives: bool = True,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> list[ScanResult]:
        """Index one or more directory trees. Blocks until finished."""
        self.start()
        pipeline = IngestPipeline(
            self.config, self.repos, progress=progress,
            cancel=self._cancel if cancel is None else cancel,
        )
        return pipeline.scan(
            list(roots) if roots else None,
            resume=resume,
            rehash=rehash,
            generate_derivatives=generate_derivatives,
        )

    def cancel(self, reason: str = "cancelled by caller") -> None:
        """Ask any running scan to stop at the next safe point."""
        self._cancel.cancel(reason)

    def pipeline(
        self, *, progress: ProgressCallback | None = None, cancel: CancelToken | None = None
    ) -> IngestPipeline:
        """A pipeline for single-asset operations (reindex, rebuild)."""
        self.start()
        return IngestPipeline(
            self.config, self.repos, progress=progress,
            cancel=self._cancel if cancel is None else cancel,
        )

    # ── plugins and search ──────────────────────────────────────────────────

    @property
    def plugins(self) -> "PluginRegistry":
        """The plugin registry, discovering on first access."""
        from .plugins import PluginRegistry

        self.start()
        if self._registry is None:
            self._registry = PluginRegistry(self.config, self.repos)
            self._registry.discover()
        return self._registry

    def backfill(
        self,
        plugin_ids: Sequence[str] | None = None,
        *,
        limit: int | None = None,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> "BackfillResult":
        """Run analyzers over the library via the task queue. Blocks."""
        from .plugins import PluginRunner

        runner = PluginRunner(
            self.config,
            self.repos,
            self.plugins,
            progress=progress,
            cancel=self._cancel if cancel is None else cancel,
        )
        return runner.run(list(plugin_ids) if plugin_ids else None, limit=limit)

    def search(self, query: "Query | str", *, with_facets: bool = True) -> "SearchResult":
        """Execute a search; accepts a Query or the shared string syntax."""
        from .search import Query, SearchPlanner, parse_query

        self.start()
        parsed = parse_query(query) if isinstance(query, str) else query
        assert isinstance(parsed, Query)  # noqa: S101 - narrows the union for mypy
        return SearchPlanner(self.repos).search(parsed, with_facets=with_facets)

    # ── reads ───────────────────────────────────────────────────────────────

    def asset(self, asset_id: int) -> dict[str, Any]:
        """Everything the system knows about one asset.

        Raises rather than returning ``None``: a caller asking for a specific
        id has already decided it exists, and a silent ``None`` turns into an
        ``AttributeError`` three frames away.
        """
        self.start()
        record = self.repos.assets.export_asset(asset_id)
        if record is None:
            raise NotFoundError(f"asset {asset_id} does not exist")
        return record

    def asset_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        self.start()
        return self.repos.assets.get_asset_by_hash(content_hash)

    def stats(self) -> dict[str, Any]:
        """Counters for ``mediaengine stat`` and ``GET /api/admin/stats``."""
        self.start()
        payload: dict[str, Any] = dict(self.repos.stats())
        payload["schema_version"] = self.db.schema_version
        payload["config"] = {
            "db_path": str(self.config.storage.db_path),
            "derivatives_path": str(self.config.storage.derivatives_path),
            "roots": [str(r) for r in self.config.library.roots],
            "hash_algorithm": self.config.scan.hash_algorithm,
        }
        payload["capabilities"] = self.capabilities()
        payload["vec_available"] = self.db.vec_available
        return payload

    def capabilities(self) -> dict[str, Any]:
        """Which optional dependencies and helper binaries are usable."""
        from .core.extractors import exiftool_available, ffprobe_available
        from .core.identity import blake3_available
        from .core.procs import have_binary

        return {
            "blake3": blake3_available(),
            "exiftool": exiftool_available(),
            "ffprobe": ffprobe_available(),
            "ffmpeg": have_binary("ffmpeg"),
            "sqlite_vec": self.db.vec_available if self._db is not None else False,
        }

    def scans(self, limit: int = 20) -> list[dict[str, Any]]:
        self.start()
        return self.repos.tasks.list_scans(limit)

    def errors(self, limit: int = 50, *, scope: str | None = None) -> list[dict[str, Any]]:
        self.start()
        return self.repos.tasks.list_errors(limit, scope=scope)

    # ── maintenance ─────────────────────────────────────────────────────────

    def migrate(self) -> int:
        """Apply pending migrations and return the resulting version."""
        self.start()
        return self.db.schema_version

    def optimize(self) -> None:
        """Run SQLite's optimizer and compact the FTS index."""
        self.start()
        self.db.optimize()

    def vacuum(self) -> None:
        self.start()
        self.db.vacuum()

    def integrity_check(self) -> list[str]:
        self.start()
        return self.db.integrity_check()

    def purge_asset_derivatives(self, asset_id: int) -> int:
        """Delete an asset's derivative files and their index rows."""
        self.start()
        asset = self.repos.assets.get_asset(asset_id)
        builder = self.pipeline().derivatives
        return builder.purge_asset(
            asset_id, str(asset["content_hash"]) if asset else None
        )

    def purge_biometrics(self) -> dict[str, int]:
        """Delete every face region, template, cluster and identity link.

        One transaction, and the media index survives it intact.
        """
        self.start()
        return self.repos.identities.purge_biometrics()

    def record_note(self, key: str, value: str) -> None:
        """Store a small piece of engine state. Used by the CLI and the API."""
        self.start()
        self.repos.tasks.kv_set(key, value)

    def health(self) -> dict[str, Any]:
        """Cheap liveness payload, shaped for ``GET /api/health``."""
        from . import __version__

        return {
            "status": "ok" if self._started else "starting",
            "version": __version__,
            "schema_version": self.db.schema_version if self._db is not None else 0,
            "at": utcnow_iso(),
        }
