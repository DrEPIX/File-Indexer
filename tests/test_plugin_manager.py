from __future__ import annotations

from pathlib import Path

import pytest

from mediaengine.config import Config, save_config
from mediaengine.engine import MediaEngine
from mediaengine.errors import ConfigError
from mediaengine.plugins.http import HttpAnalyzer
from mediaengine.plugins.manager import PluginManager, endpoint_is_local


REMOTE_MANIFEST = {
    "protocol": "mediaengine.analyzer/1",
    "id": "local.test-api",
    "version": "1.2.3",
    "description": "Test API analyzer",
    "accepts": ["image"],
    "emits": ["test.label"],
    "transport": "http",
    "transfer": "both",
    "requires": {"network": False},
    "namespaces": {"test.label": {"display_name": "Test label"}},
}


def _engine(tmp_path: Path) -> MediaEngine:
    config_path = tmp_path / "config.yaml"
    config = Config()
    config.storage.db_path = tmp_path / "data" / "library.db"
    config.storage.derivatives_path = tmp_path / "data" / "derivatives"
    config.logging.file = None
    config.source_path = config_path
    save_config(config)
    return MediaEngine(config).start()


def test_endpoint_locality_is_conservative() -> None:
    assert endpoint_is_local("http://127.0.0.1:9000")
    assert endpoint_is_local("http://localhost:9000")
    assert endpoint_is_local("http://[::1]:9000")
    assert not endpoint_is_local("https://plugins.example.test")
    assert not endpoint_is_local("http://192.168.1.10:9000")


def test_register_remote_persists_and_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(HttpAnalyzer, "remote_manifest", lambda self: REMOTE_MANIFEST)
    engine = _engine(tmp_path)
    try:
        manager = PluginManager(engine)
        result = manager.register_remote("http://127.0.0.1:9191")
        assert result.plugin_id == "local.test-api"
        assert engine.config.plugins.remote["local.test-api"].base_url == "http://127.0.0.1:9191"
        assert engine.plugins.get("local.test-api") is not None
        saved = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "local.test-api" in saved
        assert "127.0.0.1:9191" in saved

        assert manager.unregister_remote("local.test-api") is True
        assert "local.test-api" not in engine.config.plugins.remote
    finally:
        engine.close()


def test_register_remote_rejects_external_without_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(HttpAnalyzer, "remote_manifest", lambda self: REMOTE_MANIFEST)
    engine = _engine(tmp_path)
    try:
        with pytest.raises(ConfigError, match="loopback"):
            PluginManager(engine).register_remote("https://plugins.example.test")
    finally:
        engine.close()
