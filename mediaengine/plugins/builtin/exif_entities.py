"""``core.exif-entities`` — embedded metadata as searchable annotations.

This plugin is the boundary-keeper. The *extractors* store raw EXIF verbatim
and promote a fixed set of technical columns; deciding that ``EXIF:Make``
deserves to be a searchable, facetable label called ``camera.make`` is a
taxonomy decision, and taxonomy decisions belong to plugins. Disable this
plugin and the core still works; replace it with your own and your vocabulary
wins. That is the property the whole engine is built around.

Local-only: reads what the scan already extracted. Never opens the file,
never decodes a pixel, costs microseconds per asset.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..contract import Annotation, PluginInfo
from ..context import AnalysisContext

__all__ = ["ExifEntitiesAnalyzer"]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    return text or None


class ExifEntitiesAnalyzer:
    """Camera, lens and capture-settings entities from stored metadata."""

    info = PluginInfo(
        id="core.exif-entities",
        version="1.0.0",
        accepts=("image", "video"),
        emits=("camera", "lens", "capture"),
        description="Camera make/model, lens, and capture-settings labels from EXIF.",
        metadata_only=True,
        namespaces={
            "camera.make": {"display_name": "Camera make"},
            "camera.model": {"display_name": "Camera model"},
            "lens.model": {"display_name": "Lens"},
            "capture.iso": {"display_name": "ISO", "value_type": "numeric"},
        },
    )

    def analyze(self, ctx: AnalysisContext) -> Sequence[Annotation]:
        metadata = ctx.metadata
        out: list[Annotation] = []

        make = _clean(metadata.get("camera_make"))
        model = _clean(metadata.get("camera_model"))
        lens = _clean(metadata.get("lens_model"))
        if make:
            out.append(Annotation(namespace="camera.make", label=make))
        if model:
            # "Canon EOS R5", not "EOS R5": models only make sense qualified,
            # and two vendors reusing a model name would merge in facets.
            full = f"{make} {model}" if make and not model.startswith(make) else model
            out.append(Annotation(namespace="camera.model", label=full))
        if lens:
            out.append(Annotation(namespace="lens.model", label=lens))

        iso = metadata.get("iso")
        if isinstance(iso, int) and 0 < iso < 10_000_000:
            # Bucketed, because a facet of 400 distinct ISO values is noise.
            bucket = (
                "low (<=200)" if iso <= 200
                else "medium (201-800)" if iso <= 800
                else "high (801-3200)" if iso <= 3200
                else "very high (>3200)"
            )
            out.append(
                Annotation(namespace="capture.iso", label=bucket, value={"iso": iso})
            )
        return out
