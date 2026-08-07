"""The three reference analyzers the spec mandates — and only those.

Real models (CLIP, faces, OCR) live outside the package as subprocess or HTTP
plugins under ``plugins-available/``; that boundary is deliberate. These three
exist to prove the machinery and to be copied.
"""

from __future__ import annotations

from .exif_entities import ExifEntitiesAnalyzer
from .exif_gps import ExifGpsAnalyzer
from .stub_classifier import StubClassifier

__all__ = ["ExifEntitiesAnalyzer", "ExifGpsAnalyzer", "StubClassifier"]
