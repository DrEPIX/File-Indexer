"""Scan sessions, the analysis task queue, errors and the plugin registry.

The task queue is what makes backfill work and what makes the whole system
resumable. Registering a new plugin does not analyze anything; it inserts one
``pending`` row per matching asset. A worker then walks the queue. Kill the
process at any point and the queue is exactly where it was.

Crash recovery: tasks left in ``running`` by a killed process are reclaimed by
:meth:`requeue_stale`, which is called once at startup. Without it, every hard
kill would strand work forever.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from ...types import ScanState, TaskState
from ...util import json_dumps, json_loads, utcnow_iso
from .base import Repository, placeholders, row_to_dict, rows_to_dicts

__all__ = ["TaskRepository"]


class TaskRepository(Repository):
    """Job bookkeeping."""

    # ── scan sessions ───────────────────────────────────────────────────────

    def create_scan(
        self,
        root_path: str,
        *,
        options: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        return self._write(
            lambda c: self._insert(
                c,
                "scan_sessions",
                {
                    "root_path": root_path,
                    "started_at": utcnow_iso(),
                    "state": str(ScanState.RUNNING),
                    "options_json": json_dumps(options or {}),
                },
            ),
            conn,
            label="create_scan",
        )

    def update_scan(
        self,
        scan_id: int,
        *,
        files_seen: int | None = None,
        files_new: int | None = None,
        files_updated: int | None = None,
        files_skipped: int | None = None,
        bytes_seen: int | None = None,
        errors: int | None = None,
        cursor: str | None = None,
        state: ScanState | str | None = None,
        finished: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Apply counter deltas and state changes to a scan session.

        Counters are *increments*, not absolutes, so several worker threads can
        report progress without reading first.
        """
        increments = {
            "files_seen": files_seen,
            "files_new": files_new,
            "files_updated": files_updated,
            "files_skipped": files_skipped,
            "bytes_seen": bytes_seen,
            "errors": errors,
        }
        live = {k: v for k, v in increments.items() if v}

        def _op(c: sqlite3.Connection) -> None:
            if live:
                assignments = ",".join(f"{k}={k}+?" for k in live)
                c.execute(
                    f"UPDATE scan_sessions SET {assignments} WHERE id = ?",  # noqa: S608
                    [*live.values(), scan_id],
                )
            patch: dict[str, Any] = {}
            if cursor is not None:
                patch["cursor"] = cursor
            if state is not None:
                patch["state"] = str(state)
            if finished:
                patch["finished_at"] = utcnow_iso()
            if patch:
                self._update(c, "scan_sessions", patch, "id = ?", (scan_id,))

        self._write(_op, conn, label="update_scan")

    def get_scan(self, scan_id: int) -> dict[str, Any] | None:
        row = row_to_dict(self._one("SELECT * FROM scan_sessions WHERE id = ?", (scan_id,)))
        if row is not None:
            row["options"] = json_loads(row.pop("options_json", None), {})
        return row

    def list_scans(self, limit: int = 50) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self._query("SELECT * FROM scan_sessions ORDER BY id DESC LIMIT ?", (limit,))
        )

    def resumable_scans(self) -> list[dict[str, Any]]:
        """Sessions left ``running`` by a crash — candidates for resume."""
        return rows_to_dicts(
            self._query("SELECT * FROM scan_sessions WHERE state = 'running' ORDER BY id")
        )

    # ── analysis tasks ──────────────────────────────────────────────────────

    def enqueue(
        self,
        asset_id: int,
        plugin_id: str,
        plugin_version: str,
        *,
        priority: int = 100,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Queue one asset for one plugin version. Idempotent."""

        def _op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO analysis_tasks"
                "(asset_id,plugin_id,plugin_version,state,priority,scheduled_at) "
                "VALUES (?,?,?,'pending',?,?) "
                "ON CONFLICT(asset_id,plugin_id,plugin_version) DO NOTHING",
                (asset_id, plugin_id, plugin_version, priority, utcnow_iso()),
            )

        self._write(_op, conn, label="enqueue")

    def enqueue_many(
        self,
        asset_ids: Sequence[int],
        plugin_id: str,
        plugin_version: str,
        *,
        priority: int = 100,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Bulk enqueue. Returns rows actually inserted."""
        if not asset_ids:
            return 0
        now = utcnow_iso()
        rows = [(a, plugin_id, plugin_version, priority, now) for a in asset_ids]

        def _op(c: sqlite3.Connection) -> int:
            before = int(c.execute("SELECT COUNT(*) FROM analysis_tasks").fetchone()[0])
            c.executemany(
                "INSERT INTO analysis_tasks"
                "(asset_id,plugin_id,plugin_version,state,priority,scheduled_at) "
                "VALUES (?,?,?,'pending',?,?) "
                "ON CONFLICT(asset_id,plugin_id,plugin_version) DO NOTHING",
                rows,
            )
            after = int(c.execute("SELECT COUNT(*) FROM analysis_tasks").fetchone()[0])
            return after - before

        return self._write(_op, conn, label="enqueue_many")

    def claim(
        self,
        *,
        plugin_ids: Sequence[str] | None = None,
        limit: int = 1,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically take up to ``limit`` pending tasks and mark them running.

        Selection and update happen in one transaction on the single writer
        thread, so two workers can never claim the same task.
        """

        def _op(c: sqlite3.Connection) -> list[dict[str, Any]]:
            clauses = ["state = 'pending'"]
            params: list[Any] = []
            if plugin_ids:
                clauses.append(f"plugin_id IN ({placeholders(len(plugin_ids))})")
                params.extend(plugin_ids)
            params.append(limit)
            rows = c.execute(
                "SELECT t.*, a.media_type, a.content_hash FROM analysis_tasks t "
                "JOIN assets a ON a.id = t.asset_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY t.priority, t.id LIMIT ?",
                params,
            ).fetchall()
            if not rows:
                return []
            now = utcnow_iso()
            ids = [int(r["id"]) for r in rows]
            c.executemany(
                "UPDATE analysis_tasks SET state='running', started_at=?, heartbeat_at=?, "
                "attempts=attempts+1 WHERE id=?",
                [(now, now, i) for i in ids],
            )
            return rows_to_dicts(rows)

        return self._write(_op, conn, label="claim_tasks")

    def heartbeat(
        self, task_ids: Sequence[int], *, conn: sqlite3.Connection | None = None
    ) -> None:
        """Prove a long-running task is still alive so it is not reclaimed."""
        if not task_ids:
            return
        now = utcnow_iso()
        self._write(
            lambda c: c.executemany(
                "UPDATE analysis_tasks SET heartbeat_at=? WHERE id=?",
                [(now, t) for t in task_ids],
            ),
            conn,
            label="heartbeat",
        )

    def complete(
        self,
        task_id: int,
        state: TaskState | str = TaskState.DONE,
        *,
        error: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._write(
            lambda c: self._update(
                c,
                "analysis_tasks",
                {"state": str(state), "finished_at": utcnow_iso(), "last_error": error},
                "id = ?",
                (task_id,),
            ),
            conn,
            label="complete_task",
        )

    def retry_later(
        self, task_id: int, error: str, *, conn: sqlite3.Connection | None = None
    ) -> None:
        """Return a task to ``pending`` after a transient failure."""
        self._write(
            lambda c: self._update(
                c,
                "analysis_tasks",
                {"state": "pending", "last_error": error[:2000], "heartbeat_at": None},
                "id = ?",
                (task_id,),
            ),
            conn,
            label="retry_task",
        )

    def requeue_stale(
        self, older_than_iso: str, *, conn: sqlite3.Connection | None = None
    ) -> int:
        """Reclaim tasks stuck in ``running`` by a process that died.

        Called once at startup. This is the difference between "resumable" and
        "resumable unless you kill -9 at the wrong moment".
        """

        def _op(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "UPDATE analysis_tasks SET state='pending', heartbeat_at=NULL "
                "WHERE state='running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                (older_than_iso,),
            )
            return cur.rowcount

        return self._write(_op, conn, label="requeue_stale")

    def invalidate_plugin_version(
        self, plugin_id: str, keep_version: str, *, conn: sqlite3.Connection | None = None
    ) -> int:
        """Drop queued tasks for superseded versions of a plugin.

        Completed rows are kept: they are the record of what ran. Only pending
        and failed work for the old version is pointless once a new version
        exists, so it goes.
        """

        def _op(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "DELETE FROM analysis_tasks WHERE plugin_id = ? AND plugin_version <> ? "
                "AND state IN ('pending','failed','running')",
                (plugin_id, keep_version),
            )
            return cur.rowcount

        return self._write(_op, conn, label="invalidate_plugin_version")

    def clear_plugin_tasks(
        self, plugin_id: str, *, conn: sqlite3.Connection | None = None
    ) -> int:
        def _op(c: sqlite3.Connection) -> int:
            cur = c.execute("DELETE FROM analysis_tasks WHERE plugin_id = ?", (plugin_id,))
            return cur.rowcount

        return self._write(_op, conn, label="clear_plugin_tasks")

    def reset_failed(
        self, plugin_id: str | None = None, *, conn: sqlite3.Connection | None = None
    ) -> int:
        """Give failed tasks another chance, resetting the attempt counter."""

        def _op(c: sqlite3.Connection) -> int:
            if plugin_id:
                cur = c.execute(
                    "UPDATE analysis_tasks SET state='pending', attempts=0, last_error=NULL "
                    "WHERE state='failed' AND plugin_id=?",
                    (plugin_id,),
                )
            else:
                cur = c.execute(
                    "UPDATE analysis_tasks SET state='pending', attempts=0, last_error=NULL "
                    "WHERE state='failed'"
                )
            return cur.rowcount

        return self._write(_op, conn, label="reset_failed")

    def pending_count(self, plugin_id: str | None = None) -> int:
        if plugin_id:
            return int(
                self._scalar(
                    "SELECT COUNT(*) FROM analysis_tasks WHERE state='pending' AND plugin_id=?",
                    (plugin_id,),
                )
                or 0
            )
        return int(
            self._scalar("SELECT COUNT(*) FROM analysis_tasks WHERE state='pending'") or 0
        )

    def counts_by_plugin(self) -> dict[str, dict[str, int]]:
        """``{plugin_id: {state: count}}`` — what ``GET /api/plugins`` reports."""
        rows = self._query(
            "SELECT plugin_id, state, COUNT(*) AS n FROM analysis_tasks GROUP BY plugin_id, state"
        )
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(str(row["plugin_id"]), {})[str(row["state"])] = int(row["n"])
        return out

    def counts_by_state(self) -> dict[str, int]:
        rows = self._query("SELECT state, COUNT(*) AS n FROM analysis_tasks GROUP BY state")
        return {str(r["state"]): int(r["n"]) for r in rows}

    def failed_tasks(self, limit: int = 100, plugin_id: str | None = None) -> list[dict[str, Any]]:
        if plugin_id:
            rows = self._query(
                "SELECT * FROM analysis_tasks WHERE state='failed' AND plugin_id=? "
                "ORDER BY finished_at DESC LIMIT ?",
                (plugin_id, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM analysis_tasks WHERE state='failed' "
                "ORDER BY finished_at DESC LIMIT ?",
                (limit,),
            )
        return rows_to_dicts(rows)

    def completed_plugins_for_asset(self, asset_id: int) -> set[str]:
        """Which plugins have finished this asset. Drives dependency ordering."""
        rows = self._query(
            "SELECT DISTINCT plugin_id FROM analysis_tasks WHERE asset_id=? AND state='done'",
            (asset_id,),
        )
        return {str(r["plugin_id"]) for r in rows}

    # ── errors ──────────────────────────────────────────────────────────────

    def record_error(
        self,
        kind: str,
        message: str,
        *,
        scope: str | None = None,
        ref_id: int | None = None,
        ref_text: str | None = None,
        traceback: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        return self._write(
            lambda c: self._insert(
                c,
                "errors",
                {
                    "scope": scope,
                    "ref_id": ref_id,
                    "ref_text": (ref_text or "")[:1024] or None,
                    "kind": kind,
                    "message": (message or "")[:4000],
                    "traceback": (traceback or "")[:16000] or None,
                    "occurred_at": utcnow_iso(),
                },
            ),
            conn,
            label="record_error",
        )

    def list_errors(
        self, limit: int = 100, *, scope: str | None = None, since: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if since:
            clauses.append("occurred_at >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return rows_to_dicts(
            self._query(
                f"SELECT * FROM errors{where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                params,
            )
        )

    def clear_errors(self, *, before: str | None = None, conn: sqlite3.Connection | None = None) -> int:
        def _op(c: sqlite3.Connection) -> int:
            cur = (
                c.execute("DELETE FROM errors WHERE occurred_at < ?", (before,))
                if before
                else c.execute("DELETE FROM errors")
            )
            return cur.rowcount

        return self._write(_op, conn, label="clear_errors")

    # ── plugin registry ─────────────────────────────────────────────────────

    def record_plugin(
        self,
        plugin_id: str,
        version: str,
        *,
        transport: str,
        enabled: bool,
        accepts: Sequence[str],
        emits: Sequence[str],
        depends_on: Sequence[str],
        capabilities: dict[str, Any],
        config_hash: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str | None:
        """Persist plugin state; return the previous version if it changed.

        A non-None return is the signal to invalidate old tasks and re-enqueue,
        which is how "bump the version and the library re-analyzes" works
        across restarts rather than only within one process.
        """
        now = utcnow_iso()

        def _op(c: sqlite3.Connection) -> str | None:
            row = c.execute(
                "SELECT version, config_hash FROM plugin_registry WHERE plugin_id = ?",
                (plugin_id,),
            ).fetchone()
            previous: str | None = None
            if row is not None and (
                str(row["version"]) != version or (row["config_hash"] or "") != (config_hash or "")
            ):
                previous = str(row["version"])

            self._upsert(
                c,
                "plugin_registry",
                {
                    "plugin_id": plugin_id,
                    "version": version,
                    "transport": transport,
                    "enabled": int(enabled),
                    "accepts_json": json_dumps(list(accepts)),
                    "emits_json": json_dumps(list(emits)),
                    "depends_json": json_dumps(list(depends_on)),
                    "capabilities_json": json_dumps(capabilities),
                    "config_hash": config_hash,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "present": 1,
                },
                ["plugin_id"],
                update_cols=[
                    "version", "transport", "enabled", "accepts_json", "emits_json",
                    "depends_json", "capabilities_json", "config_hash", "last_seen_at", "present",
                ],
            )
            return previous

        return self._write(_op, conn, label="record_plugin")

    def mark_plugins_absent(
        self, present_ids: Sequence[str], *, conn: sqlite3.Connection | None = None
    ) -> int:
        """Flag plugins that were installed once but are gone now."""

        def _op(c: sqlite3.Connection) -> int:
            if present_ids:
                cur = c.execute(
                    "UPDATE plugin_registry SET present=0 WHERE present=1 AND plugin_id NOT IN "
                    f"({placeholders(len(present_ids))})",
                    list(present_ids),
                )
            else:
                cur = c.execute("UPDATE plugin_registry SET present=0 WHERE present=1")
            return cur.rowcount

        return self._write(_op, conn, label="mark_plugins_absent")

    def list_plugins(self) -> list[dict[str, Any]]:
        rows = rows_to_dicts(self._query("SELECT * FROM plugin_registry ORDER BY plugin_id"))
        for row in rows:
            row["accepts"] = json_loads(row.pop("accepts_json", None), [])
            row["emits"] = json_loads(row.pop("emits_json", None), [])
            row["depends_on"] = json_loads(row.pop("depends_json", None), [])
            row["capabilities"] = json_loads(row.pop("capabilities_json", None), {})
            row["enabled"] = bool(row["enabled"])
            row["present"] = bool(row["present"])
        return rows

    def set_plugin_enabled(
        self, plugin_id: str, enabled: bool, *, conn: sqlite3.Connection | None = None
    ) -> None:
        self._write(
            lambda c: self._update(
                c, "plugin_registry", {"enabled": int(enabled)}, "plugin_id = ?", (plugin_id,)
            ),
            conn,
            label="set_plugin_enabled",
        )

    # ── capability grants (audited) ─────────────────────────────────────────

    def log_capability_grant(
        self,
        plugin_id: str,
        capability: str,
        granted: bool,
        *,
        reason: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Write an audit row for a capability decision.

        The spec requires network grants to be logged. Denials are logged too;
        "why did this plugin never run" is the more common question.
        """
        self._write(
            lambda c: self._insert(
                c,
                "capability_grants",
                {
                    "plugin_id": plugin_id,
                    "capability": capability,
                    "granted": int(granted),
                    "reason": reason,
                    "occurred_at": utcnow_iso(),
                },
            ),
            conn,
            label="log_capability_grant",
        )

    def list_capability_grants(self, limit: int = 200) -> list[dict[str, Any]]:
        return rows_to_dicts(
            self._query("SELECT * FROM capability_grants ORDER BY id DESC LIMIT ?", (limit,))
        )

    # ── kv ──────────────────────────────────────────────────────────────────

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        value = self._scalar("SELECT value FROM kv WHERE key = ?", (key,))
        return default if value is None else str(value)

    def kv_set(self, key: str, value: str, *, conn: sqlite3.Connection | None = None) -> None:
        self._write(
            lambda c: self._upsert(
                c, "kv", {"key": key, "value": value, "updated_at": utcnow_iso()}, ["key"]
            ),
            conn,
            label="kv_set",
        )
