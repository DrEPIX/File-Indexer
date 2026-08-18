from __future__ import annotations

import shutil
from pathlib import Path

from mediaengine.engine import MediaEngine

from .conftest import make_config, write_jpeg


def test_remove_indexed_root_preserves_originals_and_shared_assets(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_file = write_jpeg(first_root / "shared.jpg", gradient=True)
    second_file = second_root / "shared-copy.jpg"
    second_file.parent.mkdir(parents=True)
    shutil.copy2(first_file, second_file)
    config = make_config(tmp_path)
    engine = MediaEngine(config).start()
    try:
        engine.scan([first_root, second_root], generate_derivatives=False)
        assert engine.search("").total == 1

        first_result = engine.remove_indexed_root(first_root)
        assert first_result["removed_files"] == 1
        assert first_result["removed_assets"] == 0
        assert first_file.is_file()
        assert second_file.is_file()
        hits = engine.search("").hits
        assert len(hits) == 1
        assert Path(str(hits[0]["path"])).resolve() == second_file.resolve()

        second_result = engine.remove_indexed_root(second_root)
        assert second_result["removed_files"] == 1
        assert second_result["removed_assets"] == 1
        assert first_file.is_file()
        assert second_file.is_file()
        assert engine.search("").total == 0
    finally:
        engine.close()
