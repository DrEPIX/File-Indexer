"""Generate machine-readable schemas for small models and UI builders."""

from __future__ import annotations

from typing import Any

from .registry import SurfaceRegistry


def search_request_schema(registry: SurfaceRegistry) -> dict[str, Any]:
    """Return a JSON Schema that constrains public filter and sort names."""

    enabled_filters = [key for key, value in registry.filters.items() if value.enabled]
    enabled_sorts = [key for key, value in registry.sorts.items() if value.enabled]
    search = registry.settings.get("search", {})
    if not isinstance(search, dict):
        search = {}
    default_sort = str(search.get("default_sort", "captured"))
    default_page_size = int(search.get("default_page_size", 100))
    max_page_size = int(search.get("max_page_size", 500))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "mediaengine://schemas/search-request-v1.json",
        "title": "MediaEngine Search Request",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": ["string", "null"]},
            "where": {"$ref": "#/$defs/group"},
            "sort": {"enum": enabled_sorts, "default": default_sort},
            "direction": {"enum": ["asc", "desc", None]},
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": max_page_size,
                "default": default_page_size,
            },
            "cursor": {"type": ["string", "null"]},
            "include_facets": {"type": "boolean"},
            "facet_namespaces": {"type": "array", "items": {"type": "string"}},
        },
        "$defs": {
            "clause": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "operator"],
                "properties": {
                    "key": {"enum": enabled_filters},
                    "operator": {"type": "string"},
                    "value": {},
                },
            },
            "group": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "operator": {"enum": ["all", "any", "none"]},
                    "clauses": {"type": "array", "items": {"$ref": "#/$defs/clause"}},
                    "groups": {"type": "array", "items": {"$ref": "#/$defs/group"}},
                },
            },
        },
    }
