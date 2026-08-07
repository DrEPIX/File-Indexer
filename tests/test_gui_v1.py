from __future__ import annotations

from pathlib import Path

import yaml

from mediaengine.config import Config
from mediaengine.gui.app import format_bytes, safe_derivative_path
from mediaengine.gui.config_store import load_desktop_config, save_desktop_config


def test_desktop_config_round_trip_uses_stable_absolute_storage(tmp_path: Path) -> None:
    path = tmp_path / "desktop" / "config.yaml"
    config = load_desktop_config(path)

    assert config.source_path == path.resolve()
    assert config.storage.db_path == path.parent.resolve() / "data" / "library.db"
    assert config.logging.console is False
    assert path.is_file()

    library = tmp_path / "Pictures"
    library.mkdir()
    config.library.roots = [library]
    save_desktop_config(config, path)
    reloaded = load_desktop_config(path)

    assert reloaded.library.roots == [library.resolve()]
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "source_path" not in parsed


def test_derivative_path_cannot_escape_cache(tmp_path: Path) -> None:
    config = Config()
    config.storage.derivatives_path = tmp_path / "cache"
    config.storage.derivatives_path.mkdir()
    thumbnail = config.storage.derivatives_path / "asset" / "thumb.webp"
    thumbnail.parent.mkdir()
    thumbnail.write_bytes(b"preview")

    assert safe_derivative_path(config, "asset/thumb.webp") == thumbnail.resolve()
    assert safe_derivative_path(config, "../outside.webp") is None
    assert safe_derivative_path(config, "missing.webp") is None


def test_format_bytes_is_safe_for_database_and_missing_values() -> None:
    assert format_bytes(1536) == "1.5 KiB"
    assert format_bytes("2048") == "2.0 KiB"
    assert format_bytes(None) == "0 B"
    assert format_bytes(object()) == "—"
