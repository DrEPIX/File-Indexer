"""Moving and erasing the engine's own data.

Two operations users ask for in the same breath — "keep my index on the big
drive" and "wipe it and start over" — which share one dangerous property: both
address files by path, inside a program whose first principle is that
originals are read-only. So every path here is checked against the library
roots before it is moved or deleted, a relocation that fails halfway puts back
what it already moved, and nothing in this module ever touches a file the user
gave us to index.

Both operations require a closed engine. SQLite holds the database open; on
Windows moving it under a live connection fails outright, and on POSIX it
succeeds in the worse way — the write-ahead log is left behind and every
transaction still sitting in it is lost.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config import Config, save_config
from .errors import ConfigError
from .util import human_bytes, setup_logging

__all__ = [
    "DB_SUFFIXES",
    "RelocationMode",
    "RelocationPlan",
    "RelocationReport",
    "ResetReport",
    "StorageUsage",
    "describe_storage",
    "is_temporary_location",
    "master_reset",
    "plan_relocation",
    "relocate_storage",
    "storage_home",
]

_LOG = logging.getLogger(__name__)

#: What SQLite keeps beside the database file itself. Moving ``library.db``
#: without its write-ahead log silently discards committed transactions, so
#: these travel together or not at all.
DB_SUFFIXES: tuple[str, ...] = ("", "-wal", "-shm", "-journal")

#: ``move`` relocates the existing index; ``adopt`` leaves it where it is and
#: points the configuration at a library that already lives somewhere else.
RelocationMode = Literal["move", "adopt"]


def storage_home(config: Config) -> Path:
    """The folder a user thinks of as "where my library is kept".

    The engine addresses the database and the derivative cache separately, but
    a person relocating their index means one folder holding both. This is the
    directory the desktop surfaces, and the anchor everything else moves into.
    """
    return config.storage.db_path.parent


def is_temporary_location(path: Path) -> bool:
    """Whether a path sits somewhere the OS may clear without warning.

    Installers and smoke tests happily point a config at ``%TEMP%``; when that
    leaks into a real install the symptom is a library that empties itself, so
    this is checked rather than assumed.
    """
    import tempfile

    markers = {tempfile.gettempdir().lower(), "\\temp\\", "/tmp/"}
    text = str(path).lower()
    return any(marker and marker in text for marker in markers)


def _tree_bytes(path: Path) -> int:
    """Bytes used by a directory tree, ignoring files that vanish mid-walk."""
    total = 0
    if not path.is_dir():
        return 0
    for current, _dirs, files in os.walk(path, onerror=lambda _exc: None):
        for name in files:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total


def _file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _database_files(db_path: Path) -> list[Path]:
    """Every file that belongs to one SQLite database, in move order."""
    return [Path(str(db_path) + suffix) for suffix in DB_SUFFIXES]


def _log_files(config: Config) -> list[Path]:
    """The active log plus whatever the rotating handler has kept."""
    target = config.logging.file
    if target is None:
        return []
    found = [target] if target.is_file() else []
    parent = target.parent
    if parent.is_dir():
        found.extend(sorted(parent.glob(target.name + ".*")))
    return found


def _inside(child: Path, parent: Path) -> bool:
    """Whether ``child`` is ``parent`` or lives underneath it."""
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


@contextmanager
def _log_file_released(config: Config) -> Iterator[None]:
    """Stop logging to our own file for the duration of the block.

    Windows refuses to move or delete a file another handle has open, and the
    engine's rotating log handler is exactly such a handle — held by this very
    process. Without this, relocating a library fails on the log file after the
    database has already moved, and a reset leaves the log behind. Logging is
    restored afterwards at whatever path the configuration now names.
    """
    root = logging.getLogger()
    ours = {str(item.resolve()) for item in _log_files(config)}
    detached = [
        handler
        for handler in list(root.handlers)
        if isinstance(handler, logging.FileHandler)
        and str(Path(handler.baseFilename).resolve()) in ours
    ]
    for handler in detached:
        root.removeHandler(handler)
        handler.close()
    try:
        yield
    finally:
        if detached and config.logging.file is not None:
            setup_logging(
                level=config.logging.level,
                fmt=config.logging.format,
                file=config.logging.file,
                max_bytes=config.logging.max_bytes,
                backup_count=config.logging.backup_count,
                console=config.logging.console,
            )


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """Where an installation keeps its own data, and how much of it there is."""

    home: Path
    db_path: Path
    derivatives_path: Path
    config_path: Path | None
    log_path: Path | None
    db_bytes: int
    derivatives_bytes: int
    log_bytes: int
    exists: bool
    temporary: bool
    #: Free space on the volume the library currently lives on, or ``None``
    #: when the volume cannot be queried (a disconnected network share).
    free_bytes: int | None

    @property
    def total_bytes(self) -> int:
        return self.db_bytes + self.derivatives_bytes + self.log_bytes

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe view, for the CLI's ``--json`` and the desktop."""
        return {
            "home": str(self.home),
            "db_path": str(self.db_path),
            "derivatives_path": str(self.derivatives_path),
            "config_path": str(self.config_path) if self.config_path else None,
            "log_path": str(self.log_path) if self.log_path else None,
            "db_bytes": self.db_bytes,
            "derivatives_bytes": self.derivatives_bytes,
            "log_bytes": self.log_bytes,
            "total_bytes": self.total_bytes,
            "total_human": human_bytes(self.total_bytes),
            "free_bytes": self.free_bytes,
            "exists": self.exists,
            "temporary": self.temporary,
        }


