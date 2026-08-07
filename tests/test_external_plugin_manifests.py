from __future__ import annotations

from pathlib import Path

from mediaengine.config import Config
from mediaengine.engine import MediaEngine
from mediaengine.plugins.http import HttpAnalyzer


def test_gpu_services_load_through_core_http_registry_without_network(tmp_path: Path) -> None:
    config = Config()
    config.storage.db_path = tmp_path / "registry.db"
    config.storage.derivatives_path = tmp_path / "derivatives"
    config.logging.file = None
    config.plugins.directories = [Path(__file__).parents[1] / "plugins-available"]
    # Disabled discovery avoids contacting services; this test proves the
    # checked-in manifests fit the engine's real loader exactly.
    config.plugins.enabled = []
    config.plugins.enable_all = False

    engine = MediaEngine(config).start()
    try:
        clip = engine.plugins.get("acme.clip")
        vision = engine.plugins.get("acme.vision")
        safety = engine.plugins.get("local.safety")
        assert clip is not None and isinstance(clip.analyzer, HttpAnalyzer)
        assert vision is not None and isinstance(vision.analyzer, HttpAnalyzer)
        assert safety is not None and isinstance(safety.analyzer, HttpAnalyzer)
        assert clip.info.embedding_dim == 512
        assert vision.info.embedding_dim == 512
        assert clip.info.transfer == vision.info.transfer == "both"
        assert vision.info.namespaces["vision.face"]["embedding_dim"] == 512
        assert clip.enabled is False
        assert vision.enabled is False
        assert safety.enabled is False
    finally:
        engine.close()
