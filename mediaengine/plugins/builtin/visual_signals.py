"""Fast local visual-signal tagging from thumbnail pixels.

This is deliberately small enough to run in-process. It is not a learned
identity model; it computes stable perceptual features that are useful beside
heavier models: exposure, contrast, detail, palette and aspect composition.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..contract import Annotation, PluginInfo
from ..context import AnalysisContext

__all__ = ["VisualSignalsAnalyzer", "measure_image"]


def measure_image(image: Image.Image) -> dict[str, float]:
    """Measure normalized perceptual signals on a bounded RGB thumbnail."""

    rgb = np.asarray(image.convert("RGB").resize((128, 128)), dtype=np.float32) / 255.0
    gray = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    horizontal = np.abs(np.diff(gray, axis=1)).mean()
    vertical = np.abs(np.diff(gray, axis=0)).mean()
    channel_range = rgb.max(axis=2) - rgb.min(axis=2)
    return {
        "brightness": float(gray.mean()),
        "contrast": float(gray.std()),
        "detail": float((horizontal + vertical) / 2.0),
        "saturation": float(channel_range.mean()),
        "warmth": float(rgb[:, :, 0].mean() - rgb[:, :, 2].mean()),
        "aspect": float(image.width / max(1, image.height)),
    }


def _band(value: float, low: float, high: float, labels: tuple[str, str, str]) -> tuple[str, float]:
    if value < low:
        return labels[0], min(0.99, 0.55 + (low - value) / max(low, 0.01) * 0.44)
    if value > high:
        return labels[2], min(0.99, 0.55 + (value - high) / max(1.0 - high, 0.01) * 0.44)
    midpoint = (low + high) / 2.0
    half = max((high - low) / 2.0, 0.01)
    return labels[1], min(0.99, 0.60 + (1.0 - abs(value - midpoint) / half) * 0.35)


class VisualSignalsAnalyzer:
    """Tag objective visual characteristics for images and sampled videos."""

    info = PluginInfo(
        id="core.visual-signals",
        version="1.0.0",
        accepts=("image", "video"),
        emits=(
            "visual.exposure", "visual.contrast", "visual.detail",
            "visual.palette.saturation", "visual.palette.temperature",
            "visual.composition.aspect",
        ),
        description="Fast local exposure, detail, palette and composition tagging.",
        pixels=True,
        frames=True,
        namespaces={
            "visual.exposure": {"display_name": "Exposure"},
            "visual.contrast": {"display_name": "Contrast"},
            "visual.detail": {"display_name": "Visual detail"},
            "visual.palette.saturation": {"display_name": "Saturation"},
            "visual.palette.temperature": {"display_name": "Colour temperature"},
            "visual.composition.aspect": {"display_name": "Aspect composition"},
        },
    )

    def _images(self, ctx: AnalysisContext) -> list[Image.Image]:
        if ctx.media_type == "image":
            image = ctx.image(prefer_size=256)
            return [image] if image is not None else []
        images: list[Image.Image] = []
        for _, path in ctx.keyframes(every=5.0)[:5]:
            try:
                with Image.open(Path(path)) as opened:
                    images.append(opened.convert("RGB"))
            except OSError:
                continue
        return images

    def analyze(self, ctx: AnalysisContext) -> Sequence[Annotation]:
        images = self._images(ctx)
        if not images:
            return []
        rows = [measure_image(image) for image in images]

        def average(key: str) -> float:
            return sum(row[key] for row in rows) / len(rows)

        exposure = _band(average("brightness"), 0.22, 0.82, ("dark", "balanced", "bright"))
        contrast = _band(average("contrast"), 0.10, 0.28, ("low", "balanced", "high"))
        detail = _band(average("detail"), 0.025, 0.11, ("soft", "normal", "high-detail"))
        saturation = _band(average("saturation"), 0.10, 0.42, ("muted", "balanced", "colorful"))
        warmth_value = average("warmth")
        temperature = _band(warmth_value + 0.5, 0.44, 0.56, ("cool", "neutral", "warm"))
        aspect = average("aspect")
        if aspect >= 2.0:
            aspect_label = "panoramic"
        elif aspect > 1.12:
            aspect_label = "landscape"
        elif aspect < 0.89:
            aspect_label = "portrait"
        else:
            aspect_label = "square"

        metrics: dict[str, Any] = {
            "brightness": round(average("brightness"), 4),
            "contrast": round(average("contrast"), 4),
            "detail": round(average("detail"), 4),
            "saturation": round(average("saturation"), 4),
            "warmth": round(warmth_value, 4),
            "sampled_frames": len(images),
        }
        return [
            Annotation("visual.exposure", exposure[0], confidence=round(exposure[1], 4), value=metrics),
            Annotation("visual.contrast", contrast[0], confidence=round(contrast[1], 4)),
            Annotation("visual.detail", detail[0], confidence=round(detail[1], 4)),
            Annotation("visual.palette.saturation", saturation[0], confidence=round(saturation[1], 4)),
            Annotation("visual.palette.temperature", temperature[0], confidence=round(temperature[1], 4)),
            Annotation("visual.composition.aspect", aspect_label, confidence=0.99, value={"aspect": round(aspect, 4)}),
        ]