def _nearest_existing(path: Path) -> Path:
    """The closest ancestor that exists, so a free-space check never raises."""
    current = path.resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def describe_storage(config: Config) -> StorageUsage:
    """Measure the current library location without opening the database."""
    db_path = config.storage.db_path
    derivatives = config.storage.derivatives_path
    db_bytes = sum(_file_bytes(item) for item in _database_files(db_path))
    try:
        free: int | None = shutil.disk_usage(_nearest_existing(db_path.parent)).free
    except OSError:
        free = None
    return StorageUsage(
        home=storage_home(config),
        db_path=db_path,
        derivatives_path=derivatives,
        config_path=config.source_path,
        log_path=config.logging.file,
        db_bytes=db_bytes,
        derivatives_bytes=_tree_bytes(derivatives),
        log_bytes=sum(_file_bytes(item) for item in _log_files(config)),
        exists=db_path.is_file(),
        temporary=is_temporary_location(db_path),
        free_bytes=free,
    )


@dataclass(frozen=True, slots=True)
class RelocationPlan:
    """What a relocation would do, computed before anything is touched."""

    mode: RelocationMode
    source_home: Path
    destination: Path
    db_path: Path
    derivatives_path: Path
    log_path: Path | None
    #: ``(source, destination)`` pairs, in the order they will be moved.
    moves: tuple[tuple[Path, Path], ...]
    bytes_to_move: int
    #: True when the destination already holds a database this plan will adopt.
    adopts_existing: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "source_home": str(self.source_home),
            "destination": str(self.destination),
            "db_path": str(self.db_path),
            "derivatives_path": str(self.derivatives_path),
            "log_path": str(self.log_path) if self.log_path else None,
            "moves": [[str(src), str(dst)] for src, dst in self.moves],
            "bytes_to_move": self.bytes_to_move,
            "bytes_human": human_bytes(self.bytes_to_move),
            "adopts_existing": self.adopts_existing,
        }


def plan_relocation(
    config: Config, destination: str | os.PathLike[str], *, mode: RelocationMode = "move"
) -> RelocationPlan:
    """Validate a new library location and describe the work it implies.

    Planning is separate from doing so the desktop can confirm with real
    numbers rather than a vague warning, and so every refusal happens before
    the first byte moves rather than halfway through.
    """
    target = Path(destination).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = Path(os.path.normpath(str(target)))
    source_home = storage_home(config)
    db_name = config.storage.db_path.name
    derivatives_name = config.storage.derivatives_path.name

    if target.exists() and not target.is_dir():
        raise ConfigError(f"{target} is a file, not a folder")
    for root in config.library.roots:
        if _inside(target, root):
            raise ConfigError(
                f"{target} is inside the library folder {root}. The engine would index its own "
                "previews on the next scan; choose a folder outside every library folder."
            )
    if _inside(target, config.storage.derivatives_path):
        raise ConfigError(f"{target} is inside the current preview cache; choose another folder")
    if target == source_home:
        raise ConfigError(f"the library is already stored in {target}")

    new_db = target / db_name
    new_derivatives = target / derivatives_name
    existing = new_db.is_file()

    moves: list[tuple[Path, Path]] = []
    if mode == "move":
        if existing:
            raise ConfigError(
                f"{new_db} already exists. Choose an empty folder, or switch to the library "
                "already stored there instead of moving this one on top of it."
            )
        if new_derivatives.is_dir() and any(new_derivatives.iterdir()):
            raise ConfigError(f"{new_derivatives} already contains files; choose an empty folder")
        for item in _database_files(config.storage.db_path):
            if item.is_file():
                moves.append((item, target / item.name))
        if config.storage.derivatives_path.is_dir():
            moves.append((config.storage.derivatives_path, new_derivatives))
    elif mode != "adopt":  # pragma: no cover - Literal keeps this unreachable
        raise ConfigError(f"unknown relocation mode {mode!r}")

    log_path = config.logging.file
    new_log: Path | None = log_path
    if log_path is not None and _inside(log_path, source_home):
        new_log = target / log_path.name
        if mode == "move" and log_path.is_file():
            moves.append((log_path, new_log))

    return RelocationPlan(
        mode=mode,
        source_home=source_home,
        destination=target,
        db_path=new_db,
        derivatives_path=new_derivatives,
        log_path=new_log,
        moves=tuple(moves),
        bytes_to_move=sum(
            _tree_bytes(src) if src.is_dir() else _file_bytes(src) for src, _ in moves
        ),
        adopts_existing=existing,
    )


