"""Lazy CUDA/CPU face embedding and object detection implementation."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence, cast

from PIL import Image


OBJECT_BACKEND = os.environ.get("VISION_OBJECT_BACKEND", "torchvision").lower()
CAFFE_MODEL_PATH = os.environ.get("VISION_CAFFE_MODEL", "")
CAFFE_PROTOTXT_PATH = os.environ.get("VISION_CAFFE_PROTOTXT", "")
CAFFE_MODELS_CONFIG = os.environ.get("VISION_CAFFE_MODELS_CONFIG", "")


def _configured_caffe_ids() -> list[str]:
    path = Path(CAFFE_MODELS_CONFIG)
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("models", []) if isinstance(payload, dict) else []
            return [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
        except (OSError, ValueError):
            return [f"invalid-config:{path.name}"]
    return [Path(CAFFE_MODEL_PATH).stem] if CAFFE_MODEL_PATH else ["unconfigured"]


_CAFFE_ID = ",".join(_configured_caffe_ids())
MODEL_ID = (
    "facenet-vggface2+"
    + (
        "fasterrcnn-resnet50-fpn-v2"
        if OBJECT_BACKEND == "torchvision"
        else f"caffe-ssd:{_CAFFE_ID}"
        if OBJECT_BACKEND == "caffe"
        else f"fasterrcnn-resnet50-fpn-v2+caffe-ssd:{_CAFFE_ID}"
    )
)
FACE_EMBEDDING_DIM = 512
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FaceResult:
    confidence: float
    box: tuple[float, float, float, float]
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class ObjectResult:
    label: str
    confidence: float
    box: tuple[float, float, float, float]
    backend: str = "torchvision"
    model_id: str = "fasterrcnn-resnet50-fpn-v2"


@dataclass(frozen=True, slots=True)
class FrameResult:
    faces: list[FaceResult]
    objects: list[ObjectResult]


class VisionAnalyzer(Protocol):
    @property
    def embedding_dim(self) -> int: ...

    @property
    def detail(self) -> str: ...

    def analyze(
        self,
        images: Sequence[Image.Image],
        *,
        face_threshold: float,
        object_threshold: float,
        max_faces: int,
        max_objects: int,
    ) -> list[FrameResult]: ...


class CaffeDetector:
    """Generic OpenCV-DNN adapter for Caffe SSD detection networks.

    Expected output is the common ``[image_id, class_id, confidence,
    x1, y1, x2, y2]`` layout with normalized coordinates. Model files remain
    operator-owned and are never downloaded or modified by this adapter.
    """

    def __init__(
        self,
        *,
        prototxt: str | Path,
        model: str | Path,
        labels: Sequence[str],
        model_id: str | None = None,
        input_size: tuple[int, int] = (300, 300),
        scale: float = 0.007843,
        mean: tuple[float, float, float] = (127.5, 127.5, 127.5),
        swap_rb: bool = False,
        minimum_confidence: float = 0.0,
        max_detections: int = 100,
        net: Any | None = None,
        cv2_module: Any | None = None,
    ) -> None:
        self.prototxt = Path(prototxt)
        self.model = Path(model)
        self.labels = [str(label).strip() for label in labels]
        self.model_id = model_id or self.model.stem
        self.input_size = input_size
        self.scale = scale
        self.mean = mean
        self.swap_rb = swap_rb
        self.minimum_confidence = minimum_confidence
        self.max_detections = max_detections
        self._net = net
        self._cv2 = cv2_module
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "CaffeDetector":
        prototxt = Path(CAFFE_PROTOTXT_PATH)
        model = Path(CAFFE_MODEL_PATH)
        label_path = Path(os.environ.get("VISION_CAFFE_LABELS", ""))
        missing = [str(path) for path in (prototxt, model, label_path) if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Caffe backend requires readable VISION_CAFFE_PROTOTXT, "
                f"VISION_CAFFE_MODEL and VISION_CAFFE_LABELS files; missing: {missing}"
            )
        labels = [
            line.strip()
            for line in label_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not labels:
            raise ValueError("VISION_CAFFE_LABELS contains no labels")
        return cls(
            prototxt=prototxt,
            model=model,
            labels=labels,
            model_id=model.stem,
            input_size=(
                int(os.environ.get("VISION_CAFFE_INPUT_WIDTH", "300")),
                int(os.environ.get("VISION_CAFFE_INPUT_HEIGHT", "300")),
            ),
            scale=float(os.environ.get("VISION_CAFFE_SCALE", "0.007843")),
            mean=_parse_mean(os.environ.get("VISION_CAFFE_MEAN", "127.5,127.5,127.5")),
            swap_rb=os.environ.get("VISION_CAFFE_SWAP_RB", "false").lower() in {"1", "true", "yes"},
            minimum_confidence=float(os.environ.get("VISION_CAFFE_THRESHOLD", "0")),
            max_detections=int(os.environ.get("VISION_CAFFE_MAX_DETECTIONS", "100")),
        )

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, base: Path) -> "CaffeDetector":
        """Build one detector from a multi-model JSON manifest entry."""

        model_id = str(value.get("id") or "").strip()
        if not model_id:
            raise ValueError("each Caffe model requires a non-empty id")

        def resolve(key: str, alternate: str | None = None) -> Path:
            raw = value.get(key, value.get(alternate)) if alternate else value.get(key)
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"Caffe model {model_id!r} requires {key}")
            path = Path(raw)
            return path if path.is_absolute() else (base / path).resolve()

        prototxt = resolve("prototxt")
        model = resolve("model", "weights")
        labels_value = value.get("labels")
        if isinstance(labels_value, list):
            labels = [str(item).strip() for item in labels_value if str(item).strip()]
        elif isinstance(labels_value, str):
            label_path = Path(labels_value)
            label_path = label_path if label_path.is_absolute() else (base / label_path).resolve()
            labels = _read_labels(label_path)
        else:
            raise ValueError(f"Caffe model {model_id!r} requires labels as a path or array")
        if str(value.get("output_format", "ssd7")) != "ssd7":
            raise ValueError(f"Caffe model {model_id!r}: only output_format='ssd7' is currently supported")
        for path in (prototxt, model):
            if not path.is_file():
                raise FileNotFoundError(f"Caffe model {model_id!r} file not found: {path}")
        size_value = value.get("input_size", [300, 300])
        if not isinstance(size_value, list) or len(size_value) != 2:
            raise ValueError(f"Caffe model {model_id!r} input_size must be [width, height]")
        mean_value = value.get("mean", [127.5, 127.5, 127.5])
        if not isinstance(mean_value, list) or len(mean_value) != 3:
            raise ValueError(f"Caffe model {model_id!r} mean must contain three numbers")
        return cls(
            prototxt=prototxt,
            model=model,
            labels=labels,
            model_id=model_id,
            input_size=(int(size_value[0]), int(size_value[1])),
            scale=float(value.get("scale", 0.007843)),
            mean=(float(mean_value[0]), float(mean_value[1]), float(mean_value[2])),
            swap_rb=bool(value.get("swap_rb", False)),
            minimum_confidence=float(value.get("threshold", 0.0)),
            max_detections=max(1, int(value.get("max_detections", 100))),
        )

    def load(self) -> "CaffeDetector":
        if self._net is not None:
            return self
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "Caffe detection requires opencv-python-headless"
            ) from exc
        self._cv2 = cv2
        self._net = cv2.dnn.readNetFromCaffe(str(self.prototxt), str(self.model))
        return self

    def _label(self, class_id: int) -> str:
        # Label files occur in both forms: with an explicit background entry
        # at index 0, and class-only lists where class 1 is labels[0].
        if self.labels and self.labels[0].lower() in {"background", "__background__"}:
            index = class_id
        else:
            index = class_id - 1
        return self.labels[index] if 0 <= index < len(self.labels) else f"class-{class_id}"

    def detect(self, image: Image.Image, *, threshold: float, maximum: int) -> list[ObjectResult]:
        self.load()
        import numpy as np

        assert self._cv2 is not None and self._net is not None
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        # OpenCV's default channel order is BGR. ``swapRB`` remains configurable
        # because custom Caffe deployments do not all share MobileNet-SSD's
        # preprocessing convention.
        blob = self._cv2.dnn.blobFromImage(
            rgb,
            scalefactor=self.scale,
            size=self.input_size,
            mean=self.mean,
            swapRB=self.swap_rb,
            crop=False,
        )
        with self._lock:
            self._net.setInput(blob)
            raw = np.asarray(self._net.forward())
        if raw.size % 7:
            raise RuntimeError(f"unsupported Caffe detection output shape {raw.shape}")
        width, height = image.size
        output: list[ObjectResult] = []
        effective_threshold = max(threshold, self.minimum_confidence)
        effective_maximum = min(maximum, self.max_detections)
        for row in raw.reshape(-1, 7):
            confidence = float(row[2])
            if confidence < effective_threshold:
                continue
            class_id = int(row[1])
            x1 = float(row[3]) * width
            y1 = float(row[4]) * height
            x2 = float(row[5]) * width
            y2 = float(row[6]) * height
            output.append(
                ObjectResult(
                    label=self._label(class_id),
                    confidence=confidence,
                    box=(x1, y1, x2, y2),
                    backend="caffe",
                    model_id=self.model_id,
                )
            )
            if len(output) >= effective_maximum:
                break
        return output


def _parse_mean(raw: str) -> tuple[float, float, float]:
    values = tuple(float(part.strip()) for part in raw.split(","))
    if len(values) != 3:
        raise ValueError("VISION_CAFFE_MEAN must contain three comma-separated numbers")
    return values


def _read_labels(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Caffe labels file not found: {path}")
    labels = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not labels:
        raise ValueError(f"Caffe labels file is empty: {path}")
    return labels


def configured_caffe_detectors() -> tuple[list[CaffeDetector], list[str]]:
    """Load all configured model specifications, isolating invalid entries."""

    config_path = Path(CAFFE_MODELS_CONFIG)
    if CAFFE_MODELS_CONFIG and not config_path.is_file():
        raise FileNotFoundError(f"VISION_CAFFE_MODELS_CONFIG not found: {config_path}")
    if not config_path.is_file():
        return [CaffeDetector.from_environment()], []
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("VISION_CAFFE_MODELS_CONFIG must contain a non-empty models array")
    detectors: list[CaffeDetector] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, dict):
                raise ValueError("entry must be an object")
            if row.get("enabled", True) is False:
                continue
            detectors.append(CaffeDetector.from_mapping(row, base=config_path.parent))
        except Exception as exc:  # one model must not disable its siblings
            errors.append(f"models[{index}]: {type(exc).__name__}: {exc}")
    if not detectors:
        raise RuntimeError("no valid enabled Caffe models: " + "; ".join(errors))
    return detectors, errors


class TorchVisionAnalyzer:
    """Facenet face vectors plus torchvision COCO detections on one device."""

    def __init__(self, *, device: str | None = None) -> None:
        self.requested_device = device or os.environ.get("VISION_DEVICE", "auto")
        self._device = "unloaded"
        self._torch: Any = None
        self._mtcnn: Any = None
        self._face_model: Any = None
        self._object_model: Any = None
        self._object_transform: Any = None
        self._categories: list[str] = []
        self._object_backend = OBJECT_BACKEND
        self._caffe: list[CaffeDetector] = []
        self._caffe_errors: list[str] = []

    @property
    def embedding_dim(self) -> int:
        return FACE_EMBEDDING_DIM

    @property
    def detail(self) -> str:
        suffix = f"; {len(self._caffe)} Caffe model(s)"
        if self._caffe_errors:
            suffix += f", {len(self._caffe_errors)} rejected"
        return f"{MODEL_ID} on {self._device}{suffix if self._caffe else ''}"

    def load(self) -> "TorchVisionAnalyzer":
        if self._face_model is not None:
            return self
        import torch  # type: ignore[import-not-found]
        from facenet_pytorch import InceptionResnetV1, MTCNN  # type: ignore[import-not-found]
        from torchvision.models.detection import (  # type: ignore[import-not-found]
            FasterRCNN_ResNet50_FPN_V2_Weights,
            fasterrcnn_resnet50_fpn_v2,
        )

        if self.requested_device not in {"auto", "cpu", "cuda"}:
            raise ValueError("VISION_DEVICE must be auto, cpu, or cuda")
        use_cuda = self.requested_device in {"auto", "cuda"} and torch.cuda.is_available()
        device = "cuda" if use_cuda else "cpu"

        # MTCNN provides aligned crops; VGGFace2 embeddings are the vectors the
        # core clusters and later associates with user-confirmed identities.
        mtcnn = MTCNN(keep_all=True, post_process=True, device=device)
        face_model = InceptionResnetV1(pretrained="vggface2").eval().to(device)
        if self._object_backend not in {"torchvision", "caffe", "both"}:
            raise ValueError("VISION_OBJECT_BACKEND must be torchvision, caffe, or both")
        weights = None
        object_model = None
        if self._object_backend in {"torchvision", "both"}:
            weights = FasterRCNN_ResNet50_FPN_V2_Weights.DEFAULT
            object_model = fasterrcnn_resnet50_fpn_v2(weights=weights).eval().to(device)
        if self._object_backend in {"caffe", "both"}:
            candidates, self._caffe_errors = configured_caffe_detectors()
            for detector in candidates:
                try:
                    self._caffe.append(detector.load())
                except Exception as exc:
                    message = f"{detector.model_id}: {type(exc).__name__}: {exc}"
                    self._caffe_errors.append(message)
                    _LOG.error("Caffe model rejected: %s", message)
            if self._object_backend == "caffe" and not self._caffe:
                raise RuntimeError("all Caffe models failed to load: " + "; ".join(self._caffe_errors))

        self._torch = torch
        self._mtcnn = mtcnn
        self._face_model = face_model
        self._object_model = object_model
        self._object_transform = weights.transforms() if weights is not None else None
        self._categories = cast(list[str], weights.meta["categories"]) if weights is not None else []
        self._device = device
        return self

    def _faces(self, image: Image.Image, threshold: float, maximum: int) -> list[FaceResult]:
        torch = self._torch
        boxes, probabilities = self._mtcnn.detect(image)
        if boxes is None or probabilities is None:
            return []
        candidates = [
            (box, float(probability))
            for box, probability in zip(boxes, probabilities, strict=True)
            if probability is not None and float(probability) >= threshold
        ]
        candidates.sort(key=lambda item: item[1], reverse=True)
        candidates = candidates[:maximum]
        if not candidates:
            return []
        selected_boxes = [box for box, _ in candidates]
        aligned = self._mtcnn.extract(image, selected_boxes, save_path=None)
        if aligned is None:
            return []
        with torch.inference_mode():
            vectors = self._face_model(aligned.to(self._device))
            vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
        raw_vectors = cast(list[list[float]], vectors.detach().cpu().to(torch.float32).tolist())
        return [
            FaceResult(
                confidence=confidence,
                box=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                embedding=vector,
            )
            for (box, confidence), vector in zip(candidates, raw_vectors, strict=True)
        ]

    def _objects(self, image: Image.Image, threshold: float, maximum: int) -> list[ObjectResult]:
        results: list[ObjectResult] = []
        if self._object_model is not None and self._object_transform is not None:
            results.extend(self._torchvision_objects(image, threshold, maximum))
        for detector in self._caffe:
            try:
                results.extend(
                    detector.detect(image, threshold=threshold, maximum=maximum)
                )
            except Exception as exc:
                _LOG.error("Caffe inference failed for %s: %s", detector.model_id, exc)
        results.sort(key=lambda item: item.confidence, reverse=True)
        return results[:maximum]

    def _torchvision_objects(
        self, image: Image.Image, threshold: float, maximum: int
    ) -> list[ObjectResult]:
        torch = self._torch
        tensor = self._object_transform(image).to(self._device)
        with torch.inference_mode():
            prediction = self._object_model([tensor])[0]
        results: list[ObjectResult] = []
        rows = zip(prediction["boxes"], prediction["scores"], prediction["labels"], strict=True)
        for box, score, label_id in rows:
            confidence = float(score.detach().cpu())
            if confidence < threshold:
                continue
            numeric_label = int(label_id.detach().cpu())
            label = self._categories[numeric_label] if numeric_label < len(self._categories) else str(numeric_label)
            coordinates = cast(list[float], box.detach().cpu().to(torch.float32).tolist())
            results.append(
                ObjectResult(
                    label,
                    confidence,
                    (coordinates[0], coordinates[1], coordinates[2], coordinates[3]),
                    "torchvision",
                    "fasterrcnn-resnet50-fpn-v2",
                )
            )
            if len(results) >= maximum:
                break
        return results

    def analyze(
        self,
        images: Sequence[Image.Image],
        *,
        face_threshold: float,
        object_threshold: float,
        max_faces: int,
        max_objects: int,
    ) -> list[FrameResult]:
        self.load()
        return [
            FrameResult(
                faces=self._faces(image, face_threshold, max_faces),
                objects=self._objects(image, object_threshold, max_objects),
            )
            for image in images
        ]


def load_default_analyzer() -> VisionAnalyzer:
    return TorchVisionAnalyzer().load()
