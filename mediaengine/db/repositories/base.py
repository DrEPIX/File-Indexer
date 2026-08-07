"""Repository base class.

Every repository method that writes follows the same shape::

    def create(self, ..., conn: sqlite3.Connection | None = None) -> int:
        return self._write(lambda c: self._create(c, ...), conn)

Passing ``conn`` runs the statement inside the caller's already-open
transaction; omitting it submits a job to the writer thread and blocks for the
result. That single convention is what lets ``purge_biometrics`` compose eight
separate repository calls into one atomic transaction without any of them
knowing about the others.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, TypeVar

from ..connection import Database

__all__ = ["Repository", "row_to_dict", "rows_to_dicts", "placeholders", "build_where"]

T = TypeVar("T")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a row to a plain dict, or pass through ``None``."""
    return None if row is None else {k: row[k] for k in row.keys()}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert a row sequence to plain dicts."""
    return [{k: r[k] for k in r.keys()} for r in rows]


def placeholders(n: int) -> str:
    """``"?,?,?"`` for an ``IN`` clause of ``n`` items."""
    return ",".join("?" * n)


def build_where(clauses: Sequence[str], joiner: str = " AND ") -> str:
    """Join non-empty clauses into a ``WHERE`` fragment, or return ``""``."""
    live = [c for c in clauses if c]
    return (" WHERE " + joiner.join(f"({c})" for c in live)) if live else ""


class Repository:
    """Shared plumbing for the concrete repositories."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── write dispatch ──────────────────────────────────────────────────────

    def _write(
        self,
        fn: Callable[[sqlite3.Connection], T],
        conn: sqlite3.Connection | None,
        *,
        label: str = "",
    ) -> T:
        """Run ``fn`` on ``conn`` if given, else through the writer thread."""
        if conn is not None:
            return fn(conn)
        return self.db.writer.run(fn, label=label or type(self).__name__)

    def _submit(
        self, fn: Callable[[sqlite3.Connection], T], *, label: str = ""
    ) -> "Any":  # concurrent.futures.Future[T]
        """Queue a write without waiting for it. Returns a Future."""
        return self.db.writer.submit(fn, label=label or type(self).__name__)

    # ── read helpers ────────────────────────────────────────────────────────

    def _query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
        return self.db.query(sql, params)  # type: ignore[arg-type]

    def _one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> sqlite3.Row | None:
        return self.db.query_one(sql, params)  # type: ignore[arg-type]

    def _scalar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        return self.db.scalar(sql, params)  # type: ignore[arg-type]

    # ── generic statement helpers, used by every subclass ───────────────────

    @staticmethod
    def _insert(
        conn: sqlite3.Connection, table: str, values: Mapping[str, Any], *, or_: str = ""
    ) -> int:
        """INSERT a dict and return the new rowid."""
        cols = list(values)
        verb = f"INSERT OR {or_}" if or_ else "INSERT"
        sql = (
            f"{verb} INTO {table} ({','.join(cols)}) "  # noqa: S608 - table names are literals
            f"VALUES ({placeholders(len(cols))})"
        )
        cur = conn.execute(sql, [values[c] for c in cols])
        try:
            return int(cur.lastrowid or 0)
        finally:
            cur.close()

    @staticmethod
    def _update(
        conn: sqlite3.Connection,
        table: str,
        values: Mapping[str, Any],
        where: str,
        params: Sequence[Any],
    ) -> int:
        """UPDATE with a dict of columns; returns rows affected."""
        if not values:
            return 0
        assignments = ",".join(f"{c}=?" for c in values)
        sql = f"UPDATE {table} SET {assignments} WHERE {where}"  # noqa: S608
        cur = conn.execute(sql, [*values.values(), *params])
        try:
            return cur.rowcount
        finally:
            cur.close()

    @staticmethod
    def _upsert(
        conn: sqlite3.Connection,
        table: str,
        values: Mapping[str, Any],
        conflict: Sequence[str],
        update_cols: Sequence[str] | None = None,
    ) -> int:
        """INSERT … ON CONFLICT DO UPDATE. Returns the affected rowid."""
        cols = list(values)
        updates = list(update_cols) if update_cols is not None else [
            c for c in cols if c not in conflict
        ]
        action = (
            "DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in updates)
            if updates
            else "DO NOTHING"
        )
        sql = (
            f"INSERT INTO {table} ({','.join(cols)}) "  # noqa: S608
            f"VALUES ({placeholders(len(cols))}) "
            f"ON CONFLICT ({','.join(conflict)}) {action}"
        )
        cur = conn.execute(sql, [values[c] for c in cols])
        try:
            return int(cur.lastrowid or 0)
        finally:
            cur.close()