@dataclass(frozen=True, slots=True)
class RelocationReport:
    """What a relocation actually did."""

    mode: RelocationMode
    source_home: Path
    destination: Path
    db_path: Path
    derivatives_path: Path
    moved: tuple[Path, ...]
    bytes_moved: int
    config_saved: bool
    adopted_existing: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "source_home": str(self.source_home),
            "destination": str(self.destination),
            "db_path": str(self.db_path),
            "derivatives_path": str(self.derivatives_path),
            "moved": [str(item) for item in self.moved],
            "bytes_moved": self.bytes_moved,
            "bytes_human": human_bytes(self.bytes_moved),
            "config_saved": self.config_saved,
            "adopted_existing": self.adopted_existing,
        }


def relocate_storage(
    config: Config,
    destination: str | os.PathLike[str],
    *,
    mode: RelocationMode = "move",
    save: bool = True,
) -> RelocationReport:
    """Point the configuration at a new library folder, moving data if asked.

    The engine must already be closed. ``mode="move"`` carries the index,
    preview cache and log across; ``mode="adopt"`` changes nothing on disk and
    simply starts using whatever library lives at ``destination`` — which is
    how a user switches between two libraries, or reconnects to one that was
    moved by hand.

    A failure part-way through moves back everything already moved: a library
    whose database landed on the new drive while its previews stayed on the old
    one is worse than either outcome alone.
    """
    plan = plan_relocation(config, destination, mode=mode)
    plan.destination.mkdir(parents=True, exist_ok=True)
    if not os.access(plan.destination, os.W_OK):
        raise ConfigError(f"{plan.destination} is not writable")
    if plan.bytes_to_move:
        try:
            free = shutil.disk_usage(plan.destination).free
        except OSError:  # pragma: no cover - unqueryable volume
            free = -1
        if 0 <= free < plan.bytes_to_move:
            raise ConfigError(
                f"{plan.destination} has {human_bytes(free)} free but the library needs "
                f"{human_bytes(plan.bytes_to_move)}"
            )

    done: list[tuple[Path, Path]] = []
    with _log_file_released(config):
        try:
            for source, target in plan.moves:
                shutil.move(str(source), str(target))
                done.append((source, target))
        except (OSError, shutil.Error) as exc:
            for source, target in reversed(done):
                try:
                    shutil.move(str(target), str(source))
                except (OSError, shutil.Error):  # pragma: no cover - best effort
                    _LOG.error("could not roll back %s -> %s", target, source)
            raise ConfigError(f"could not move the library to {plan.destination}: {exc}") from exc

        config.storage.db_path = plan.db_path
        config.storage.derivatives_path = plan.derivatives_path
        if plan.log_path is not None:
            config.logging.file = plan.log_path
    saved = False
    if save and config.source_path is not None:
        save_config(config)
        saved = True
    _LOG.info("library relocated to %s (%s)", plan.destination, plan.mode)
    return RelocationReport(
        mode=plan.mode,
        source_home=plan.source_home,
        destination=plan.destination,
        db_path=plan.db_path,
        derivatives_path=plan.derivatives_path,
        moved=tuple(target for _, target in done),
        bytes_moved=plan.bytes_to_move,
        config_saved=saved,
        adopted_existing=plan.adopts_existing,
    )


