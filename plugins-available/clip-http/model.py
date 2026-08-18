"""Lazy CLIP model loading and batched image/text inference."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol, Sequence, cast

from PIL import Image


MODEL_ID = "openai/clip-vit-base-patch32"

# A broad starter taxonomy, intentionally limited to observable content and
# activity. It avoids identity, ethnicity, health, religion and other
# sensitive-trait inference. Operators can replace it through per-plugin
# config without changing code or the database schema.
DEFAULT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "format": (
        "animation", "gameplay", "live performance", "screen recording",
        "security camera footage", "slideshow", "tutorial", "vlog",
        "news broadcast", "interview", "documentary", "product demonstration",
    ),
    "subject": (
        "animals", "artwork", "documents and text", "food", "nature",
        "people", "technology", "vehicles", "architecture", "clothing",
        "toys", "tools", "plants", "water",
    ),
    "activity": (
        "celebration", "conversation", "cooking", "exercise", "making music",
        "shopping", "sports", "computer use", "travel", "driving", "dancing",
        "construction", "crafting", "cleaning", "eating", "giving a presentation",
    ),
    "scene": (
        "city", "home", "nature", "office", "outdoors", "road",
        "sports venue", "stage", "beach", "forest", "mountains", "restaurant",
        "classroom", "workshop", "store", "vehicle interior",
    ),
    "style": (
        "black and white", "cinematic", "handheld camera", "aerial footage",
        "close-up shot", "wide shot",
    ),
}
DEFAULT_GENRES: tuple[str, ...] = (
    "horror film", "comedy", "drama", "action film", "science fiction",
    "fantasy", "romance", "thriller", "mystery", "documentary film",
    "internet meme", "reaction meme", "music video", "news report",
    "sports highlight", "advertisement", "family home video", "travel video",
    "educational video", "experimental art video",
)
DEFAULT_LABELS = (
    *(label for labels in DEFAULT_CATEGORIES.values() for label in labels),
    *DEFAULT_GENRES,
)


@dataclass(frozen=True, slots=True)
class EncodedBatch:
    """Native image vectors plus one confidence mapping per image."""

    embeddings: list[list[float]]
    label_scores: list[dict[str, float]]


class Encoder(Protocol):
    """Inference interface used by the HTTP layer and its fake contract model."""

    @property
    def embedding_dim(self) -> int:
        """Return the model's native projection dimension."""

    @property
    def detail(self) -> str:
        """Return a concise health description."""

    def encode(self, images: Sequence[Image.Image], labels: Sequence[str]) -> EncodedBatch:
        """Encode a batch in one forward pass."""


class ClipEncoder:
    """Transformers CLIP wrapper with automatic CUDA-to-CPU fallback."""

    def __init__(self, *, model_id: str = MODEL_ID, device: str | None = None) -> None:
        self.model_id = model_id
        self.requested_device = device or os.environ.get("CLIP_DEVICE", "auto")
        self._device = "unloaded"
        self._model: Any = None
        self._processor: Any = None
        self._torch: Any = None
        self._embedding_dim = 0

    @property
    def embedding_dim(self) -> int:
        """Return the model's discovered native projection dimension."""

        if self._embedding_dim <= 0:
            raise RuntimeError("model is not loaded")
        return self._embedding_dim

    @property
    def detail(self) -> str:
        """Describe the loaded model and selected device."""

        return f"{self.model_id} on {self._device}"

    def load(self) -> "ClipEncoder":
        """Download/cache weights if needed, select a device, and load once."""

        if self._model is not None:
            return self
        import torch
        from transformers import CLIPModel, CLIPProcessor

        if self.requested_device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        elif self.requested_device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        else:
            device = self.requested_device
        model = CLIPModel.from_pretrained(self.model_id)
        processor = CLIPProcessor.from_pretrained(self.model_id)
        model.eval()
        model.to(device)
        projection_dim = int(model.config.projection_dim)
        if projection_dim <= 0:
            raise RuntimeError(f"invalid CLIP projection dimension: {projection_dim}")
        self._torch = torch
        self._model = model
        self._processor = processor
        self._device = device
        self._embedding_dim = projection_dim
        return self

    def encode(self, images: Sequence[Image.Image], labels: Sequence[str]) -> EncodedBatch:
        """Encode all images together and optionally score zero-shot prompts."""

        if not images:
            return EncodedBatch(embeddings=[], label_scores=[])
        self.load()
        torch = self._torch
        processor = self._processor
        model = self._model
        image_inputs = processor(images=list(images), return_tensors="pt")
        image_inputs = {key: value.to(self._device) for key, value in image_inputs.items()}
        with torch.inference_mode():
            image_features = model.get_image_features(**image_inputs)
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            score_rows: list[dict[str, float]] = []
            if labels:
                prompts = [f"a representative video frame showing {label}" for label in labels]
                text_inputs = processor(text=prompts, return_tensors="pt", padding=True)
                text_inputs = {key: value.to(self._device) for key, value in text_inputs.items()}
                text_features = model.get_text_features(**text_inputs)
                text_features = text_features / text_features.norm(dim=-1, keepdim=True)
                probabilities = (100.0 * image_features @ text_features.T).softmax(dim=-1)
                for row in probabilities.detach().cpu().tolist():
                    score_rows.append({label: float(score) for label, score in zip(labels, row, strict=True)})
            else:
                score_rows = [{} for _ in images]
        vectors = cast(list[list[float]], image_features.detach().cpu().to(torch.float32).tolist())
        if any(len(vector) != self.embedding_dim for vector in vectors):
            raise RuntimeError("model returned an inconsistent embedding dimension")
        return EncodedBatch(embeddings=vectors, label_scores=score_rows)


def load_default_encoder() -> Encoder:
    """Factory used by the background loader and replaceable in tests."""

    return ClipEncoder().load()
