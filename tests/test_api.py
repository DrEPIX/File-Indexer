from __future__ import annotations

from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image

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
    engine.db.writer.run(
        lambda conn: conn.execute(
            "UPDATE files SET mtime_ns=? WHERE asset_id=?",
            (1_704_164_645_000_000_000, asset_id),
        )
    )
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
            root = client.get("/", follow_redirects=False)
            assert root.status_code == 307
            assert root.headers["location"] == "/docs"
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/surface").status_code == 401
            headers = {"Authorization": "Bearer test-token-123456"}
            surface = client.get("/api/surface", headers=headers)
            assert surface.status_code == 200
            assert len(surface.json()["filters"]) >= 30
            detail = client.get("/api/assets/1", headers=headers)
            assert detail.status_code == 200
            assert detail.json()["asset"]["media_type"] == "image"
            assert client.get("/api/assets/0", headers=headers).status_code == 422
            plugins = client.get("/api/plugins", headers=headers)
            assert plugins.status_code == 200, plugins.text
            assert isinstance(plugins.json(), list)
            assert any(item["plugin_id"] == "core.exif-entities" for item in plugins.json())

            catalog = client.get("/api/plugins/catalog", headers=headers)
            assert catalog.status_code == 200, catalog.text
            lm_studio = next(
                item for item in catalog.json() if item["plugin_id"] == "local.lm-studio"
            )
            assert lm_studio["kind"] == "Local LLM"
            assert lm_studio["configurable"] is True

            network_denied = client.patch(
                "/api/plugins/local.lm-studio",
                headers=headers,
                json={"enabled": True},
            )
            assert network_denied.status_code == 409
            assert "grant_network=true" in network_denied.json()["detail"]

            backfill = client.post(
                "/api/plugins/core.exif-entities/backfill",
                headers=headers,
                json={"limit": 10},
            )
            assert backfill.status_code == 202, backfill.text
            job_id = backfill.json()["id"]
            deadline = time.monotonic() + 5
            while True:
                job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
                if job["state"] in {"done", "failed", "cancelled"}:
                    break
                assert time.monotonic() < deadline, job
                time.sleep(0.02)
            assert job["state"] == "done", job
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

            excluded = client.post(
                "/api/search",
                headers=headers,
                json={
                    "where": {
                        "operator": "none",
                        "clauses": [
                            {"key": "annotation.label", "operator": "eq", "value": "bright"}
                        ],
                    },
                    "include_facets": False,
                },
            )
            assert excluded.status_code == 200, excluded.text
            assert excluded.json()["total"] == 0

            modified = client.post(
                "/api/search",
                headers=headers,
                json={
                    "where": {
                        "clauses": [
                            {"key": "date.modified", "operator": "after", "value": "2024-01-01T00:00:00Z"}
                        ]
                    },
                    "include_facets": False,
                },
            )
            assert modified.status_code == 200, modified.text
            assert modified.json()["total"] == 1

            invalid = client.post(
                "/api/annotations",
                headers=headers,
                json={"asset_id": 1, "namespace": "Not Valid!", "label": "x"},
            )
            assert invalid.status_code == 422
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


def test_api_rejects_a_second_concurrent_scan(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    gate = threading.Event()

    def slow_scan(*args: object, **kwargs: object) -> list[object]:
        gate.wait(timeout=2)
        return []

    engine.scan = slow_scan  # type: ignore[method-assign]
    app = create_app(
        engine.config,
        engine=engine,
        sheet_path=Path(__file__).parents[1] / "qol_contract" / "change_sheet.toml",
    )
    headers = {"Authorization": "Bearer test-token-123456"}
    try:
        with TestClient(app) as client:
            first = client.post("/api/scan", headers=headers, json={})
            assert first.status_code == 202
            second = client.post("/api/scan", headers=headers, json={})
            assert second.status_code == 409
            assert first.json()["id"] in second.json()["detail"]
            gate.set()
    finally:
        gate.set()
        engine.close()


def test_background_scan_to_search_user_journey(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    Image.new("RGB", (48, 32), "cornflowerblue").save(library / "First Test Photo.png")

    config = Config()
    config.storage.db_path = tmp_path / "journey.db"
    config.storage.derivatives_path = tmp_path / "derivatives"
    config.logging.file = None
    config.api.auth_token = "test-token-123456"
    engine = MediaEngine(config).start()
    app = create_app(
        config,
        engine=engine,
        sheet_path=Path(__file__).parents[1] / "qol_contract" / "change_sheet.toml",
    )
    headers = {"Authorization": "Bearer test-token-123456"}
    try:
        with TestClient(app) as client:
            started = client.post(
                "/api/scan",
                headers=headers,
                json={"roots": [str(library)], "generate_derivatives": False},
            )
            assert started.status_code == 202, started.text
            job_id = started.json()["id"]
            job = started.json()
            deadline = time.monotonic() + 10
            while job["state"] not in {"done", "failed", "cancelled"}:
                assert time.monotonic() < deadline, job
                time.sleep(0.02)
                response = client.get(f"/api/jobs/{job_id}", headers=headers)
                assert response.status_code == 200
                job = response.json()
            assert job["state"] == "done", job
            assert job["result"][0]["files_new"] == 1

            found = client.post(
                "/api/search",
                headers=headers,
                json={"query": '"First Test" type:image', "include_facets": True},
            )
            assert found.status_code == 200, found.text
            assert found.json()["total"] == 1
            assert found.json()["items"][0]["filename"] == "First Test Photo.png"
    finally:
        engine.close()
