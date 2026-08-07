"""Standalone FastAPI implementation of ``mediaengine.analyzer/1`` over HTTP."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import math
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError

from model import DEFAULT_LABELS, MODEL_ID, EncodedBatch, Encoder, load_default_encoder


PROTOCOL = "mediaengine.analyzer/1"
PLUGIN_ID = "acme.clip"
PLUGIN_VERSION = "1.0.0"
EXPECTED_EMBEDDING_DIM = 512


class ModelRuntime:
    """Thread-safe lazy model lifecycle shared by all requests."""

    def __init__(self, loader: Callable[[], Encoder] = load_default_encoder) -> None:
        self._loader = loader
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._encoder: Encoder | None = None
        self._error: str | None = None

    def start_loading(self) -> None:
        """Start one background load without blocking the health endpoint."""

        with self._lock:
            if self._encoder is not None or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._load, name="clip-model-loader", daemon=True)
            self._thread.start()

    def _load(self) -> None:
        try:
            encoder = self._loader()
            if encoder.embedding_dim != EXPECTED_EMBEDDING_DIM:
                raise RuntimeError(
                    f"encoder dimension {encoder.embedding_dim} does not match "
                    f"the declared dimension {EXPECTED_EMBEDDING_DIM}"
                )
            with self._lock:
                self._encoder = encoder
        except Exception as exc:  # model libraries expose many exception types
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> tuple[str, Encoder | None, str]:
        """Return health state, ready encoder, and human-readable detail."""

        with self._lock:
            if self._error is not None:
                return "error", None, self._error
            if self._encoder is not None:
                return "ok", self._encoder, self._encoder.detail
            return "loading", None, f"loading {MODEL_ID}"

    def install_for_test(self, encoder: Encoder) -> None:
        """Install a deterministic encoder for black-box contract tests."""

        with self._lock:
            self._encoder = encoder
            self._error = None
            self._thread = None

    def reset_for_test(self, loader: Callable[[], Encoder]) -> None:
        """Reset lifecycle state with a controlled test loader."""

        with self._lock:
            self._loader = loader
            self._encoder = None
            self._error = None
            self._thread = None


runtime = ModelRuntime()
app = FastAPI(title="MediaEngine CLIP analyzer", version=PLUGIN_VERSION)


def manifest() -> dict[str, Any]:
    """Return the frozen analyzer manifest plus the acknowledged dimension extension."""

    return {
        "protocol": PROTOCOL,
        "id": PLUGIN_ID,
        "version": PLUGIN_VERSION,
        "model_id": MODEL_ID,
        "embedding_dim": EXPECTED_EMBEDDING_DIM,
        "accepts": ["image", "video"],
        "emits": ["clip", "clip.tag"],
        "depends_on": [],
        "transfer": "both",
        "requires": {
            "pixels": True,
            "frames": True,
            "audio": False,
            "text": False,
            "metadata_only": False,
            # CUDA accelerates this service, but CPU remains supported and
            # therefore GPU is not a required host capability.
            "gpu": False,
            "network": True,
            "max_concurrency": 2,
        },
        "namespaces": {
            "clip": {"display_name": "Visual similarity", "value_type": "text", "facetable": False},
            "clip.tag": {"display_name": "CLIP zero-shot labels", "value_type": "categorical", "facetable": True},
        },
    }


def authorized(authorization: str | None = Header(default=None)) -> None:
    """Enforce the optional engine-to-analyzer bearer token."""

    expected = os.environ.get("CLIP_AUTH_TOKEN", "")
    if expected and authorization != f"Bearer {expected}":
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def error(status: int, kind: str, message: str, *, retryable: bool, retry_after: int | None = None) -> JSONResponse:
    """Build the exact analyzer error envelope and optional backoff header."""

    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        status_code=status,
        content={"error": {"kind": kind, "message": message, "retryable": retryable}},
        headers=headers,
    )


@app.get("/manifest", dependencies=[Depends(authorized)])  # type: ignore[untyped-decorator]
def get_manifest() -> dict[str, Any]:
    """Return registration metadata without forcing a model download."""

    return manifest()


@app.get("/health", dependencies=[Depends(authorized)])  # type: ignore[untyped-decorator]
def health() -> JSONResponse:
    """Start lazy loading and report loading, ready, or terminal error state."""

    runtime.start_loading()
    status, encoder, detail = runtime.snapshot()
    payload = {"status": status, "model_loaded": encoder is not None, "detail": detail}
    return JSONResponse(status_code=503 if status == "error" else 200, content=payload)


def decode_image(reference: Mapping[str, Any]) -> Image.Image:
    """Decode a path or inline derivative into an owned RGB Pillow image."""

    try:
        if isinstance(reference.get("path"), str):
            with Image.open(str(reference["path"])) as source:
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


def image_reference(derivatives: Mapping[str, Any]) -> Mapping[str, Any]:
    """Prefer a 512px thumbnail, then the largest thumbnail, then original."""

    thumbnails = derivatives.get("thumbnails")
    if isinstance(thumbnails, Mapping) and thumbnails:
        preferred = thumbnails.get("512")
        if isinstance(preferred, Mapping):
            return preferred
        numeric = [(int(key), value) for key, value in thumbnails.items() if str(key).isdigit() and isinstance(value, Mapping)]
        if numeric:
            return max(numeric, key=lambda pair: pair[0])[1]
    original = derivatives.get("original")
    if isinstance(original, Mapping):
        return original
    raise ValueError("image work item has no usable original or thumbnail")


def requested_labels(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate configurable zero-shot labels and preserve their order."""

    raw = config.get("prompts", list(DEFAULT_LABELS))
    if raw is None or raw is False:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("config.prompts must be an array of strings")
    labels = tuple(str(item).strip() for item in raw if str(item).strip())
    if len(labels) > 64:
        raise ValueError("config.prompts is limited to 64 labels")
    return labels


