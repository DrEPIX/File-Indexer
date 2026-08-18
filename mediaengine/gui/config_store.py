"""Persistent desktop configuration.

The CLI intentionally treats the current directory as its default data home.
A desktop shortcut has no dependable current directory, so V1 uses a stable
per-user location instead.  A ``config.yaml`` beside a packaged executable is
still honoured, making portable installs possible without special flags.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from ..config import Config, load_config, save_config

APP_NAME = "File Indexer V1"


def executable_dir() -> Path:
    """Return the directory users perceive as the application directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundled_resource_dir() -> Path:
    """Return PyInstaller's data root or the source checkout root."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root).resolve() if frozen_root else executable_dir()


def user_data_dir() -> Path:
    """Return V1's stable writable directory on the current platform."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_NAME


def desktop_config_path() -> Path:
    """Choose a portable config when present, otherwise the per-user config."""
    portable = executable_dir() / "config.yaml"
    return portable if portable.is_file() else user_data_dir() / "config.yaml"


def _new_config(path: Path) -> Config:
    home = path.parent.resolve()
    config = Config()
    config.storage.db_path = home / "data" / "library.db"
    config.storage.derivatives_path = home / "data" / "derivatives"
    config.logging.file = home / "data" / "engine.log"
    config.logging.format = "text"
    config.logging.console = False

    bundled_plugins = bundled_resource_dir() / "plugins-available"
    if bundled_plugins.is_dir():
        config.plugins.directories = [bundled_plugins]
    config.source_path = path.resolve()
    return config


def save_desktop_config(config: Config, path: Path | None = None) -> Path:
    """Atomically save a GUI-editable configuration file."""
    destination = (path or config.source_path or desktop_config_path()).resolve()
    return save_config(config, destination)


def load_desktop_config(path: Path | None = None) -> Config:
    """Load V1 configuration, creating safe per-user defaults on first run."""
    selected = (path or desktop_config_path()).resolve()
    if selected.is_file():
        config = load_config(selected)
    else:
        config = _new_config(selected)
        save_desktop_config(config, selected)
    config.logging.console = False
    if _repair_log_location(config):
        save_desktop_config(config, selected)
    return config


def _repair_log_location(config: Config) -> bool:
    """Move the log back beside the library if it points somewhere disposable.

    A config can end up naming a log inside ``%TEMP%`` — a packaged smoke test,
    an installer, or a library that was relocated while a temporary config was
    live. Windows then clears that directory and the desktop writes its
    diagnostics into a folder that no longer exists, which is precisely the
    situation where someone needs the log to explain why the app is
    misbehaving. Returns whether anything changed.
    """
    from ..maintenance import is_temporary_location

    current = config.logging.file
    if current is None or not is_temporary_location(current):
        return False
    config.logging.file = config.storage.db_path.parent / "engine.log"
    return True
