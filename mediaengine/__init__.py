"""MediaEngine — a local-first, plugin-driven media indexing engine.

The core stores, indexes and searches *arbitrary namespaced annotations*
produced by plugins. It knows nothing about what tags exist. Ship a new
analyzer that emits ``garment.color=red`` or ``scene.setting=beach`` and its
labels become searchable and facetable with no core changes.

Typical embedded use::

    from mediaengine import MediaEngine, load_config

    engine = MediaEngine(load_config("config.yaml"))
    engine.start()
    job = engine.scan("/photos")
    job.wait()
    print(engine.search(Query(text="beach")).total)
    engine.close()
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, load_config  # noqa: E402
from .errors import MediaEngineError  # noqa: E402
from .types import AnnotationSource, MediaType  # noqa: E402

__all__ = [
    "__version__",
    "Config",
    "load_config",
    "MediaEngine",
    "MediaEngineError",
    "MediaType",
    "AnnotationSource",
    "Query",
]


def __getattr__(name: str) -> object:
    """Lazy re-exports so `import mediaengine` stays cheap.

    Importing the engine facade pulls in the pipeline, plugin registry and
    search planner. A caller that only wants `load_config` should not pay for
    that.
    """
    if name == "MediaEngine":
        from .engine import MediaEngine

        return MediaEngine
    if name == "Query":
        from .search.query import Query

        return Query
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
