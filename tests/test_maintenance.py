"""Relocating and erasing an installation's own data.

The interesting cases are all failure cases: a destination inside the library,
a half-finished move, and a reset asked to delete a folder that turns out to
hold the user's originals.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from mediaengine.config import Config, load_config
from mediaengine.engine import MediaEngine
from mediaengine.errors import ConfigError
from mediaengine.maintenance import (
    describe_storage,
    is_temporary_location,
    master_reset,
    plan_relocation,
    relocate_storage,
    storage_home,
)

from .conftest import make_config


def _populate(config: Config) -> None:
    """Give a config real files to move: a database, a cache, and a log."""
    config.storage.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.storage.db_path.write_bytes(b"SQLite format 3\x00" + b"0" * 512)
    Path(str(config.storage.db_path) + "-wal").write_bytes(b"w" * 128)
    cache = config.storage.derivatives_path / "ab" / "cd"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "thumb.webp").write_bytes(b"t" * 64)
    if config.logging.file is not None:
        config.logging.file.parent.mkdir(parents=True, exist_ok=True)
        config.logging.file.write_text("started\n", encoding="utf-8")


def _saved_config(tmp_path: Path, **overrides: object) -> Config:
    config = make_config(tmp_path, **overrides)
    config.source_path = tmp_path / "config.yaml"
    config.logging.file = tmp_path / "data" / "engine.log"
    return config


# ── describing where things are ──────────────────────────────────────────────


def test_storage_usage_counts_the_database_wal_and_cache(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    _populate(config)
    usage = describe_storage(config)
    assert usage.home == storage_home(config) == config.storage.db_path.parent
    db = config.storage.db_path
    # The write-ahead log counts: a database moved without it loses whatever
    # was still in it.
    assert usage.db_bytes == db.stat().st_size + Path(str(db) + "-wal").stat().st_size
    assert usage.derivatives_bytes == 64
    assert config.logging.file is not None
    assert usage.log_bytes == config.logging.file.stat().st_size
    assert usage.total_bytes == usage.db_bytes + usage.derivatives_bytes + usage.log_bytes
    assert usage.exists is True
    assert usage.as_dict()["total_human"]


def test_storage_usage_is_readable_before_the_first_scan(tmp_path: Path) -> None:
    """The settings screen opens before any library exists; it must not raise."""
    usage = describe_storage(_saved_config(tmp_path))
    assert usage.exists is False
    assert usage.total_bytes == 0
    assert usage.free_bytes is None or usage.free_bytes >= 0


def test_a_temporary_library_location_is_flagged() -> None:
    import tempfile

    assert is_temporary_location(Path(tempfile.gettempdir()) / "File Indexer" / "library.db")
    assert not is_temporary_location(Path.home() / "Pictures" / "library.db")


# ── planning a move ──────────────────────────────────────────────────────────


def test_a_plan_names_every_file_it_would_move(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    _populate(config)
    plan = plan_relocation(config, tmp_path / "elsewhere")
    sources = {source.name for source, _ in plan.moves}
    assert {"library.db", "library.db-wal", "derivatives", "engine.log"} <= sources
    assert plan.bytes_to_move == describe_storage(config).total_bytes
    assert plan.db_path == tmp_path / "elsewhere" / "library.db"
    assert plan.as_dict()["mode"] == "move"


def test_the_destination_may_not_sit_inside_a_library_folder(tmp_path: Path) -> None:
    """Otherwise the next scan indexes the engine's own previews."""
    root = tmp_path / "photos"
    root.mkdir()
    config = _saved_config(tmp_path, library={"roots": [str(root)]})
    with pytest.raises(ConfigError, match="inside the library folder"):
        plan_relocation(config, root / "index")


