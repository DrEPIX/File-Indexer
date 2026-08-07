"""Ingest core: walk, identify, extract, derive, index.

The stages are separate modules rather than one indexer class because each has
to be independently resumable and independently testable. :mod:`.pipeline`
composes them; nothing else in the package reaches across stage boundaries.

Heavier members (:mod:`.derivatives`, :mod:`.pipeline`, :mod:`.extractors`)
resolve lazily, so importing :mod:`mediaengine.core` for a glob matcher does
not pull in Pillow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .control import CancelToken, ProgressCallback, ProgressEvent, ProgressReporter
from .globs import GlobMatcher, compile_glob, to_relative_posix
from .identity import (
    Detection,
    HashResult,
    detect_media_type,
    hash_bytes,
    hash_dirname,
    hash_file,
    perceptual_hash,
    sniff_bytes,
)
from .procs import CommandResult, find_binary, have_binary, run_command
from .walker import BatchProducer, WalkBatch, WalkEntry, Walker, WalkStats

if TYPE_CHECKING:  # pragma: no cover - import-time cost avoidance
    from .derivatives import DerivativeBuilder, DerivativeResult
    from .pipeline import IngestPipeline, ScanResult
    from .sidecars import SidecarLinker

__all__ = [
    "CancelToken",
    "ProgressEvent",
    "ProgressReporter",
    "ProgressCallback",
    "GlobMatcher",
    "compile_glob",
    "to_relative_posix",
    "HashResult",
    "Detection",
    "hash_file",
    "hash_bytes",
    "hash_dirname",
    "detect_media_type",
    "sniff_bytes",
    "perceptual_hash",
    "CommandResult",
    "run_command",
    "find_binary",
    "have_binary",
    "Walker",
    "WalkEntry",
    "WalkBatch",
    "WalkStats",
    "BatchProducer",
    "DerivativeBuilder",
    "DerivativeResult",
    "SidecarLinker",
    "IngestPipeline",
    "ScanResult",
]

_LAZY: dict[str, str] = {
    "DerivativeBuilder": ".derivatives",
    "DerivativeResult": ".derivatives",
    "SidecarLinker": ".sidecars",
    "IngestPipeline": ".pipeline",
    "ScanResult": ".pipeline",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)
