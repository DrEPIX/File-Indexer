from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mediaengine.api import create_app
from mediaengine.config import Config
from mediaengine.engine import MediaEngine


def _engine(tmp_path: Path) -> MediaEngine:
    config = Config()
    config.storage.db_path = tmp_path / "library.db"
    config.storage.derivatives_path = tmp_path / "derivatives"
    config.logging.file = None
    config.api.auth_token = "test-token-123456"
    engine = MediaEngine(config).start()
    asset_id, _ = engine.repos.assets.upsert_asset(
        content_hash="sha256:" + "a" * 64,
        hash_algorithm="sha256",
        media_type="image",
        mime_type="image/jpeg",
        size_bytes=1234,
        captured_at="2024-01-02T03:04:05.000Z",
    )
    engine.repos.assets.upsert_file(asset_id=asset_id, path=str(tmp_path / "Beach Photo.jpg"))
    engine.repos.assets.set_technical_metadata(
        asset_id, {"width": 1920, "height": 1080, "camera_make": "ExampleCam"}
    )
    engine.repos.search_docs.upsert(asset_id, filename="Beach Photo.jpg", labels="sunset")
    return engine


def test_health_auth_surface_and_asset(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    app = create_app(
        engine.config,
        engine=engine,
        sheet_path=Path(__file__).parents[1] / "qol_contract" / "change_sheet.toml",
    )
    try:
        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/surface").status_code == 401
            headers = {"Authorization": "Bearer test-token-123456"}
            surface = client.get("/api/surface", headers=headers)
            assert surface.status_code == 200
            assert len(surface.json()["filters"]) >= 30
            detail = client.get("/api/assets/1", headers=headers)
            assert detail.status_code == 200
            assert detail.json()["asset"]["media_type"] == "image"
    finally:
        engine.close()


def test_search_uses_qol_filters_and_core_fts_sanitizer(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    app = create_app(
        engine.config,
        engine=engine,
        sheet_path=Path(__file__).parents[1] / "qol_contract" / "change_sheet.toml",
    )
    headers = {"Authorization": "Bearer test-token-123456"}
    payload = {
        "text": "beach - sunset:",
        "where": {
            "operator": "all",
            "clauses": [
                {"key": "media.type", "operator": "eq", "value": "image"},
                {"key": "dimensions.width", "operator": "gte", "value": 1000},
                {"key": "file.name", "operator": "contains", "value": "Photo"},
            ],
        },
        "page_size": 10,
        "include_facets": False,
    }
    try:
        with TestClient(app) as client:
            response = client.post("/api/search", headers=headers, json=payload)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["total"] == 1
            assert data["items"][0]["filename"] == "Beach Photo.jpg"
            assert data["items"][0]["width"] == 1920

            compact = client.post(
                "/api/search",
                headers=headers,
                json={"query": "beach type:image", "include_facets": False},
            )
            assert compact.status_code == 200, compact.text
            assert compact.json()["total"] == 1

            created = client.post(
                "/api/annotations",
                headers=headers,
                json={"asset_id": 1, "namespace": "mood", "label": "bright"},
            )
            assert created.status_code == 201, created.text
            tagged = client.post(
                "/api/search",
                headers=headers,
                json={
                    "where": {
                        "clauses": [
                            {"key": "annotation.label", "operator": "eq", "value": "bright"}
                        ]
                    },
                    "include_facets": False,
                },
            )
            assert tagged.status_code == 200, tagged.text
            assert tagged.json()["total"] == 1
    finally:
        engine.close()


def test_search_rejects_unimplemented_vector_similarity(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    app = create_app(
        engine.config,
        engine=engine,
        sheet_path=Path(__file__).parents[1] / "qol_contract" / "change_sheet.toml",
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/search",
                headers={"Authorization": "Bearer test-token-123456"},
                json={
                    "where": {"clauses": [{"key": "semantic.similar_to", "operator": "similar_to", "value": 1}]}
                },
            )
            assert response.status_code == 422
            assert "vector" in response.json()["detail"]
    finally:
        engine.close()
