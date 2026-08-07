"""Dedicated local image-safety classifier with CUDA/CPU fallback."""

from __future__ import annotations

import os
from typing import Any, Protocol, Sequence, cast

from PIL import Image


MODEL_ID = os.environ.get("SAFETY_MODEL_ID", "Falconsai/nsfw_image_detection")


class SafetyClassifier(Protocol):
    @property
    def detail(self) -> str: ...

    def classify(self, images: Sequence[Image.Image]) -> list[float]: ...


class TransformersSafetyClassifier:
    """Batched ViT classifier returning an NSFW probability per frame."""

    def __init__(self, *, model_id: str = MODEL_ID, device: str | None = None) -> None:
        self.model_id = model_id
        self.requested_device = device or os.environ.get("SAFETY_DEVICE", "auto")
        self._device = "unloaded"
        self._torch: Any = None
        self._model: Any = None
        self._processor: Any = None
        self._unsafe_indices: list[int] = []

    @property
    def detail(self) -> str:
        return f"{self.model_id} on {self._device}"

    def load(self) -> "TransformersSafetyClassifier":
        if self._model is not None:
            return self
        import torch
        from transformers import (
            AutoImageProcessor,
            AutoModelForImageClassification,
        )

        if self.requested_device not in {"auto", "cpu", "cuda"}:
            raise ValueError("SAFETY_DEVICE must be auto, cpu, or cuda")
        use_cuda = self.requested_device in {"auto", "cuda"} and torch.cuda.is_available()
        device = "cuda" if use_cuda else "cpu"
        model = AutoModelForImageClassification.from_pretrained(self.model_id)
        processor = AutoImageProcessor.from_pretrained(self.model_id)
        labels = {
            int(index): str(label).lower()
            for index, label in dict(model.config.id2label).items()
        }
        unsafe_words = ("nsfw", "unsafe", "explicit", "porn", "sexy", "hentai")
        unsafe = [index for index, label in labels.items() if any(word in label for word in unsafe_words)]
        if not unsafe:
            raise RuntimeError(f"model labels do not identify an NSFW class: {labels}")
        self._torch = torch
        self._model = model.eval().to(device)
        self._processor = processor
        self._unsafe_indices = unsafe
        self._device = device
        return self

    def classify(self, images: Sequence[Image.Image]) -> list[float]:
        if not images:
            return []
        self.load()
        inputs = self._processor(images=list(images), return_tensors="pt")
        inputs = {name: tensor.to(self._device) for name, tensor in inputs.items()}
        with self._torch.inference_mode():
            logits = self._model(**inputs).logits
            probabilities = self._torch.softmax(logits, dim=-1)
            scores = probabilities[:, self._unsafe_indices].sum(dim=-1)
        return cast(list[float], scores.detach().cpu().to(self._torch.float32).tolist())


def load_default_classifier() -> SafetyClassifier:
    return TransformersSafetyClassifier().load()
