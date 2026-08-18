"""One façade for both an in-process GUI and a thin HTTP controller."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from .models import SearchPlan, SearchRequest
from .planner import QueryPlanner
from .registry import SurfaceRegistry


@runtime_checkable
class BackendPort(Protocol):
    """Minimal adapter contract the eventual MediaEngine core must implement."""

    def execute_search(self, plan: SearchPlan) -> Mapping[str, Any]:
        """Execute one validated search plan."""

    def get_asset(self, asset_id: int) -> Mapping[str, Any] | None:
        """Return the complete public asset detail, or None."""

    def list_facets(self, namespace: str | None = None) -> Mapping[str, Any]:
        """Return dynamic annotation facets and counts."""

    def capabilities(self) -> frozenset[str]:
        """Return implemented planner capabilities such as structured, fts, or spatial."""


class QoLService:
    """Stable public surface that keeps GUI code away from backend internals."""

    def __init__(self, registry: SurfaceRegistry, backend: BackendPort) -> None:
        self.registry = registry
        self.backend = backend
        self.planner = QueryPlanner(registry)

    def describe(self) -> Mapping[str, Any]:
        """Return the self-describing UI/API manifest."""

        manifest = self.registry.manifest()
        manifest["backend_capabilities"] = sorted(self.backend.capabilities())
        return manifest

    def search(self, request: SearchRequest | Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate, capability-check, and execute a search."""

        parsed = request if isinstance(request, SearchRequest) else SearchRequest.from_mapping(request)
        plan = self.planner.plan(parsed)
        missing = plan.required_capabilities - self.backend.capabilities()
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise NotImplementedError(f"backend does not implement required capabilities: {missing_list}")
        return self.backend.execute_search(plan)

    def asset(self, asset_id: int) -> Mapping[str, Any] | None:
        """Fetch an asset without exposing repository objects."""

        if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id < 1:
            raise ValueError("asset_id must be a positive integer")
        return self.backend.get_asset(asset_id)

    def facets(self, namespace: str | None = None) -> Mapping[str, Any]:
        """Fetch all facets or one dynamic annotation namespace."""

        if namespace is not None:
            if not isinstance(namespace, str) or not namespace.strip():
                raise ValueError("namespace must be a non-empty string or null")
            namespace = namespace.strip()
        return self.backend.list_facets(namespace)
