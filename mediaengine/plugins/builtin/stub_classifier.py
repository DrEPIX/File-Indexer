"""``example.stub-classifier`` — the reference pixel analyzer.

Exists to prove the extension pipeline end to end with something a human can
see working: it classifies each image's dominant colour into
``color.dominant=blue`` (etc.), which then appears as a search facet the core
was never taught about. It is also the template to copy for a real
classifier — the shape of ``analyze`` is identical whether the label comes
from a histogram or a neural net.

Deliberately dependency-free and **entirely local**: pixels come from the
already-generated thumbnail via ``ctx.image()``, so it never decodes an
original and never touches the network. On a library with thumbnails built,
it runs at thousands of assets per minute on one core.
"""

from __future__ import annotations

import colorsys
from collections.abc import Sequence

from ..contract import Annotation, PluginInfo
from ..context import AnalysisContext

__all__ = ["StubClassifier"]

#: Hue centres (degrees) → names. Boundaries are midpoints between centres.
_HUES: tuple[tuple[float, str], ...] = (
    (0.0, "red"),
    (30.0, "orange"),
    (55.0, "yellow"),
    (110.0, "green"),
    (175.0, "cyan"),
    (225.0, "blue"),
    (280.0, "purple"),
    (325.0, "pink"),
    (360.0, "red"),
)


def _name_hue(degrees: float) -> str:
    best_name = "red"
    best_distance = 361.0
    for centre, name in _HUES:
        distance = abs(degrees - centre)
        if distance < best_distance:
            best_distance = distance
            best_name = name
    return best_name


class StubClassifier:
    """Dominant-colour labels from thumbnail pixels."""

    info = PluginInfo(
        id="example.stub-classifier",
        version="1.0.0",
        accepts=("image", "video"),
        emits=("color",),
        description="Dominant-colour demo analyzer; the template for real classifiers.",
        pixels=True,
        namespaces={"color.dominant": {"display_name": "Dominant colour"}},
    )

    def analyze(self, ctx: AnalysisContext) -> Sequence[Annotation]:
        image = ctx.image(prefer_size=256)
        if image is None:
            return []  # no pixels available (corrupt file, offline volume)

        # 32×32 is plenty to vote on a dominant colour and keeps the per-asset
        # cost trivial even with no numpy in the loop.
        small = image.resize((32, 32))
        votes: dict[str, int] = {}
        total = 0
        for r, g, b in small.getdata():
            h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            if v < 0.16:
                name = "black"
            elif s < 0.12:
                # Desaturated pixels have no meaningful hue; calling a grey
                # sky "blue" is the classic naive-classifier mistake.
                name = "white" if v > 0.85 else "gray"
            else:
                name = _name_hue(h * 360.0)
            votes[name] = votes.get(name, 0) + 1
            total += 1

        if not total:
            return []
        winner, count = max(votes.items(), key=lambda item: item[1])
        coverage = count / total
        return [
            Annotation(
                namespace="color.dominant",
                label=winner,
                # Coverage as confidence: honest about how dominant the
                # dominant colour actually was, and it exercises the
                # confidence-threshold filter path end to end.
                confidence=round(max(0.05, min(0.99, coverage)), 3),
                value={"coverage": round(coverage, 3), "votes": len(votes)},
            )
        ]
