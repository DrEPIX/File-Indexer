"""HTTP transport tests use an in-memory server; no network leaves pytest."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from mediaengine.config import Config
from mediaengine.plugins.context import AnalysisContext
from mediaengine.plugins.http import HttpAnalyzer, plugin_info_from_manifest
from mediaengine.plugins.registry import PluginRegistry


def manifest(plugin_id: str = "local.test") -> dict[str, object]:
    return {
        "protocol": "mediaengine.analyzer/1",
        "id": plugin_id,
        "version": "1.2.3",
        "accepts": ["image", "video"],
        "emits": ["test.category"],
        "transfer": "paths",
        "requires": {"pixels": True, "frames": True, "max_concurrency": 2},
        "namespaces": {
            "test.category": {"display_name": "Test category", "facetable": True}
        },
    }


def test_manifest_parser_keeps_transport_capabilities() -> None:
    info = plugin_info_from_manifest(manifest())
    assert info.id == "local.test"
    assert info.transport == "http"
    assert info.frames is True
    assert info.max_concurrency == 2
    assert "test.category" in info.namespaces


def test_http_analyzer_serializes_context_and_decodes_annotations(
    tmp_path: Path, repos: object, config: Config
) -> None:
    from mediaengine.db.repositories import Repositories

    assert isinstance(repos, Repositories)
    source = tmp_path / "image.jpg"
    source.write_bytes(b"fixture")
    asset_id, _ = repos.assets.upsert_asset(
        content_hash="sha256:test-http",
        media_type="image",
        size_bytes=source.stat().st_size,
        mime_type="image/jpeg",
        hash_algorithm="sha256",
    )
    repos.assets.upsert_file(asset_id=asset_id, path=str(source), stat=source.stat())
    asset = repos.assets.get_asset(asset_id)
    assert asset is not None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "model_loaded": True})
        assert request.url.path == "/analyze"
        payload = json.loads(request.content)
        assert payload["asset"]["asset_id"] == asset_id
        assert payload["derivatives"]["original"]["path"] == str(source.resolve())
        return httpx.Response(
            200,
            json={
                "protocol": "mediaengine.analyzer/1",
                "request_id": payload["request_id"],
                "annotations": [
                    {
                        "namespace": "test.category",
                        "label": "example",
                        "confidence": 0.8,
                    }
                ],
            },
        )

    info = plugin_info_from_manifest(manifest())
    analyzer = HttpAnalyzer(
        info,
        base_url="http://testserver",
        http_transport=httpx.MockTransport(handler),
    )
    context = AnalysisContext(
        repos=repos,
        config=config,
        asset=asset,
        plugin_id=info.id,
        plugin_config={},
    )
    result = list(analyzer.analyze(context))
    assert [(item.namespace, item.label, item.confidence) for item in result] == [
        ("test.category", "example", 0.8)
    ]


def test_registry_discovers_http_manifest_one_directory_deep(
    tmp_path: Path, repos: object, config: Config
) -> None:
    from mediaengine.db.repositories import Repositories

    assert isinstance(repos, Repositories)
    plugin_dir = tmp_path / "plugins" / "test-http"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        """
[plugin]
id = "local.test"
version = "1.2.3"
transport = "http"
accepts = ["image", "video"]
emits = ["test.category"]
transfer = "paths"

[plugin.requires]
pixels = true
frames = true
max_concurrency = 2

[plugin.http]
base_url = "http://127.0.0.1:65534"

[plugin.namespaces."test.category"]
display_name = "Test category"
facetable = true
""".strip(),
        encoding="utf-8",
    )
    config.plugins.directories = [tmp_path / "plugins"]
    config.plugins.enabled = []
    discovered = PluginRegistry(config, repos).discover()
    assert "local.test" in discovered
    assert discovered["local.test"].analyzer is not None
    assert discovered["local.test"].enabled is False
