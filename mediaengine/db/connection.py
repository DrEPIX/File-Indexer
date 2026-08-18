"""SQLite connection management and migrations.

Concurrency model, in one paragraph: SQLite in WAL mode allows one writer and
many concurrent readers. Rather than fight ``SQLITE_BUSY`` across a dozen
worker threads, the engine funnels **every** write through a single dedicated
thread (see :mod:`mediaengine.db.writer`) holding one connection. Readers get
their own thread-local connections and never block the writer. This trades a
little write throughput for the near-total elimination of lock contention and
retry logic.

Callers should not construct :class:`sqlite3.Connection` objects themselves.
Use :meth:`Database.read` for queries and :attr:`Database.writer` for changes.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from ..errors import MigrationError
from .writer import Writer

__all__ = ["Database", "MIGRATIONS_DIR", "register_sqlite_functions"]

_LOG = logging.getLogger(__name__)

MIGRATIONS_DIR: Final[Path] = Path(__file__).parent / "migrations"
_MIGRATION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

# Applied to every connection, reader and writer alike.
_COMMON_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-32000",  # 32 MiB per connection
)

# WAL is a database-level property; setting it once is enough, but it is
# idempotent and cheap, so the writer sets it on every open.
_WRITER_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA wal_autocheckpoint=2000",
)

_READER_PRAGMAS: Final[tuple[str, ...]] = ("PRAGMA query_only=ON",)


def _haversine_km(
    lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None
) -> float | None:
    """Great-circle distance in kilometres, registered as a SQL function.

    Used as the post-filter after an R*Tree bounding-box pre-filter, which is
    how radius search works without a real spatial index.
    """
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    from math import asin, cos, radians, sin, sqrt

    r_lat1, r_lat2 = radians(lat1), radians(lat2)
    d_lat = r_lat2 - r_lat1
    d_lon = radians(lon2) - radians(lon1)
    a = sin(d_lat / 2) ** 2 + cos(r_lat1) * cos(r_lat2) * sin(d_lon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(min(1.0, a)))


def _regexp(pattern: str | None, value: str | None) -> bool:
    """``x REGEXP y`` support. Off by default in Python's sqlite3."""
    if pattern is None or value is None:
        return False
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


def register_sqlite_functions(conn: sqlite3.Connection) -> None:
    """Attach the engine's custom SQL functions to a connection."""
    conn.create_function("haversine_km", 4, _haversine_km, deterministic=True)
    conn.create_function("regexp", 2, _regexp, deterministic=True)


