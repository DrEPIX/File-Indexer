from __future__ import annotations

from typing import Any

from PIL import Image

from mediaengine.plugins.builtin.visual_signals import VisualSignalsAnalyzer, measure_image


class FakeContext:
    media_type = "image"
    config: dict[str, Any] = {}

    def __init__(self, image: Image.Image) -> None:
        self._image = image

    def image(self, *, prefer_size: int | None = 512) -> Image.Image:
        return self._image

    def keyframes(self, *, every: float | None = None) -> list[tuple[float, Any]]:
        return []


def test_measure_image_reports_normalized_signals() -> None:
    measured = measure_image(Image.new("RGB", (200, 100), (240, 80, 30)))
    assert 0.0 <= measured["brightness"] <= 1.0
    assert measured["warmth"] > 0.5
    assert measured["aspect"] == 2.0


def test_visual_agent_emits_searchable_signal_namespaces() -> None:
    analyzer = VisualSignalsAnalyzer()
    annotations = analyzer.analyze(FakeContext(Image.new("RGB", (300, 100), (10, 10, 10))))  # type: ignore[arg-type]
    by_namespace = {item.namespace: item for item in annotations}
    assert by_namespace["visual.exposure"].label == "dark"
    assert by_namespace["visual.composition.aspect"].label == "panoramic"
    assert by_namespace["visual.palette.saturation"].label == "muted"