def test_moving_onto_an_existing_library_is_refused(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    _populate(config)
    other = tmp_path / "other"
    other.mkdir()
    (other / "library.db").write_bytes(b"someone else's index")
    with pytest.raises(ConfigError, match="already exists"):
        plan_relocation(config, other)
    # ...but adopting it is exactly how you switch libraries.
    plan = plan_relocation(config, other, mode="adopt")
    assert plan.adopts_existing and not plan.moves


def test_relocating_to_the_current_folder_is_refused(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    _populate(config)
    with pytest.raises(ConfigError, match="already stored"):
        plan_relocation(config, storage_home(config))


def test_a_file_where_a_folder_belongs_is_refused(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a folder"):
        plan_relocation(config, blocker)


# ── performing a move ────────────────────────────────────────────────────────


def test_a_move_carries_the_index_cache_and_log_then_saves(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    _populate(config)
    report = relocate_storage(config, tmp_path / "library-drive")

    assert report.config_saved is True
    assert (tmp_path / "library-drive" / "library.db").is_file()
    assert (tmp_path / "library-drive" / "library.db-wal").is_file()
    assert (tmp_path / "library-drive" / "derivatives" / "ab" / "cd" / "thumb.webp").is_file()
    assert not (tmp_path / "data" / "library.db").exists()
    assert config.storage.db_path == tmp_path / "library-drive" / "library.db"
    # The saved file is what the next launch reads, so it must agree.
    assert load_config(tmp_path / "config.yaml").storage.db_path == config.storage.db_path


def test_adopting_leaves_the_old_library_untouched(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    _populate(config)
    existing = tmp_path / "second-library"
    existing.mkdir()
    (existing / "library.db").write_bytes(b"SQLite format 3\x00")

    report = relocate_storage(config, existing, mode="adopt")

    assert report.adopted_existing and not report.moved
    assert (tmp_path / "data" / "library.db").is_file(), "the original library must survive"
    assert config.storage.db_path == existing / "library.db"


def test_a_failed_move_puts_back_what_it_already_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database on the new drive and previews on the old one is the worst outcome."""
    config = _saved_config(tmp_path)
    _populate(config)
    original_db = config.storage.db_path
    real_move = shutil.move

    def explode(src: str, dst: str) -> object:
        if src.endswith("derivatives"):
            raise OSError(28, "No space left on device")
        return real_move(src, dst)

    monkeypatch.setattr(shutil, "move", explode)
    with pytest.raises(ConfigError, match="could not move the library"):
        relocate_storage(config, tmp_path / "half-way")

    assert original_db.is_file(), "the database must be back where it started"
    assert Path(str(original_db) + "-wal").is_file()
    assert config.storage.db_path == original_db, "config must not point at a half-move"


def test_a_move_refuses_when_the_destination_cannot_hold_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _saved_config(tmp_path)
    _populate(config)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: shutil._ntuple_diskusage(100, 99, 1)  # type: ignore[attr-defined]
    )
    with pytest.raises(ConfigError, match="free"):
        relocate_storage(config, tmp_path / "tiny-disk")
    assert config.storage.db_path.is_file()


def test_a_relocated_library_still_opens(tmp_path: Path) -> None:
    """The point of the feature: the index keeps working from its new home."""
    config = _saved_config(tmp_path)
    engine = MediaEngine(config)
    engine.start()
    engine.record_note("before", "move")
    engine.close()

    relocate_storage(config, tmp_path / "new-home")

    moved = MediaEngine(config)
    moved.start()
    try:
        assert moved.repos.tasks.kv_get("before") == "move"
    finally:
        moved.close()


# ── master reset ─────────────────────────────────────────────────────────────


def test_master_reset_deletes_the_index_cache_and_logs(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    _populate(config)
    before = describe_storage(config).total_bytes

    report = master_reset(config)

    assert not config.storage.db_path.exists()
    assert not Path(str(config.storage.db_path) + "-wal").exists()
    assert not config.storage.derivatives_path.exists()
    assert config.logging.file is not None and not config.logging.file.exists()
    assert report.bytes_freed == before
    assert report.config == "reset"


def test_master_reset_never_deletes_the_originals(tmp_path: Path) -> None:
    """The cache is deliberately misconfigured to sit over the user's photos."""
    root = tmp_path / "photos"
    (root / "trip").mkdir(parents=True)
    keepsake = root / "trip" / "IMG_0001.JPG"
    keepsake.write_bytes(b"original bytes")
    config = _saved_config(tmp_path, library={"roots": [str(root)]})
    config.storage.derivatives_path = root
    _populate(config)

    report = master_reset(config)

    assert keepsake.read_bytes() == b"original bytes"
    assert root.is_dir()
    assert any("library folder" in reason for _path, reason in report.skipped)
    # Everything that was safe to remove still went.
    assert not config.storage.db_path.exists()


def test_master_reset_forgets_the_library_folders_but_keeps_the_location(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    config = _saved_config(tmp_path, library={"roots": [str(root)]})
    config.storage.db_path = tmp_path / "chosen-drive" / "library.db"
    config.plugins.enabled = ["core.exif-gps", "filters.sport"]
    config.plugins.per_plugin = {"filters.sport": {"model": "x"}}
    _populate(config)

    report = master_reset(config)

    assert report.roots_cleared == 1
    assert config.library.roots == []
    assert "filters.sport" not in config.plugins.enabled
    assert config.plugins.per_plugin == {}
    assert config.storage.db_path == tmp_path / "chosen-drive" / "library.db"
    assert load_config(tmp_path / "config.yaml").library.roots == []


def test_master_reset_keeps_the_paths_a_packaged_build_cannot_rediscover(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    bundled = tmp_path / "app" / "plugins-available"
    bundled.mkdir(parents=True)
    config.plugins.directories = [bundled]
    config.plugins.filter_pack_dirs = [tmp_path / "packs"]

    master_reset(config)

    assert config.plugins.directories == [bundled]
    assert config.plugins.filter_pack_dirs == [tmp_path / "packs"]


def test_master_reset_can_delete_the_config_for_a_true_first_run(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    config.storage.db_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text("library: {}\n", encoding="utf-8")
    _populate(config)

    report = master_reset(config, delete_config=True)

    assert report.config == "deleted"
    assert not (tmp_path / "config.yaml").exists()


def test_master_reset_leaves_custom_filter_packs_alone_unless_asked(tmp_path: Path) -> None:
    """A pack someone wrote by hand is their work, not derived data."""
    config = _saved_config(tmp_path)
    packs = tmp_path / "filter-packs"
    packs.mkdir()
    mine = packs / "mine.toml"
    mine.write_text("[pack]\n", encoding="utf-8")
    _populate(config)

    master_reset(config)
    assert mine.is_file()

    master_reset(config, delete_filter_packs=True)
    assert not packs.exists()


def test_master_reset_is_idempotent(tmp_path: Path) -> None:
    """Running it twice — or on a fresh install — must not raise."""
    config = _saved_config(tmp_path)
    _populate(config)
    master_reset(config)
    second = master_reset(config)
    assert second.bytes_freed == 0
    assert second.removed == ()


def test_the_library_reopens_empty_after_a_reset(tmp_path: Path) -> None:
    config = _saved_config(tmp_path)
    engine = MediaEngine(config)
    engine.start()
    engine.record_note("survives", "no")
    engine.close()

    master_reset(config)

    rebuilt = MediaEngine(config)
    rebuilt.start()
    try:
        assert rebuilt.repos.tasks.kv_get("survives") is None
        assert rebuilt.stats()["assets_by_type"] == {}
    finally:
        rebuilt.close()


# ── the log file this very process is holding open ───────────────────────────


def test_a_move_succeeds_while_the_process_is_logging_to_the_library(tmp_path: Path) -> None:
    """Windows refuses to move an open file, and we hold the log open ourselves."""
    from mediaengine.util import setup_logging

    config = _saved_config(tmp_path)
    config.logging.level = "INFO"
    _populate(config)
    setup_logging(level="INFO", fmt="text", file=config.logging.file, console=False)
    try:
        logging.getLogger("test").info("something worth keeping")
        relocate_storage(config, tmp_path / "new-drive")

        assert (tmp_path / "new-drive" / "engine.log").is_file()
        assert config.logging.file == tmp_path / "new-drive" / "engine.log"
        # Logging keeps working, at the new location.
        logging.getLogger("test").info("after the move")
        assert "after the move" in (tmp_path / "new-drive" / "engine.log").read_text(
            encoding="utf-8"
        )
    finally:
        setup_logging(level="ERROR", fmt="text", file=None, console=False)


def test_a_reset_deletes_the_log_it_is_writing_to(tmp_path: Path) -> None:
    from mediaengine.util import setup_logging

    config = _saved_config(tmp_path)
    _populate(config)
    setup_logging(level="INFO", fmt="text", file=config.logging.file, console=False)
    try:
        logging.getLogger("test").info("history")
        report = master_reset(config)

        assert not any("engine.log" in reason for _path, reason in report.skipped)
        assert config.logging.file is not None
        # Logging resumes into a fresh file; what it must not contain is the
        # history the reset was asked to destroy.
        assert "history" not in config.logging.file.read_text(encoding="utf-8")
    finally:
        setup_logging(level="ERROR", fmt="text", file=None, console=False)
