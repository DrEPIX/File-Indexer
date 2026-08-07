"""The plugin system: contract, context, registry, runner.

Split follows the trust boundary. :mod:`.contract` and :mod:`.context` are the
plugin-facing API — no database types leak through them. :mod:`.registry` and
:mod:`.runner` are engine-side: discovery, enablement, validation, producer
attribution and the commit path. Plugins return annotations; they never write.
"""

from __future__ import annotations

from .contract import PROTOCOL, Analyzer, Annotation, PluginInfo, Region, validate_annotations
from .context import AnalysisContext
from .http import HttpAnalyzer, plugin_info_from_manifest
from .registry import LoadedPlugin, PluginRegistry
from .runner import BackfillResult, PluginRunner

__all__ = [
    "PROTOCOL",
    "Annotation",
    "Region",
    "PluginInfo",
    "Analyzer",
    "validate_annotations",
    "AnalysisContext",
    "HttpAnalyzer",
    "plugin_info_from_manifest",
    "PluginRegistry",
    "LoadedPlugin",
    "PluginRunner",
    "BackfillResult",
]
