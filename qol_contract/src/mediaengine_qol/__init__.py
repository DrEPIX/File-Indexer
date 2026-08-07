"""Stable, framework-neutral QoL contract for MediaEngine integrations."""

from .facade import BackendPort, QoLService
from .models import FilterClause, QueryGroup, SearchRequest
from .registry import SurfaceRegistry

__all__ = [
    "BackendPort",
    "FilterClause",
    "QoLService",
    "QueryGroup",
    "SearchRequest",
    "SurfaceRegistry",
]