def mean_normalized(vectors: Sequence[Sequence[float]]) -> list[float]:
    """Mean-pool equal-dimension vectors and L2-normalize the result."""

    if not vectors:
        raise ValueError("cannot pool an empty vector list")
    dimension = len(vectors[0])
    if dimension == 0 or any(len(vector) != dimension for vector in vectors):
        raise ValueError("inconsistent embedding dimensions")
    mean = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(dimension)]
    norm = math.sqrt(sum(value * value for value in mean))
    return [value / norm for value in mean] if norm > 0 else mean


def tag_annotations(scores: Mapping[str, float], config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Emit configurable zero-shot labels at or above a confidence threshold."""

    threshold = float(config.get("tag_threshold", 0.20))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("config.tag_threshold must be between 0 and 1")
    return [
        {"namespace": "clip.tag", "label": label, "confidence": float(confidence)}
        for label, confidence in scores.items()
        if confidence >= threshold
    ]


def collect_images(work: Mapping[str, Any]) -> tuple[str, list[Image.Image], list[float | None]]:
    """Decode an image or a bounded keyframe batch from a work item."""

    asset = work.get("asset")
    derivatives = work.get("derivatives")
    config = work.get("config")
    if not isinstance(asset, Mapping) or not isinstance(derivatives, Mapping):
        raise ValueError("asset and derivatives must be objects")
    media_type = str(asset.get("media_type", ""))
    config_map = config if isinstance(config, Mapping) else {}
    if media_type == "image":
        return media_type, [decode_image(image_reference(derivatives))], [None]
    if media_type == "video":
        maximum = int(config_map.get("max_keyframes", 5))
        if not 1 <= maximum <= 64:
            raise ValueError("config.max_keyframes must be between 1 and 64")
        raw_keyframes = derivatives.get("keyframes")
        if not isinstance(raw_keyframes, Sequence) or isinstance(raw_keyframes, (str, bytes)):
            raise ValueError("video work item has no keyframes")
        selected = [item for item in raw_keyframes if isinstance(item, Mapping)][:maximum]
        if not selected:
            raise ValueError("video work item has no usable keyframes")
        return (
            media_type,
            [decode_image(item) for item in selected],
            [float(item["time"]) if item.get("time") is not None else None for item in selected],
        )
    return media_type, [], []


def build_annotations(
    media_type: str,
    batch: EncodedBatch,
    frame_times: Sequence[float | None],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Convert native model output into the frozen annotation shape."""

    annotations: list[dict[str, Any]] = []
    if media_type == "image":
        annotations.append({"namespace": "clip", "label": "embedding", "embedding": batch.embeddings[0]})
        annotations.extend(tag_annotations(batch.label_scores[0], config))
        return annotations
    for vector, frame_time in zip(batch.embeddings, frame_times, strict=True):
        annotations.append(
            {
                "namespace": "clip",
                "label": "embedding",
                "region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "frame_time": frame_time, "page_number": None, "kind": "frame"},
                "embedding": vector,
            }
        )
    annotations.append({"namespace": "clip", "label": "embedding", "embedding": mean_normalized(batch.embeddings)})
    if batch.label_scores:
        averaged = {
            label: sum(row.get(label, 0.0) for row in batch.label_scores) / len(batch.label_scores)
            for label in batch.label_scores[0]
        }
        annotations.extend(tag_annotations(averaged, config))
    return annotations


@app.post("/analyze", dependencies=[Depends(authorized)])  # type: ignore[untyped-decorator]
async def analyze(work: dict[str, Any]) -> JSONResponse:
    """Decode requested derivatives, batch inference, and return annotations."""

    started = time.monotonic()
    if work.get("protocol") != PROTOCOL:
        return error(422, "invalid_protocol", "unsupported or missing protocol", retryable=False)
    request_id = work.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return error(422, "invalid_request", "request_id must be a non-empty string", retryable=False)
    asset = work.get("asset")
    media_type = asset.get("media_type") if isinstance(asset, Mapping) else None
    if media_type not in {"image", "video"}:
        return JSONResponse(
            status_code=200,
            content={"protocol": PROTOCOL, "request_id": request_id, "annotations": [], "model_id": MODEL_ID, "duration_ms": 0},
        )
    runtime.start_loading()
    status, encoder, detail = runtime.snapshot()
    if status == "error":
        return error(503, "model_error", detail, retryable=True, retry_after=30)
    if encoder is None:
        return error(503, "model_loading", detail, retryable=True, retry_after=5)
    try:
        deadline_s = float(work.get("deadline_s", 120.0))
        if deadline_s <= 0:
            return error(408, "deadline_exceeded", "deadline elapsed before analysis", retryable=True)
        config_value = work.get("config")
        config: Mapping[str, Any] = config_value if isinstance(config_value, Mapping) else {}
        labels = requested_labels(config)
        decoded_type, images, frame_times = collect_images(work)
        remaining = deadline_s - (time.monotonic() - started)
        if remaining <= 0:
            return error(408, "deadline_exceeded", "deadline elapsed while decoding", retryable=True)
        batch = await asyncio.wait_for(asyncio.to_thread(encoder.encode, images, labels), timeout=remaining)
        annotations = build_annotations(decoded_type, batch, frame_times, config)
    except asyncio.TimeoutError:
        return error(408, "deadline_exceeded", "model inference exceeded deadline", retryable=True)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        return error(400, "corrupt_media", str(exc), retryable=False)
    except Exception as exc:
        return error(500, "inference_error", f"{type(exc).__name__}: {exc}", retryable=True)
    duration_ms = int((time.monotonic() - started) * 1000)
    return JSONResponse(
        status_code=200,
        content={
            "protocol": PROTOCOL,
            "request_id": request_id,
            "annotations": annotations,
            "model_id": MODEL_ID,
            "duration_ms": duration_ms,
        },
    )
