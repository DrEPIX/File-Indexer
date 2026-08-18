"""Discovery of filter packs: what is shipped, what a user added, what is on.

Packs come from three places, in ascending priority:

1. ``mediaengine/filters/builtin/*.toml`` — shipped with the app.
2. ``<config dir>/filter-packs/*.toml`` — packs the user wrote or downloaded.
3. ``plugins.filter_pack_dirs`` in config — any extra directories.

A pack existing is not the same as a pack running. Discovery is deliberately
separate from enablement, exactly as it is for analyzers: the store can only
offer what it can see, and a broken pack must be visible as broken rather than
silently absent.
"""

from __future__ import annotations

import logging
import shutil
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ConfigError
from .pack import FilterPack, load_pack, parse_pack

__all__ = [
    "BUILTIN_DIR",
    "PackDiscovery",
    "discover_packs",
    "install_pack",
    "pack_path",
    "remove_pack",
    "user_pack_dir",
]

_LOG = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"


def user_pack_dir(config: Config) -> Path:
    """Where user-authored packs live, beside the config that references them."""
    if config.source_path is not None:
        return config.source_path.resolve().parent / "filter-packs"
    return config.storage.db_path.resolve().parent / "filter-packs"


class PackDiscovery:
    """Every pack found, plus the ones that failed to parse."""

    def __init__(self) -> None:
        self.packs: dict[str, FilterPack] = {}
        self.errors: dict[str, str] = {}

    def add(self, pack: FilterPack) -> None:
        # Later sources win, so a user copy of a shipped pack shadows it
        # instead of colliding with it.
        self.packs[pack.id] = pack

    def fail(self, source: str, reason: str) -> None:
        self.errors[source] = reason
        _LOG.warning("filter pack %s failed to load: %s", source, reason)

    def sorted_packs(self) -> list[FilterPack]:
        return sorted(self.packs.values(), key=lambda pack: (not pack.builtin, pack.name))


def _load_dir(discovery: PackDiscovery, directory: Path, *, builtin: bool) -> None:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.toml")):
        try:
            discovery.add(load_pack(path, builtin=builtin))
        except ConfigError as exc:
            discovery.fail(str(path), str(exc))


def discover_packs(config: Config, *, extra_dirs: Sequence[Path] | None = None) -> PackDiscovery:
    """Find every pack this installation can offer."""
    discovery = PackDiscovery()
    _load_dir(discovery, BUILTIN_DIR, builtin=True)
    _load_dir(discovery, user_pack_dir(config), builtin=False)
    for directory in _configured_dirs(config, extra_dirs):
        _load_dir(discovery, directory, builtin=False)
    return discovery


def pack_path(config: Config, pack_id: str) -> Path:
    """Where a user-installed pack with this id is kept.

    Dots become dashes because the id is also a plugin id, and a filename with
    dots in it reads like a chain of extensions to every file manager on
    Windows.
    """
    return user_pack_dir(config) / f"{pack_id.replace('.', '-')}.toml"


def install_pack(
    config: Config, source: str | Path, *, overwrite: bool = False
) -> FilterPack:
    """Copy a ``*.toml`` taxonomy into this installation and return what it declares.

    Validation happens before the copy, not after. A pack that lands in the
    folder and only then turns out to be malformed is indistinguishable, to the
    person who just installed it, from one that installed and did nothing —
    whereas a refusal at import names the line that is wrong.
    """
    origin = Path(source).expanduser()
    if not origin.is_file():
        raise ConfigError(f"{origin} is not a file")
    try:
        document = tomllib.loads(origin.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{origin} could not be read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{origin.name} is not valid TOML: {exc}") from exc
    pack = parse_pack(document, source=str(origin))

    destination = pack_path(config, pack.id)
    if destination.exists() and not overwrite:
        raise ConfigError(
            f"{pack.id} is already installed ({destination.name}). Remove it first, or install "
            "with overwrite to replace it."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve() != origin.resolve():
        shutil.copyfile(origin, destination)
    _LOG.info("installed filter pack %s from %s", pack.id, origin)
    return load_pack(destination, builtin=False)


def remove_pack(config: Config, pack_id: str) -> Path | None:
    """Delete a user-installed pack, returning the file that went.

    Shipped packs are not removable — they are part of the application, and
    "removing" one is what disabling it already does. Returns ``None`` when
    nothing was installed under that id.
    """
    target = pack_path(config, pack_id)
    if not target.is_file():
        # A pack the user dropped in by hand may carry any filename, so fall
        # back to matching on the id the document actually declares.
        directory = user_pack_dir(config)
        if not directory.is_dir():
            return None
        for candidate in sorted(directory.glob("*.toml")):
            try:
                if load_pack(candidate).id == pack_id:
                    target = candidate
                    break
            except ConfigError:
                continue
        else:
            return None
    target.unlink()
    _LOG.info("removed filter pack %s (%s)", pack_id, target)
    return target


def _configured_dirs(config: Config, extra_dirs: Sequence[Path] | None) -> Iterable[Path]:
    raw: Any = getattr(config.plugins, "filter_pack_dirs", None) or []
    for value in raw:
        yield Path(value)
    for value in extra_dirs or ():
        yield Path(value)
