"""HTTP analyzer for optional CUDA-accelerated face and object inference."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError

from vision_model import FACE_EMBEDDING_DIM, MODEL_ID, FrameResult, VisionAnalyzer, load_default_analyzer


PROTOCOL = "mediaengine.analyzer/1"
PLUGIN_ID = "acme.vision"
PLUGIN_VERSION = "1.0.0"


class Runtime:
    """Load heavyweight models once while keeping health checks responsive."""

    def __init__(self, loader: Callable[[], VisionAnalyzer] = load_default_analyzer) -> None:
        self._loader = loader
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._analyzer: VisionAnalyzer | None = None
        self._error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._analyzer is not None or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._load, name="vision-model-loader", daemon=True)
            self._thread.start()

    def _load(self) -> None:
        try:
            analyzer = self._loader()
            if analyzer.embedding_dim != FACE_EMBEDDING_DIM:
                raise RuntimeError(
                    f"face embedding dimension {analyzer.embedding_dim} does not match declared {FACE_EMBEDDING_DIM}"
                )
            with self._lock:
                self._analyzer = analyzer
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> tuple[str, VisionAnalyzer | None, str]:
        with self._lock:
            if self._error is not None:
                return "error", None, self._error
            if self._analyzer is not None:
                return "ok", self._analyzer, self._analyzer.detail
            return "loading", None, f"loading {MODEL_ID}"

    def install_for_test(self, analyzer: VisionAnalyzer) -> None:
        with self._lock:
            self._analyzer = analyzer
            self._error = None
            self._thread = None


runtime = Runtime()
app = FastAPI(title="MediaEngine face and object analyzer", version=PLUGIN_VERSION)


def manifest() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "id": PLUGIN_ID,
        "version": PLUGIN_VERSION,
        "model_id": MODEL_ID,
        "embedding_dim": FACE_EMBEDDING_DIM,
        "accepts": ["image", "video"],
        "emits": ["vision.face", "vision.object"],
        "depends_on": [],
        "transfer": "both",
        "requires": {
            "pixels": True,
            "frames": True,
            "audio": False,
            "text": False,
            "metadata_only": False,
            "gpu": False,
            "network": True,
            "max_concurrency": 1,
        },
        "namespaces": {
            "vision.face": {
                "display_name": "Detected faces",
                "value_type": "categorical",
                "facetable": True,
                "embedding_dim": FACE_EMBEDDING_DIM,
            },
            "vision.object": {
                "display_name": "Detected objects",
                "value_type": "categorical",
                "facetable": True,
            },
        },
    }


def authorized(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("VISION_AUTH_TOKEN", "")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def failure(status: int, kind: str, message: str, *, retryable: bool, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        status_code=status,
        content={"error": {"kind": kind, "message": message, "retryable": retryable}},
        headers=headers,
    )


@app.get("/manifest", dependencies=[Depends(authorized)])  # type: ignore[untyped-decorator]
def get_manifest() -> dict[str, Any]:
    return manifest()


@app.get("/health", dependencies=[Depends(authorized)])  # type: ignore[untyped-decorator]
def health() -> JSONResponse:
    runtime.start()
    status, analyzer, detail = runtime.snapshot()
    return JSONResponse(
        status_code=503 if status == "error" else 200,
        content={"status": status, "model_loaded": analyzer is not None, "detail": detail},
    )


def decode(reference: Mapping[str, Any]) -> Image.Image:
    try:
        path = reference.get("path")
        if isinstance(path, str):
            with Image.open(path) as source:
                source.load()
                return source.convert("RGB")
        encoded = reference.get("content_b64")
        if isinstance(encoded, str):
            raw = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                return source.convert("RGB")
    except (OSError, UnidentifiedImageError, binascii.Error, ValueError) as exc:
        raise ValueError(f"cannot decode derivative: {exc}") from exc
    raise ValueError("derivative contains neither path nor content_b64")


def collect(work: Mapping[str, Any], max_keyframes: int) -> tuple[list[Image.Image], list[float | None]]:
    asset = work.get("asset")
    derivatives = work.get("derivatives")
    if not isinstance(asset, Mapping) or not isinstance(derivatives, Mapping):
        raise ValueError("asset and derivatives must be objects")
    if asset.get("media_type") == "image":
        thumbnails = derivatives.get("thumbnails")
        preferred = thumbnails.get("512") if isinstance(thumbnails, Mapping) else None
        original = derivatives.get("original")
        reference = preferred if isinstance(preferred, Mapping) else original
        if not isinstance(reference, Mapping):
            raise ValueError("image has no 512 thumbnail or original")
        return [decode(reference)], [None]
    keyframes = derivatives.get("keyframes")
    if not isinstance(keyframes, Sequence) or isinstance(keyframes, (str, bytes)):
        raise ValueError("video has no keyframes")
    selected = [item for item in keyframes if isinstance(item, Mapping)][:max_keyframes]
    if not selected:
        raise ValueError("video has no usable keyframes")
    return [decode(item) for item in selected], [float(item["time"]) if item.get("time") is not None else None for item in selected]


def bounded(config: Mapping[str, Any], key: str, default: int, maximum: int) -> int:
    value = int(config.get(key, default))
    if not 1 <= value <= maximum:
        raise ValueError(f"config.{key} must be between 1 and {maximum}")
    return value


def threshold(config: Mapping[str, Any], key: str, default: float) -> float:
    value = float(config.get(key, default))
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"config.{key} must be between 0 and 1")
    return value


def env_number(name: str, default: float) -> float:
    """Read one operator-facing default while keeping request overrides typed."""

    try:
        return float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc


def env_integer(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def region(box: tuple[float, float, float, float], image: Image.Image, frame_time: float | None, kind: str) -> dict[str, Any]:
    left, top, right, bottom = box
    width, height = image.size
    # Detectors can return boxes a few pixels outside the source image. Clamp
    # before normalization so every emitted region satisfies the host contract.
    x1 = min(max(left, 0.0), float(width))
    y1 = min(max(top, 0.0), float(height))
    x2 = min(max(right, x1), float(width))
    y2 = min(max(bottom, y1), float(height))
    return {
        "x": x1 / width,
        "y": y1 / height,
        "w": (x2 - x1) / width,
        "h": (y2 - y1) / height,
        "frame_time": frame_time,
        "page_number": None,
        "kind": kind,
    }


def build_annotations(results: Sequence[FrameResult], images: Sequence[Image.Image], times: Sequence[float | None]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for result, image, frame_time in zip(results, images, times, strict=True):
        output.extend(
            {
                "namespace": "vision.face",
                "label": "face",
                "confidence": face.confidence,
                "region": region(face.box, image, frame_time, "face"),
                "embedding": face.embedding,
            }
            for face in result.faces
        )
        output.extend(
            {
                "namespace": "vision.object",
                "label": detected.label,
                "confidence": detected.confidence,
                "region": region(detected.box, image, frame_time, "object"),
            }
            for detected in result.objects
        )
    return output


@app.post("/analyze", dependencies=[Depends(authorized)])  # type: ignore[untyped-decorator]
async def analyze(work: dict[str, Any]) -> JSONResponse:
    started = time.monotonic()
    if work.get("protocol") != PROTOCOL:
        return failure(422, "invalid_protocol", "unsupported or missing protocol", retryable=False)
    request_id = work.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return failure(422, "invalid_request", "request_id must be a non-empty string", retryable=False)
    asset = work.get("asset")
    if not isinstance(asset, Mapping) or asset.get("media_type") not in {"image", "video"}:
        return JSONResponse(
            status_code=200,
            content={"protocol": PROTOCOL, "request_id": request_id, "annotations": [], "model_id": MODEL_ID, "duration_ms": 0},
        )

    runtime.start()
    status, analyzer, detail = runtime.snapshot()
    if status == "error":
        return failure(503, "model_error", detail, retryable=True, retry_after=30)
    if analyzer is None:
        return failure(503, "model_loading", detail, retryable=True, retry_after=10)
    try:
        deadline = float(work.get("deadline_s", 180.0))
        config_value = work.get("config")
        config: Mapping[str, Any] = config_value if isinstance(config_value, Mapping) else {}
        images, times = collect(
            work,
            bounded(config, "max_keyframes", env_integer("VISION_MAX_KEYFRAMES", 3), 32),
        )
        remaining = deadline - (time.monotonic() - started)
        if remaining <= 0:
            return failure(408, "deadline_exceeded", "deadline elapsed while decoding", retryable=True)
        results = await asyncio.wait_for(
            asyncio.to_thread(
                analyzer.analyze,
                images,
                face_threshold=threshold(
                    config, "face_threshold", env_number("VISION_FACE_THRESHOLD", 0.90)
                ),
                object_threshold=threshold(
                    config, "object_threshold", env_number("VISION_OBJECT_THRESHOLD", 0.70)
                ),
                max_faces=bounded(
                    config, "max_faces", env_integer("VISION_MAX_FACES", 32), 256
                ),
                max_objects=bounded(
                    config, "max_objects", env_integer("VISION_MAX_OBJECTS", 100), 512
                ),
            ),
            timeout=remaining,
        )
        emitted = build_annotations(results, images, times)
    except asyncio.TimeoutError:
        return failure(408, "deadline_exceeded", "vision inference exceeded deadline", retryable=True)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        return failure(400, "invalid_media_or_config", str(exc), retryable=False)
    except Exception as exc:
        return failure(500, "inference_error", f"{type(exc).__name__}: {exc}", retryable=True)
    return JSONResponse(
        status_code=200,
        content={
            "protocol": PROTOCOL,
            "request_id": request_id,
            "annotations": emitted,
            "model_id": MODEL_ID,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
