"""Shared fixtures.

Every test gets its own database and its own derivative cache under ``tmp_path``
so the suite can run in parallel and never touches a real library.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from mediaengine.config import Config
from mediaengine.core.procs import find_binary
from mediaengine.db.connection import Database
from mediaengine.db.repositories import Repositories
from mediaengine.engine import MediaEngine

# A 1x1 red GIF and a minimal PNG, as literals, so the smallest tests need no
# image library at all.
TINY_GIF = bytes.fromhex(
    "47494638396101000100800000ff0000ffffff21f90401000000002c00000000010001000002024401003b"
)


def make_config(tmp_path: Path, root: Path | None = None, **overrides: object) -> Config:
    """A config pointing entirely inside ``tmp_path``."""
    data = tmp_path / "data"
    payload: dict[str, object] = {
        "library": {
            "roots": [str(root)] if root else [],
            "min_file_size": 0,
        },
        "storage": {
            "db_path": str(data / "library.db"),
            "derivatives_path": str(data / "derivatives"),
            "thumbnail_sizes": [64, 256],
        },
        "logging": {"file": None, "console": False, "level": "ERROR"},
    }
    for key, value in overrides.items():
        section = payload.setdefault(key, {})
        assert isinstance(section, dict)
        section.update(value)  # type: ignore[arg-type]
    config = Config(**payload)  # type: ignore[arg-type]
    config.resolve_paths(tmp_path)
    return config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "test.db")
    db.migrate()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def repos(database: Database) -> Repositories:
    return Repositories(database)


@pytest.fixture
def engine(config: Config) -> Iterator[MediaEngine]:
    instance = MediaEngine(config)
    instance.start()
    try:
        yield instance
    finally:
        instance.close()


def write_jpeg(
    path: Path,
    size: tuple[int, int] = (64, 48),
    colour: tuple[int, int, int] = (20, 90, 160),
    *,
    gps: bool = False,
    captured: str | None = None,
    gradient: bool = False,
) -> Path:
    """A real JPEG, optionally carrying EXIF the tests assert on."""
    from PIL import Image

    image = Image.new("RGB", size, colour)
    if gradient:
        # Vertical bands of non-monotonic brightness. A *monotonic* left-to-
        # right ramp is useless for testing a dhash: every "is this pixel
        # brighter than the one to its right" comparison answers no, so it
        # hashes identically to a flat colour. Bands make adjacent columns
        # differ in both directions and produce a hash with real structure.
        levels = (30, 210, 95, 245)
        for x in range(size[0]):
            level = levels[(x * len(levels)) // max(1, size[0])]
            for y in range(size[1]):
                image.putpixel((x, y), (level, level, 90))

    exif = Image.Exif()
    exif[0x010F] = "Canon"
    exif[0x0110] = "EOS R5"
    if captured:
        exif[0x8769] = {0x9003: captured}
    if gps:
        exif[0x8825] = {
            1: "N",
            2: (51.0, 30.0, 26.0),
            3: "W",
            4: (0.0, 7.0, 39.0),
            5: 0,
            6: 35.0,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, exif=exif, quality=88)
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A small but adversarial library: duplicates, unicode, raw pairs, junk."""
    root = tmp_path / "library"
    (root / "photos" / "trip").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "node_modules").mkdir(parents=True)

    write_jpeg(root / "photos" / "IMG_0001.JPG", gps=True, captured="2019:04:07 14:22:11",
               gradient=True)
    # Same bytes, second path: one asset, two files.
    shutil.copy(root / "photos" / "IMG_0001.JPG", root / "photos" / "trip" / "duplicate.jpg")

    write_jpeg(root / "photos" / "IMG_0002.JPG", colour=(10, 200, 10))
    # A TIFF-based raw: header plus the 'CR' marker Canon stamps at offset 8.
    (root / "photos" / "IMG_0002.CR2").write_bytes(b"II*\x00\x10\x00\x00\x00CR\x02\x00" + b"\0" * 512)
    (root / "photos" / "IMG_0002.xmp").write_text("<x:xmpmeta/>", encoding="utf-8")

    (root / "docs" / "проверка ünïcødé 文件.txt").write_text(
        "beach sunset holiday", encoding="utf-8"
    )
    (root / "docs" / ("n" * 180 + ".txt")).write_text("long filename", encoding="utf-8")
    (root / "docs" / "empty.bin").write_bytes(b"")

    intact = (root / "photos" / "IMG_0001.JPG").read_bytes()
    (root / "photos" / "truncated.jpg").write_bytes(intact[: len(intact) // 3])
    (root / "photos" / ".hidden.jpg").write_bytes(intact)
    (root / "node_modules" / "vendored.jpg").write_bytes(intact)
    return root


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    """A short synthetic video. Skips the test when ffmpeg is unavailable."""
    ffmpeg = find_binary("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=4:size=160x120:rate=10",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    """Keep pytest output readable; the engine logs at INFO by default."""
    import logging

    previous = logging.getLogger().level
    logging.getLogger().setLevel(logging.ERROR)
    yield
    logging.getLogger().setLevel(previous)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: spawns subprocesses or sleeps")
    config.addinivalue_line("markers", "requires_ffmpeg: needs ffmpeg/ffprobe on PATH")
    config.addinivalue_line("markers", "requires_exiftool: needs exiftool on PATH")


@pytest.fixture
def has_exiftool() -> bool:
    return find_binary("exiftool") is not None
