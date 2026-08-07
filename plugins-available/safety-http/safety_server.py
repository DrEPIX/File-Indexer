"""Local NSFW risk analyzer for images and sampled video frames."""

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

from safety_model import MODEL_ID, SafetyClassifier, load_default_classifier


PROTOCOL = "mediaengine.analyzer/1"
PLUGIN_ID = "local.safety"
PLUGIN_VERSION = "1.0.0"


class Runtime:
    """Load one model asynchronously and share it across requests."""

    def __init__(self, loader: Callable[[], SafetyClassifier] = load_default_classifier) -> None:
        self._loader = loader
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._classifier: SafetyClassifier | None = None
        self._error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._classifier is not None or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._load, name="safety-model-loader", daemon=True)
            self._thread.start()

    def _load(self) -> None:
        try:
            classifier = self._loader()
            with self._lock:
                self._classifier = classifier
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> tuple[str, SafetyClassifier | None, str]:
        with self._lock:
            if self._error is not None:
                return "error", None, self._error
            if self._classifier is not None:
                return "ok", self._classifier, self._classifier.detail
            return "loading", None, f"loading {MODEL_ID}"

    def install_for_test(self, classifier: SafetyClassifier) -> None:
        with self._lock:
            self._classifier = classifier
            self._error = None
            self._thread = None


runtime = Runtime()
app = FastAPI(title="MediaEngine local safety analyzer", version=PLUGIN_VERSION)


def manifest() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "id": PLUGIN_ID,
        "version": PLUGIN_VERSION,
        "model_id": MODEL_ID,
        "accepts": ["image", "video"],
        "emits": ["safety.nsfw", "safety.nsfw.frame"],
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
            "safety.nsfw": {
                "display_name": "NSFW safety rating",
                "description": "Advisory local model rating; never deletes or hides media by itself.",
                "value_type": "categorical",
                "facetable": True,
            },
            "safety.nsfw.frame": {
                "display_name": "Flagged video frames",
                "value_type": "categorical",
                "facetable": False,
            },
        },
    }


def authorized(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("SAFETY_AUTH_TOKEN", "")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def failure(status: int, kind: str, message: str, *, retryable: bool) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"kind": kind, "message": message, "retryable": retryable}},
    )


@app.get("/manifest", dependencies=[Depends(authorized)])  # type: ignore[untyped-decorator]
def get_manifest() -> dict[str, Any]:
    return manifest()


@app.get("/health", dependencies=[Depends(authorized)])  # type: ignore[untyped-decorator]
def health() -> JSONResponse:
    runtime.start()
    status, classifier, detail = runtime.snapshot()
    return JSONResponse(
        status_code=503 if status == "error" else 200,
        content={"status": status, "model_loaded": classifier is not None, "detail": detail},
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
            with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as source:
                source.load()
                return source.convert("RGB")
    except (OSError, UnidentifiedImageError, binascii.Error, ValueError) as exc:
        raise ValueError(f"cannot decode derivative: {exc}") from exc
    raise ValueError("derivative contains neither path nor content_b64")


def _image_reference(derivatives: Mapping[str, Any]) -> Mapping[str, Any]:
    thumbnails = derivatives.get("thumbnails")
    if isinstance(thumbnails, Mapping):
        preferred = thumbnails.get("512")
        if isinstance(preferred, Mapping):
            return preferred
        candidates = [value for value in thumbnails.values() if isinstance(value, Mapping)]
        if candidates:
            return candidates[-1]
    original = derivatives.get("original")
    if isinstance(original, Mapping):
        return original
    raise ValueError("image has no usable thumbnail or original")


def collect(work: Mapping[str, Any], maximum: int) -> tuple[list[Image.Image], list[float | None]]:
    asset = work.get("asset")
    derivatives = work.get("derivatives")
    if not isinstance(asset, Mapping) or not isinstance(derivatives, Mapping):
        raise ValueError("asset and derivatives must be objects")
    if asset.get("media_type") == "image":
        return [decode(_image_reference(derivatives))], [None]
    frames = derivatives.get("keyframes")
    if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes)):
        raise ValueError("video has no keyframes")
    selected = [item for item in frames if isinstance(item, Mapping)][:maximum]
    if not selected:
        raise ValueError("video has no usable keyframes")
    return (
        [decode(item) for item in selected],
        [float(item["time"]) if item.get("time") is not None else None for item in selected],
    )


def settings(config: Mapping[str, Any]) -> tuple[float, float, int]:
    review = float(config.get("review_threshold", os.environ.get("SAFETY_REVIEW_THRESHOLD", 0.45)))
    flagged = float(config.get("flag_threshold", os.environ.get("SAFETY_FLAG_THRESHOLD", 0.75)))
    maximum = int(config.get("max_keyframes", os.environ.get("SAFETY_MAX_KEYFRAMES", 12)))
    if not 0.0 <= review < flagged <= 1.0:
        raise ValueError("review_threshold and flag_threshold must satisfy 0 <= review < flag <= 1")
    if not 1 <= maximum <= 64:
        raise ValueError("max_keyframes must be between 1 and 64")
    return review, flagged, maximum


def build_annotations(
    scores: Sequence[float],
    times: Sequence[float | None],
    review_threshold: float,
    flag_threshold: float,
) -> list[dict[str, Any]]:
    if not scores:
        return []
    maximum = max(float(score) for score in scores)
    average = sum(float(score) for score in scores) / len(scores)
    label = "flagged" if maximum >= flag_threshold else "review" if maximum >= review_threshold else "safe"
    confidence = maximum if label != "safe" else 1.0 - maximum
    flagged_frames = sum(score >= review_threshold for score in scores)
    output: list[dict[str, Any]] = [
        {
            "namespace": "safety.nsfw",
            "label": label,
            "confidence": round(confidence, 6),
            "value": {
                "max_score": round(maximum, 6),
                "mean_score": round(average, 6),
                "sampled_frames": len(scores),
                "review_frames": flagged_frames,
                "review_threshold": review_threshold,
                "flag_threshold": flag_threshold,
                "policy_version": 1,
            },
        }
    ]
    for score, frame_time in zip(scores, times, strict=True):
        if score < review_threshold:
            continue
        output.append(
            {
                "namespace": "safety.nsfw.frame",
                "label": "flagged" if score >= flag_threshold else "review",
                "confidence": round(float(score), 6),
                "region": {
                    "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0,
                    "frame_time": frame_time, "page_number": None, "kind": "frame",
                },
            }
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
    status, classifier, detail = runtime.snapshot()
    if status == "error":
        return failure(503, "model_error", detail, retryable=True)
    if classifier is None:
        return failure(503, "model_loading", detail, retryable=True)
    try:
        raw_config = work.get("config")
        config: Mapping[str, Any] = raw_config if isinstance(raw_config, Mapping) else {}
        review, flagged, maximum = settings(config)
        images, times = collect(work, maximum)
        deadline = float(work.get("deadline_s", 120.0))
        remaining = deadline - (time.monotonic() - started)
        if remaining <= 0:
            return failure(408, "deadline_exceeded", "deadline elapsed while decoding", retryable=True)
        scores = await asyncio.wait_for(
            asyncio.to_thread(classifier.classify, images), timeout=remaining
        )
        if len(scores) != len(images) or any(not 0.0 <= score <= 1.0 for score in scores):
            raise RuntimeError("classifier returned invalid probabilities")
        emitted = build_annotations(scores, times, review, flagged)
    except asyncio.TimeoutError:
        return failure(408, "deadline_exceeded", "safety inference exceeded deadline", retryable=True)
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
