"""Runs analyzers over the task queue and commits what they return.

The runner is the enforcement point for the plugin contract: validation,
producer attribution, user-data supremacy (via the repository commit path) and
error accounting all happen here, uniformly, regardless of which plugin is
running. Milestone 3's remaining transports (subprocess JSONL, HTTP) plug in
underneath `_execute` — everything above it, which is everything with a rule
in it, stays exactly as written.

Work flows through ``analysis_tasks``: enqueue is idempotent, claiming is
atomic on the writer thread, and a crash leaves rows that the next startup
requeues. Killing the process anywhere costs nothing but time.
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..core.control import CancelToken, ProgressCallback, ProgressReporter
from ..db.repositories import PendingAnnotation, PendingRegion, Repositories
from ..errors import PermanentError, PluginExecutionError, TransientError
from ..types import AnnotationSource, TaskState
from ..util import stable_hash
from .contract import Annotation, validate_annotations
from .context import AnalysisContext
from .registry import LoadedPlugin, PluginRegistry

__all__ = ["PluginRunner", "BackfillResult"]

_LOG = logging.getLogger(__name__)

#: How many tasks to claim per writer round-trip.
_CLAIM_BATCH = 16


@dataclass(slots=True)
class BackfillResult:
    """What one backfill pass did."""

    enqueued: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    blocked_by_user: int = 0
    annotations_written: int = 0
    duration_s: float = 0.0
    by_plugin: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enqueued": self.enqueued,
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "blocked_by_user": self.blocked_by_user,
            "annotations_written": self.annotations_written,
            "duration_s": round(self.duration_s, 2),
            "by_plugin": self.by_plugin,
        }


class PluginRunner:
    """Drains the analysis queue for in-process plugins."""

    def __init__(
        self,
        config: Config,
        repos: Repositories,
        registry: PluginRegistry,
        *,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self.config = config
        self.repos = repos
        self.registry = registry
        self.cancel = CancelToken() if cancel is None else cancel
        self.reporter = ProgressReporter(progress)

    # ── enqueue ─────────────────────────────────────────────────────────────

    def enqueue_backfill(self, plugins: list[LoadedPlugin], *, limit: int | None = None) -> int:
        """One pending task per (matching asset × plugin version). Idempotent:
        the ``(asset, plugin, version)`` unique key makes re-enqueueing free,
        which is why 'just run backfill again' is always a safe answer."""
        total = 0
        for plugin in plugins:
            accepts = list(plugin.info.accepts)
            after_id = 0
            remaining = limit
            while True:
                page = 1000 if remaining is None else min(1000, remaining)
                if page <= 0:
                    break
                asset_ids = self.repos.assets.iter_asset_ids(
                    media_types=accepts, after_id=after_id, limit=page
                )
                if not asset_ids:
                    break
                total += self.repos.tasks.enqueue_many(
                    asset_ids, plugin.info.id, plugin.info.version
                )
                after_id = asset_ids[-1]
                if remaining is not None:
                    remaining -= len(asset_ids)
        return total

    # ── drain ───────────────────────────────────────────────────────────────

    def run(
        self,
        plugin_ids: list[str] | None = None,
        *,
        enqueue: bool = True,
        limit: int | None = None,
    ) -> BackfillResult:
        """Run the queue to empty for the given (or all enabled) plugins."""
        started = time.monotonic()
        result = BackfillResult()

        available = {p.info.id: p for p in self.registry.enabled()}
        if plugin_ids:
            # Explicitly named plugins run even if config leaves them disabled:
            # `backfill --plugin x` is the operator saying so at the keyboard.
            for plugin_id in plugin_ids:
                if plugin_id in available:
                    continue
                loaded = self.registry.get(plugin_id)
                if loaded is not None and loaded.analyzer is not None:
                    available[plugin_id] = loaded
            targets = {k: v for k, v in available.items() if k in set(plugin_ids)}
        else:
            targets = available

        if not targets:
            _LOG.info("no runnable plugins %s", f"among {plugin_ids}" if plugin_ids else "enabled")
            return result

        if enqueue:
            result.enqueued = self.enqueue_backfill(list(targets.values()), limit=limit)

        wanted = list(targets)
        while not self.cancel.cancelled:
            claimed = self.repos.tasks.claim(plugin_ids=wanted, limit=_CLAIM_BATCH)
            if not claimed:
                break
            for task in claimed:
                # wait(0) rather than .cancelled: the token trips from another
                # thread, which mypy's narrowing of a bool property inside the
                # `while not cancelled` loop cannot represent.
                if self.cancel.wait(0):
                    # Unclaim cleanly; the next run picks it up.
                    self.repos.tasks.retry_later(int(task["id"]), "cancelled before start")
                    continue
                self._run_task(task, targets, result)
            self.reporter.advance("analyze", by=len(claimed))

        result.duration_s = time.monotonic() - started
        self.reporter.flush(
            "analyze",
            message=f"{result.completed} done, {result.failed} failed, "
            f"{result.annotations_written} annotations",
        )
        return result

    # ── one task ────────────────────────────────────────────────────────────

    def _run_task(
        self,
        task: dict[str, Any],
        targets: dict[str, LoadedPlugin],
        result: BackfillResult,
    ) -> None:
        task_id = int(task["id"])
        asset_id = int(task["asset_id"])
        plugin_id = str(task["plugin_id"])
        plugin = targets.get(plugin_id)
        per_plugin = result.by_plugin.setdefault(
            plugin_id, {"done": 0, "failed": 0, "skipped": 0, "annotations": 0}
        )

        if plugin is None or plugin.analyzer is None:
            self.repos.tasks.complete(task_id, TaskState.SKIPPED, error="plugin not loadable")
            result.skipped += 1
            per_plugin["skipped"] += 1
            return

        # A task enqueued for a version that has since changed is stale; the
        # registry already re-enqueued current-version work.
        if str(task["plugin_version"]) != plugin.info.version:
            self.repos.tasks.complete(task_id, TaskState.SKIPPED, error="superseded version")
            result.skipped += 1
            per_plugin["skipped"] += 1
            return

        asset = self.repos.assets.get_asset(asset_id)
        if asset is None:
            self.repos.tasks.complete(task_id, TaskState.SKIPPED, error="asset deleted")
            result.skipped += 1
            per_plugin["skipped"] += 1
            return

        try:
            written, blocked = self._execute(plugin, asset)
        except TransientError as exc:
            attempts = int(task.get("attempts") or 0)
            if attempts < self.config.workers.max_retries:
                self.repos.tasks.retry_later(task_id, str(exc))
            else:
                self._fail(task_id, asset_id, plugin_id, exc, result, per_plugin)
            return
        except PermanentError as exc:
            self._fail(task_id, asset_id, plugin_id, exc, result, per_plugin)
            return
        except Exception as exc:  # noqa: BLE001 - plugin code is untrusted
            self._fail(
                task_id,
                asset_id,
                plugin_id,
                PluginExecutionError(f"{plugin_id} raised: {exc}", plugin_id=plugin_id),
                result,
                per_plugin,
                traceback_text=traceback.format_exc(),
            )
            return

        self.repos.tasks.complete(task_id, TaskState.DONE)
        result.completed += 1
        result.annotations_written += written
        result.blocked_by_user += blocked
        per_plugin["done"] += 1
        per_plugin["annotations"] += written

    def _execute(self, plugin: LoadedPlugin, asset: dict[str, Any]) -> tuple[int, int]:
        """Analyze one asset and commit. Returns (written, blocked_by_user)."""
        info = plugin.info
        assert plugin.analyzer is not None  # noqa: S101 - guarded by caller
        context = AnalysisContext(
            repos=self.repos,
            config=self.config,
            asset=asset,
            plugin_id=info.id,
            plugin_config=self.config.plugin_config(info.id),
        )
        raw = list(plugin.analyzer.analyze(context) or [])
        validate_annotations(
            info.id,
            raw,
            embedding_dim=info.embedding_dim,
            per_namespace_dim={
                ns: int(meta["embedding_dim"])
                for ns, meta in info.namespaces.items()
                if isinstance(meta.get("embedding_dim"), int)
            },
        )
        if not raw:
            return 0, 0  # "nothing to say" is success, per contract

        producer_id = self.repos.annotations.get_or_create_producer(
            info.id,
            info.version,
            model_id=info.model_id or None,
            config_hash=plugin.config_hash or (
                stable_hash(self.config.plugin_config(info.id))
                if self.config.plugin_config(info.id)
                else None
            ),
            transport=info.transport,
        )
        pending = [self._to_pending(a) for a in raw]
        committed = self.repos.annotations.commit(
            int(asset["id"]), producer_id, info.id, pending
        )
        if committed.total() or committed.embeddings_created:
            # New labels must reach the FTS index or search lies.
            self._reindex(int(asset["id"]))
        return committed.total(), committed.blocked_by_user

    @staticmethod
    def _to_pending(annotation: Annotation) -> PendingAnnotation:
        region = annotation.region
        return PendingAnnotation(
            namespace=annotation.namespace.strip(),
            label=annotation.label.strip(),
            value=annotation.value,
            confidence=annotation.confidence,
            region=PendingRegion(
                x=region.x, y=region.y, w=region.w, h=region.h,
                frame_time=region.frame_time, page_number=region.page_number,
                kind=region.kind,
            )
            if region is not None
            else None,
            embedding=annotation.embedding,
            source=AnnotationSource.DERIVED,
        )

    def _reindex(self, asset_id: int) -> None:
        from ..core.pipeline import IngestPipeline

        IngestPipeline(self.config, self.repos, cancel=self.cancel).reindex_asset(asset_id)

    def _fail(
        self,
        task_id: int,
        asset_id: int,
        plugin_id: str,
        exc: Exception,
        result: BackfillResult,
        per_plugin: dict[str, int],
        *,
        traceback_text: str | None = None,
    ) -> None:
        self.repos.tasks.complete(task_id, TaskState.FAILED, error=str(exc)[:2000])
        self.repos.tasks.record_error(
            type(exc).__name__,
            str(exc),
            scope="plugin",
            ref_id=asset_id,
            ref_text=plugin_id,
            traceback=traceback_text,
        )
        result.failed += 1
        per_plugin["failed"] += 1
