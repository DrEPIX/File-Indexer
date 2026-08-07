"""``core.exif-gps`` — geotag presence as a searchable annotation.

The pipeline already stores coordinates in ``asset_locations`` (that is
technical metadata, and spatial queries run on the R*Tree). What this plugin
adds is the *searchable claim*: ``gps:geotagged`` makes location a facet and
lets ``has:gps`` behave identically to every plugin-defined filter, through
the same annotation machinery, instead of being a special case bolted onto
the query language.

Local-only, metadata-only, no file access.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..contract import Annotation, PluginInfo
from ..context import AnalysisContext

__all__ = ["ExifGpsAnalyzer"]


class ExifGpsAnalyzer:
    """Emits ``gps=geotagged`` for assets that carry a location."""

    info = PluginInfo(
        id="core.exif-gps",
        version="1.0.0",
        accepts=("image", "video"),
        emits=("gps",),
        description="Marks geotagged assets so location works as a facet.",
        metadata_only=True,
        namespaces={"gps": {"display_name": "Location", "value_type": "geo"}},
    )

    def analyze(self, ctx: AnalysisContext) -> Sequence[Annotation]:
        location = ctx.location
        if location is None:
            return []  # nothing to say — a valid, successful result
        return [
            Annotation(
                namespace="gps",
                label="geotagged",
                value={
                    "latitude": location.get("latitude"),
                    "longitude": location.get("longitude"),
                    "altitude_m": location.get("altitude_m"),
                    "source": location.get("source"),
                },
            )
        ]
