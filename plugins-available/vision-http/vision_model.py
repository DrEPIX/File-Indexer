"""Lazy CUDA/CPU face embedding and object detection implementation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol, Sequence, cast

from PIL import Image


MODEL_ID = "facenet-vggface2+fasterrcnn-resnet50-fpn-v2"
FACE_EMBEDDING_DIM = 512


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

    @property
    def embedding_dim(self) -> int:
        return FACE_EMBEDDING_DIM

    @property
    def detail(self) -> str:
        return f"{MODEL_ID} on {self._device}"

    def load(self) -> "TorchVisionAnalyzer":
        if self._object_model is not None:
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
        weights = FasterRCNN_ResNet50_FPN_V2_Weights.DEFAULT
        object_model = fasterrcnn_resnet50_fpn_v2(weights=weights).eval().to(device)

        self._torch = torch
        self._mtcnn = mtcnn
        self._face_model = face_model
        self._object_model = object_model
        self._object_transform = weights.transforms()
        self._categories = cast(list[str], weights.meta["categories"])
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