class Database:
    """Owns the database file, its migrations, and all connections to it.

    Not a connection pool in the usual sense: readers are thread-local and
    live as long as their thread, which suits a worker-pool architecture where
    threads are long-lived.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        writer_queue_size: int = 10_000,
        load_vec_extension: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout
        self._load_vec = load_vec_extension
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._closed = False
        self.vec_available = False

        self._writer_conn = self._open(readonly=False)
        self.writer = Writer(self._writer_conn, queue_size=writer_queue_size, db_path=self.path)
        self.writer.start()

    # ── connection setup ────────────────────────────────────────────────────

    def _open(self, *, readonly: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=self._timeout,
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,
            detect_types=0,
        )
        conn.row_factory = sqlite3.Row
        for pragma in _COMMON_PRAGMAS:
            conn.execute(pragma)
        for pragma in _WRITER_PRAGMAS if not readonly else _READER_PRAGMAS:
            conn.execute(pragma)
        register_sqlite_functions(conn)
        if self._load_vec:
            self._try_load_vec(conn)
        return conn

    def _try_load_vec(self, conn: sqlite3.Connection) -> None:
        """Best-effort load of sqlite-vec. Absence is not an error."""
        try:
            import sqlite_vec  # type: ignore[import-not-found]
        except ImportError:
            return
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self.vec_available = True
        except Exception as exc:  # extension loading disabled in this build
            _LOG.debug("sqlite-vec unavailable: %s", exc)

    # ── reads ───────────────────────────────────────────────────────────────

    def reader(self) -> sqlite3.Connection:
        """The calling thread's read-only connection, created on first use."""
        if self._closed:
            raise RuntimeError("database is closed")
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open(readonly=True)
            self._local.conn = conn
            with self._readers_lock:
                self._readers.append(conn)
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Context manager yielding this thread's reader connection."""
        yield self.reader()

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        """Run a SELECT and materialise every row."""
        cur = self.reader().execute(sql, params)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        """Run a SELECT and return the first row, or ``None``."""
        cur = self.reader().execute(sql, params)
        try:
            row: sqlite3.Row | None = cur.fetchone()
            return row
        finally:
            cur.close()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        """Run a SELECT and return the first column of the first row."""
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # ── migrations ──────────────────────────────────────────────────────────

    def migrate(self, migrations_dir: Path | None = None) -> int:
        """Apply every pending forward migration. Returns the new version.

        Migrations are numbered SQL files named ``NNNN_description.sql``. Each
        runs inside a transaction; a failure rolls that file back and aborts,
        leaving ``schema_version`` at the last good number.
        """
        directory = migrations_dir or MIGRATIONS_DIR
        migrations = self._discover_migrations(directory)

        def _apply(conn: sqlite3.Connection) -> int:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "  version INTEGER PRIMARY KEY,"
                "  name TEXT NOT NULL,"
                "  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
                ")"
            )
            cur = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
            current = int(cur.fetchone()[0])
            cur.close()

            for version, name, sql_path in migrations:
                if version <= current:
                    continue
                _LOG.info("applying migration %04d_%s", version, name)
                sql = sql_path.read_text(encoding="utf-8")
                # executescript() implicitly commits any open transaction
                # before it runs, so the BEGIN/COMMIT must live *inside* the
                # script text. DDL is transactional in SQLite, which is what
                # makes a failed migration leave no half-built schema behind.
                # `name` is constrained to [a-z0-9_]+ by _MIGRATION_RE, so
                # inlining it into the INSERT is safe.
                script = (
                    "BEGIN;\n"
                    f"{sql}\n"
                    f"INSERT INTO schema_version(version, name) VALUES ({version}, '{name}');\n"
                    "COMMIT;\n"
                )
                try:
                    conn.executescript(script)
                except Exception as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise MigrationError(f"migration {version:04d}_{name} failed: {exc}") from exc
                current = version
            return current

        # executescript implicitly commits, so this cannot be nested inside a
        # transaction the writer opened; run it as an unwrapped job.
        version = self.writer.run_raw(_apply)
        _LOG.debug("schema at version %d", version)
        return version

    @staticmethod
    def _discover_migrations(directory: Path) -> list[tuple[int, str, Path]]:
        if not directory.is_dir():
            raise MigrationError(f"migrations directory not found: {directory}")
        found: list[tuple[int, str, Path]] = []
        seen: dict[int, str] = {}
        for entry in sorted(directory.iterdir()):
            if entry.suffix != ".sql":
                continue
            m = _MIGRATION_RE.match(entry.name)
            if not m:
                raise MigrationError(
                    f"migration filename must match NNNN_name.sql, got {entry.name!r}"
                )
            version, name = int(m.group(1)), m.group(2)
            if version in seen:
                raise MigrationError(
                    f"duplicate migration number {version:04d}: {seen[version]} and {entry.name}"
                )
            seen[version] = entry.name
            found.append((version, name, entry))
        if not found:
            raise MigrationError(f"no migrations found in {directory}")
        return found

    @property
    def schema_version(self) -> int:
        """Current applied schema version, or 0 if the table does not exist."""
        try:
            value = self.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_version")
        except sqlite3.OperationalError:
            return 0
        return int(value or 0)

    # ── maintenance ─────────────────────────────────────────────────────────

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        """Fold the WAL back into the main database file."""
        self.writer.run_raw(lambda c: c.execute(f"PRAGMA wal_checkpoint({mode})").fetchone())

    def optimize(self) -> None:
        """Run ``PRAGMA optimize`` and rebuild FTS internals."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("PRAGMA optimize")
            conn.execute("INSERT INTO search_index(search_index) VALUES('optimize')")

        self.writer.run_raw(_op)

    def vacuum(self) -> None:
        """Rewrite the database file compactly. Blocks all writes."""
        self.writer.run_raw(lambda c: c.execute("VACUUM"))

    def integrity_check(self) -> list[str]:
        """Return SQLite's integrity report; ``["ok"]`` means healthy."""
        return [str(r[0]) for r in self.query("PRAGMA integrity_check")]

    def stats(self) -> dict[str, int]:
        """Row counts for the tables an operator actually asks about."""
        tables = (
            "assets",
            "files",
            "annotations",
            "regions",
            "embeddings",
            "identities",
            "analysis_tasks",
            "errors",
            "producers",
            "derivatives",
        )
        out: dict[str, int] = {}
        for table in tables:
            try:
                out[table] = int(self.scalar(f"SELECT COUNT(*) FROM {table}") or 0)
            except sqlite3.Error:
                out[table] = -1
        try:
            page_size = int(self.scalar("PRAGMA page_size") or 0)
            page_count = int(self.scalar("PRAGMA page_count") or 0)
            out["db_bytes"] = page_size * page_count
        except sqlite3.Error:
            out["db_bytes"] = -1
        return out

    # ── lifecycle ───────────────────────────────────────────────────────────

    def close(self, *, checkpoint: bool = True) -> None:
        """Drain the writer, checkpoint the WAL, close every connection."""
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.stop(drain=True, checkpoint=checkpoint)
        finally:
            with self._readers_lock:
                for conn in self._readers:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                self._readers.clear()
            try:
                self._writer_conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Database {self.path} v{self.schema_version}>"