def _refuse_reason(path: Path, config: Config) -> str | None:
    """Why deleting ``path`` would be unsafe, or ``None`` when it is fine.

    The checks are deliberately blunt. A reset is the one operation where a
    path bug destroys the data the engine exists to protect, so anything that
    even neighbours the user's originals is refused rather than reasoned about.
    """
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - unreadable mount
        return "cannot be resolved"
    if not resolved.is_absolute():
        return "is not an absolute path"
    if resolved == Path(resolved.anchor):
        return "is a drive root"
    if resolved == Path.home():
        return "is your home folder"
    for root in config.library.roots:
        if _inside(resolved, root):
            return f"is inside the library folder {root}"
        if _inside(root, resolved):
            return f"contains the library folder {root}"
    return None


@dataclass(frozen=True, slots=True)
class ResetReport:
    """What a master reset removed, and what it refused to touch."""

    removed: tuple[Path, ...]
    skipped: tuple[tuple[Path, str], ...]
    bytes_freed: int
    roots_cleared: int
    #: ``reset`` (defaults written back), ``deleted``, or ``unsaved``.
    config: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "removed": [str(item) for item in self.removed],
            "skipped": [[str(item), reason] for item, reason in self.skipped],
            "bytes_freed": self.bytes_freed,
            "bytes_human": human_bytes(self.bytes_freed),
            "roots_cleared": self.roots_cleared,
            "config": self.config,
        }


def _restore_defaults(config: Config) -> None:
    """Rewrite ``config`` in place as a first-run configuration.

    Everything describing *where* this installation keeps its data survives —
    storage paths, log destination, and the directories plugins and filter
    packs are discovered from, which a packaged build sets once at install time
    and cannot rediscover on its own. Everything describing *what the user did*
    is dropped.
    """
    fresh = Config()
    fresh.storage = config.storage
    fresh.logging = config.logging
    fresh.plugins.directories = list(config.plugins.directories)
    fresh.plugins.filter_pack_dirs = list(config.plugins.filter_pack_dirs)
    config.library = fresh.library
    config.scan = fresh.scan
    config.workers = fresh.workers
    config.plugins = fresh.plugins
    config.search = fresh.search
    config.api = fresh.api


def master_reset(
    config: Config,
    *,
    delete_config: bool = False,
    delete_filter_packs: bool = False,
    save: bool = True,
) -> ResetReport:
    """Erase the index, the preview cache, the logs, and every setting.

    This is the "start over as if freshly installed" operation: afterwards
    nothing derived survives — not annotations, not user labels, not face
    clusters, not the scan history. **Original files are never touched**, and
    :func:`_refuse_reason` makes that structural rather than aspirational: any
    target sitting inside, or containing, a library folder is skipped and
    reported instead of deleted.

    Where the library lives is preserved. Someone who deliberately moved their
    index onto a fast drive does not want a reset to drag it back into
    ``%LOCALAPPDATA%``; they want an empty library in the place they chose.

    The engine must already be closed.
    """
    removed: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    freed = 0

    files: list[Path] = _database_files(config.storage.db_path)
    files.extend(_log_files(config))
    directories: list[Path] = [config.storage.derivatives_path]
    if delete_filter_packs:
        from .filters import user_pack_dir

        directories.append(user_pack_dir(config))

    with _log_file_released(config):
        for item in files:
            reason = _refuse_reason(item, config)
            if reason is not None:
                skipped.append((item, reason))
                continue
            if not item.is_file():
                continue
            size = _file_bytes(item)
            try:
                item.unlink()
            except OSError as exc:
                skipped.append((item, str(exc)))
                continue
            removed.append(item)
            freed += size

        for directory in directories:
            reason = _refuse_reason(directory, config)
            if reason is not None:
                skipped.append((directory, reason))
                continue
            if not directory.is_dir():
                continue
            size = _tree_bytes(directory)
            # `onexc` would report every individual failure but only exists on
            # 3.12+, and a partly-deleted cache is reported the same way either
            # way: the tree is still there, and the message says why.
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                skipped.append((directory, str(exc)))
                continue
            removed.append(directory)
            freed += size

    roots_cleared = len(config.library.roots)
    state = "unsaved"
    if delete_config and config.source_path is not None:
        source = config.source_path
        reason = _refuse_reason(source, config)
        if reason is not None:
            skipped.append((source, reason))
        else:
            try:
                source.unlink(missing_ok=True)
                removed.append(source)
                state = "deleted"
            except OSError as exc:
                skipped.append((source, str(exc)))
    if state != "deleted":
        _restore_defaults(config)
        if save and config.source_path is not None:
            save_config(config)
            state = "reset"

    _LOG.warning("master reset removed %d item(s), freeing %s", len(removed), human_bytes(freed))
    return ResetReport(
        removed=tuple(removed),
        skipped=tuple(skipped),
        bytes_freed=freed,
        roots_cleared=roots_cleared,
        config=state,
    )
