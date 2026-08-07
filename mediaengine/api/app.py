"""FastAPI controller over the engine and declarative QoL service."""

from __future__ import annotations

import asyncio
import hmac
import os
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ..config import Config, load_config
from ..engine import MediaEngine


def _import_qol() -> tuple[Any, Any, Any]:
    """Import the bundled package, with a source-checkout convenience path."""
    try:
        from mediaengine_qol import QoLService, SurfaceRegistry
        from mediaengine_qol.mediaengine_backend import MediaEngineBackend
    except ModuleNotFoundError:
        source = Path(__file__).resolve().parents[2] / "qol_contract" / "src"
        if not source.is_dir():
            raise RuntimeError("mediaengine-qol-contract is not installed") from None
        sys.path.insert(0, str(source))
        from mediaengine_qol import QoLService, SurfaceRegistry
        from mediaengine_qol.mediaengine_backend import MediaEngineBackend
    return QoLService, SurfaceRegistry, MediaEngineBackend


def _sheet_path(explicit: str | Path | None = None) -> Path:
    candidates = [
        Path(explicit) if explicit else None,
        Path(os.environ["MEDIAENGINE_QOL_SHEET"]) if os.environ.get("MEDIAENGINE_QOL_SHEET") else None,
        Path.cwd() / "qol_contract" / "change_sheet.toml",
        Path("/config/change_sheet.toml"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("QoL change sheet not found; set MEDIAENGINE_QOL_SHEET")


class JobManager:
    """One-process background scan registry suitable for the first UI."""

    def __init__(self, engine: MediaEngine) -> None:
        self.engine = engine
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, Any] = {}

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        from ..core.control import CancelToken

        job_id = uuid.uuid4().hex
        token = CancelToken()
        job = {"id": job_id, "state": "queued", "progress": None, "result": None, "error": None}
        with self._lock:
            self._jobs[job_id] = job
            self._tokens[job_id] = token

        def progress(event: Any) -> None:
            with self._lock:
                job["progress"] = event.as_dict()

        def run() -> None:
            with self._lock:
                job["state"] = "running"
            try:
                results = self.engine.scan(
                    payload.get("roots") or None,
                    resume=bool(payload.get("resume", True)),
                    rehash=bool(payload.get("rehash", False)),
                    generate_derivatives=bool(payload.get("generate_derivatives", True)),
                    progress=progress,
                    cancel=token,
                )
                with self._lock:
                    job["result"] = [item.as_dict() for item in results]
                    # BatchProducer cancels its shared token during ordinary
                    # cleanup. ScanResult.cancelled is the authoritative user-
                    # visible outcome; consulting the token here mislabels a
                    # completed scan as cancelled.
                    job["state"] = "cancelled" if any(item.cancelled for item in results) else "done"
            except Exception as exc:  # surfaced to the job endpoint
                with self._lock:
                    job["state"] = "failed"
                    job["error"] = f"{type(exc).__name__}: {exc}"

        threading.Thread(target=run, name=f"scan-{job_id[:8]}", daemon=True).start()
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(job_id)
        if token is None:
            return False
        token.cancel("cancelled through API")
        return True

    def start_backfill(self, plugin_id: str, limit: int | None) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "kind": "plugin_backfill",
            "plugin_id": plugin_id,
            "state": "queued",
            "progress": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job

        def run() -> None:
            with self._lock:
                job["state"] = "running"
            try:
                result = self.engine.backfill([plugin_id], limit=limit)
                with self._lock:
                    job["result"] = result.as_dict()
                    job["state"] = "done" if result.failed == 0 else "failed"
                    if result.failed:
                        job["error"] = f"{result.failed} analysis task(s) failed"
            except Exception as exc:  # surfaced to the job endpoint
                with self._lock:
                    job["state"] = "failed"
                    job["error"] = f"{type(exc).__name__}: {exc}"

        threading.Thread(target=run, name=f"backfill-{job_id[:8]}", daemon=True).start()
        return dict(job)


def create_app(
    config: Config | None = None,
    *,
    engine: MediaEngine | None = None,
    sheet_path: str | Path | None = None,
) -> Any:
    try:
        from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse
    except ImportError as exc:
        raise RuntimeError("FastAPI is not installed; install mediaengine[api]") from exc

    # With postponed annotations FastAPI resolves names in module globals,
    # while these imports intentionally stay optional and local to the factory.
    globals()["Request"] = Request
    globals()["WebSocket"] = WebSocket

    QoLService, SurfaceRegistry, MediaEngineBackend = _import_qol()
    cfg = config or load_config()
    owned_engine = engine is None
    runtime = engine or MediaEngine(cfg)
    registry = SurfaceRegistry.from_toml(_sheet_path(sheet_path))
    service = QoLService(registry, MediaEngineBackend(runtime))
    jobs = JobManager(runtime)

    @asynccontextmanager
    async def lifespan(_: Any):
        runtime.start()
        try:
            yield
        finally:
            if owned_engine:
                runtime.close()

    app = FastAPI(
        title="MediaEngine API",
        version="0.1.0",
        docs_url="/docs" if cfg.api.docs_enabled else None,
        redoc_url="/redoc" if cfg.api.docs_enabled else None,
        lifespan=lifespan,
    )
    if cfg.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.api.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )

    def require_auth(request: Request) -> None:
        expected = cfg.api.auth_token
        if not expected:
            return
        auth = request.headers.get("authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="valid bearer token required")

    protected = [Depends(require_auth)]

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return runtime.health()

    @app.get("/api/surface", dependencies=protected)
    def surface() -> Any:
        return service.describe()

    @app.post("/api/search", dependencies=protected)
    def search(payload: dict[str, Any]) -> Any:
        try:
            # The compact query-string mode is Claude's core search surface;
            # the recursive JSON mode below is the QoL plan surface. Supporting
            # both here keeps CLI, UI, and automation clients on one endpoint.
            if payload.get("query") is not None:
                from ..search import parse_query

                query = parse_query(
                    str(payload.get("query", "")),
                    limit=int(payload.get("page_size", payload.get("limit", 50))),
                    offset=int(payload.get("offset", 0)),
                )
                return runtime.search(query, with_facets=bool(payload.get("include_facets", True)))
            return service.search(payload)
        except (ValueError, NotImplementedError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/assets/{asset_id}", dependencies=protected)
    def asset(asset_id: int) -> Any:
        result = service.asset(asset_id)
        if result is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return result

    @app.get("/api/assets/{asset_id}/export", dependencies=protected)
    def export_asset(asset_id: int) -> Any:
        return asset(asset_id)

    @app.get("/api/assets/{asset_id}/thumb", dependencies=protected)
    def thumbnail(asset_id: int) -> Any:
        options = runtime.repos.derivatives.for_asset(asset_id, kind="thumb")
        if not options:
            raise HTTPException(status_code=404, detail="thumbnail not available")
        chosen = min(options, key=lambda item: abs(int(item["variant"]) - 512))
        root = cfg.storage.derivatives_path.resolve()
        path = (root / str(chosen["rel_path"])).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="invalid derivative path") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="thumbnail file missing")
        return FileResponse(path)

    @app.delete("/api/assets/{asset_id}/derived", dependencies=protected)
    def purge_asset(asset_id: int) -> dict[str, Any]:
        return {"asset_id": asset_id, "deleted": runtime.purge_asset_derivatives(asset_id)}

    @app.get("/api/facets", dependencies=protected)
    def facets(namespace: str | None = None) -> Any:
        return service.facets(namespace)

    @app.get("/api/map", dependencies=protected)
    def map_points(
        min_lat: float = -90.0, min_lon: float = -180.0,
        max_lat: float = 90.0, max_lon: float = 180.0, zoom: int = 1,
    ) -> Any:
        return {
            "bounds": runtime.repos.places.bounds(),
            "clusters": runtime.repos.places.cluster_for_map(
                min_lat, min_lon, max_lat, max_lon, zoom=zoom
            ),
        }

    @app.post("/api/annotations", dependencies=protected, status_code=201)
    def create_annotation(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            asset_id = int(payload["asset_id"])
            namespace = str(payload["namespace"]).strip()
            label = str(payload["label"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="asset_id, namespace, and label are required"
            ) from exc
        if not namespace or not label:
            raise HTTPException(status_code=422, detail="namespace and label must be non-empty")
        if runtime.repos.assets.get_asset(asset_id) is None:
            raise HTTPException(status_code=404, detail="asset not found")
        annotation_id = runtime.repos.annotations.add_user_annotation(
            asset_id,
            namespace,
            label,
            value=payload.get("value") if isinstance(payload.get("value"), dict) else None,
        )
        runtime.pipeline().reindex_asset(asset_id)
        return {"id": annotation_id, "asset_id": asset_id, "source": "user"}

    @app.post("/api/scan", dependencies=protected, status_code=202)
    def start_scan(payload: dict[str, Any]) -> Any:
        return jobs.start(payload)

    @app.get("/api/jobs/{job_id}", dependencies=protected)
    def get_job(job_id: str) -> Any:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.delete("/api/jobs/{job_id}", dependencies=protected)
    def cancel_job(job_id: str) -> dict[str, Any]:
        if not jobs.cancel(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        return {"id": job_id, "cancel_requested": True}

    @app.websocket("/api/jobs/{job_id}/events")
    async def job_events(websocket: WebSocket, job_id: str) -> None:
        expected = cfg.api.auth_token
        token = websocket.query_params.get("token", "")
        if expected and not hmac.compare_digest(token, expected):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                job = jobs.get(job_id)
                if job is None:
                    await websocket.send_json({"id": job_id, "state": "missing"})
                    return
                await websocket.send_json(job)
                if job["state"] in {"done", "failed", "cancelled"}:
                    return
                await asyncio.sleep(0.25)
        except WebSocketDisconnect:
            return

    @app.get("/api/admin/stats", dependencies=protected)
    def stats() -> Any:
        return runtime.stats()

    @app.get("/api/scans", dependencies=protected)
    def scans(limit: int = 20) -> Any:
        return runtime.scans(min(max(limit, 1), 500))

    @app.get("/api/errors", dependencies=protected)
    def errors(limit: int = 50, scope: str | None = None) -> Any:
        return runtime.errors(min(max(limit, 1), 500), scope=scope)

    @app.get("/api/plugins", dependencies=protected)
    def plugins() -> Any:
        return runtime.plugins.describe()

    @app.post("/api/plugins/{plugin_id}/backfill", dependencies=protected, status_code=202)
    def backfill_plugin(plugin_id: str, payload: dict[str, Any] | None = None) -> Any:
        if runtime.plugins.get(plugin_id) is None:
            raise HTTPException(status_code=404, detail="plugin not found")
        requested = payload or {}
        limit_value = requested.get("limit")
        try:
            limit = None if limit_value is None else max(1, int(limit_value))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="limit must be a positive integer") from exc
        return jobs.start_backfill(plugin_id, limit)

    @app.delete("/api/producers/{producer_id}/annotations", dependencies=protected)
    def purge_producer(producer_id: int) -> Any:
        return runtime.repos.annotations.purge_producer(producer_id)

    @app.delete("/api/biometrics", dependencies=protected)
    def purge_biometrics() -> Any:
        return runtime.purge_biometrics()

    app.state.engine = runtime
    app.state.qol = service
    app.state.jobs = jobs
    return app
