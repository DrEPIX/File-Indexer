"""The plugin-facing contract: what an analyzer receives and returns.

This module is the Python rendering of ``_coordination/CONTRACTS.md`` §1–§2
(frozen at v1.1). It deliberately imports nothing from the database layer —
a plugin's world is this module plus :class:`AnalysisContext`, so a plugin
author never sees a repository, a connection, or SQL. The runner converts
these shapes into repository calls; plugins never write to the database.

Validation lives here too, because the rule is "the core enforces on
receipt" and every transport must apply exactly the same checks. A violation
raises :class:`~mediaengine.errors.PluginContractError`, which fails the task
loudly — a model emitting confidence 1.4 is a bug someone wants to see, not
clamp.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..errors import PluginContractError

__all__ = [
    "PROTOCOL",
    "Region",
    "Annotation",
    "PluginInfo",
    "Analyzer",
    "validate_annotations",
]

#: Wire protocol identifier, echoed in every HTTP and JSONL payload.
PROTOCOL = "mediaengine.analyzer/1"

_ID_RE = re.compile(r"^[a-z0-9._-]+$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9._-]{1,64}$")

_MAX_LABEL = 256
_MAX_VALUE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class Region:
    """A normalized bounding box, contract §1.

    Coordinates are fractions of the media's dimensions, origin top-left —
    never pixels, so a region drawn on a thumbnail lands correctly on the
    original.
    """

    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    frame_time: float | None = None
    page_number: int | None = None
    kind: str | None = None


@dataclass(slots=True)
class Annotation:
    """One claim a plugin makes about an asset, contract §1."""

    namespace: str
    label: str
    value: dict[str, Any] | None = None
    confidence: float | None = None
    region: Region | None = None
    embedding: Sequence[float] | None = None


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """A plugin's manifest, contract §5, as in-process plugins declare it.

    Out-of-process plugins carry the same fields in ``plugin.toml`` or their
    ``/manifest`` response; this is the common in-memory form all three
    transports normalise into.
    """

    id: str
    version: str
    accepts: tuple[str, ...]
    emits: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    model_id: str = ""
    description: str = ""
    transport: str = "in_process"
    transfer: str = "paths"
    embedding_dim: int | None = None

    # The capability block, §5. Declare the minimum: a metadata-only plugin
    # never pays decode cost, which is the whole point of declaring.
    pixels: bool = False
    frames: bool = False
    audio: bool = False
    text: bool = False
    metadata_only: bool = False
    gpu: bool = False
    network: bool = False
    max_concurrency: int = 1

    namespaces: dict[str, dict[str, Any]] = field(default_factory=dict)  # type: ignore[misc]

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise PluginContractError(
                f"plugin id {self.id!r} must match [a-z0-9._-]+", plugin_id=self.id
            )
        if not self.version:
            raise PluginContractError("plugin version must be non-empty", plugin_id=self.id)
        bad = [m for m in self.accepts if m not in ("image", "video", "audio", "document", "other")]
        if bad or not self.accepts:
            raise PluginContractError(
                f"accepts must be a non-empty subset of the media types, got {self.accepts!r}",
                plugin_id=self.id,
            )
        if self.transport not in ("in_process", "subprocess", "http"):
            raise PluginContractError(
                f"unsupported plugin transport {self.transport!r}", plugin_id=self.id
            )
        if self.transfer not in ("paths", "inline", "both"):
            raise PluginContractError(
                f"unsupported transfer mode {self.transfer!r}", plugin_id=self.id
            )
        if self.embedding_dim is not None and self.embedding_dim <= 0:
            raise PluginContractError(
                "embedding_dim must be positive", plugin_id=self.id
            )


@runtime_checkable
class Analyzer(Protocol):
    """What the registry expects an in-process plugin class to provide.

    A plugin is a class exposing ``info`` and ``analyze``. Instantiated once
    per engine lifetime; ``analyze`` is called once per asset and must be
    stateless across calls, because the scheduler owes it no ordering.
    """

    info: PluginInfo

    def analyze(self, ctx: "AnalysisContext") -> Sequence[Annotation]:  # noqa: F821
        """Inspect one asset via the context; return zero or more claims.

        An empty return is a valid success — "nothing to say" — and marks the
        task done. Raise to fail the task; the error is recorded.
        """
        ...


def validate_annotations(
    plugin_id: str,
    annotations: Sequence[Annotation],
    *,
    embedding_dim: int | None = None,
    per_namespace_dim: dict[str, int] | None = None,
) -> None:
    """Contract §1 receipt validation, identical across all transports.

    :raises PluginContractError: on the first violation, naming the plugin —
        the task fails and the reason lands in the ``errors`` table.
    """
    from ..util import json_dumps

    for index, annotation in enumerate(annotations):
        where = f"{plugin_id} annotation[{index}]"
        namespace = (annotation.namespace or "").strip()
        label = (annotation.label or "").strip()
        if not namespace or not _NAMESPACE_RE.match(namespace):
            raise PluginContractError(
                f"{where}: namespace {annotation.namespace!r} must match [a-z0-9._-]{{1,64}}",
                plugin_id=plugin_id,
            )
        if not label or len(label) > _MAX_LABEL:
            raise PluginContractError(
                f"{where}: label must be non-empty and <= {_MAX_LABEL} chars",
                plugin_id=plugin_id,
            )
        if annotation.confidence is not None and not (0.0 <= annotation.confidence <= 1.0):
            # An error, not a clamp: out-of-range confidence is a model bug
            # that clamping would bury.
            raise PluginContractError(
                f"{where}: confidence {annotation.confidence} outside [0.0, 1.0]",
                plugin_id=plugin_id,
            )
        if annotation.value is not None:
            if not isinstance(annotation.value, dict):
                raise PluginContractError(
                    f"{where}: value must be a JSON object or None", plugin_id=plugin_id
                )
            if len(json_dumps(annotation.value).encode("utf-8")) > _MAX_VALUE_BYTES:
                raise PluginContractError(
                    f"{where}: value exceeds {_MAX_VALUE_BYTES} bytes serialized",
                    plugin_id=plugin_id,
                )
        region = annotation.region
        if region is not None:
            for name, coordinate in (("x", region.x), ("y", region.y)):
                if coordinate is not None and not (-0.5 <= coordinate <= 1.5):
                    raise PluginContractError(
                        f"{where}: region.{name}={coordinate} is not normalized "
                        "(coordinates are fractions, not pixels)",
                        plugin_id=plugin_id,
                    )
            for name, extent in (("w", region.w), ("h", region.h)):
                if extent is not None and extent < 0:
                    raise PluginContractError(
                        f"{where}: region.{name} must be >= 0", plugin_id=plugin_id
                    )
        if annotation.embedding is not None:
            declared = (per_namespace_dim or {}).get(namespace, embedding_dim)
            actual = len(annotation.embedding)
            if actual == 0:
                raise PluginContractError(f"{where}: embedding is empty", plugin_id=plugin_id)
            if declared is not None and actual != declared:
                # §5.1: no padding, no truncation, no silent accept. A dim
                # change without a version bump poisons similarity search.
                raise PluginContractError(
                    f"{where}: embedding dim {actual} != declared {declared}",
                    plugin_id=plugin_id,
                )
