"""Validate public requests and compile them to backend-neutral plans."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .errors import QueryError, SheetError
from .models import (
    FilterClause,
    FilterDefinition,
    PlannedClause,
    PlannedGroup,
    QueryGroup,
    SearchPlan,
    SearchRequest,
    ValueType,
)
from .registry import SurfaceRegistry


UNARY_OPERATORS = frozenset({"exists", "missing", "is_true", "is_false"})
LIST_OPERATORS = frozenset({"in", "not_in", "between", "overlaps"})


class QueryPlanner:
    """The single validation boundary between untrusted clients and a backend."""

    def __init__(self, registry: SurfaceRegistry) -> None:
        self.registry = registry

    def plan(self, request: SearchRequest) -> SearchPlan:
        """Normalize and validate a request without producing SQL."""

        search_settings = self.registry.settings.get("search", {})
        if not isinstance(search_settings, Mapping):
            search_settings = {}
        max_page_size = int(search_settings.get("max_page_size", 500))
        page_size = request.page_size
        if page_size is None:
            page_size = int(search_settings.get("default_page_size", 100))
        if not 1 <= page_size <= max_page_size:
            raise QueryError(f"page_size must be between 1 and {max_page_size}")
        sort_key = request.sort or str(search_settings.get("default_sort", "captured"))
        try:
            sort = self.registry.sorts[sort_key]
        except KeyError as exc:
            raise QueryError(f"unknown sort: {sort_key!r}") from exc
        if not sort.enabled:
            raise QueryError(f"sort is disabled: {request.sort!r}")
        direction = request.direction or sort.default_direction
        if direction not in sort.directions:
            raise QueryError(f"sort {sort.key!r} does not allow direction {direction!r}")
        capabilities: set[str] = set()
        max_depth = int(search_settings.get("max_boolean_depth", 12))
        where = self._plan_group(
            request.where, capabilities, depth=0, max_depth=max_depth
        )
        if request.text:
            capabilities.add("fts")
        if request.include_facets:
            capabilities.add("facets")
        return SearchPlan(
            text=request.text.strip() if request.text else None,
            where=where,
            sort_field=sort.field,
            direction=direction,
            page_size=page_size,
            cursor=request.cursor,
            include_facets=request.include_facets,
            facet_namespaces=request.facet_namespaces,
            required_capabilities=frozenset(capabilities),
        )

    def _plan_group(
        self,
        group: QueryGroup,
        capabilities: set[str],
        depth: int,
        max_depth: int,
    ) -> PlannedGroup:
        if depth > max_depth:
            raise QueryError(f"query nesting exceeds {max_depth} levels")
        clauses = tuple(self._plan_clause(clause, capabilities) for clause in group.clauses)
        groups = tuple(
            self._plan_group(child, capabilities, depth + 1, max_depth)
            for child in group.groups
        )
        return PlannedGroup(operator=group.operator, clauses=clauses, groups=groups)

    def _plan_clause(self, clause: FilterClause, capabilities: set[str]) -> PlannedClause:
        try:
            definition = self.registry.filter(clause.key)
        except SheetError as exc:
            raise QueryError(str(exc)) from exc
        if not definition.enabled:
            raise QueryError(f"filter is disabled: {definition.key!r}")
        if clause.operator not in definition.operators:
            raise QueryError(
                f"filter {definition.key!r} does not allow operator {clause.operator!r}; "
                f"allowed: {', '.join(definition.operators)}"
            )
        value = self._coerce_value(definition, clause.operator, clause.value)
        capabilities.add(definition.capability)
        return PlannedClause(
            public_key=definition.key,
            backend_field=definition.field,
            operator=clause.operator,
            value=value,
            capability=definition.capability,
        )

    def _coerce_value(self, definition: FilterDefinition, operator: str, value: Any) -> Any:
        if operator in UNARY_OPERATORS:
            if value not in (None, True, False):
                raise QueryError(f"operator {operator!r} does not take a value")
            return None
        if value is None and not definition.nullable:
            raise QueryError(f"filter {definition.key!r} requires a value")
        if operator in LIST_OPERATORS:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise QueryError(f"operator {operator!r} requires an array value")
            if operator == "between" and len(value) != 2:
                raise QueryError("between requires exactly two values")
            return [self._coerce_scalar(definition, item) for item in value]
        return self._coerce_scalar(definition, value)

    @staticmethod
    def _coerce_scalar(definition: FilterDefinition, value: Any) -> Any:
        try:
            if value is None:
                return None
            if definition.value_type is ValueType.BOOLEAN:
                if isinstance(value, bool):
                    return value
                if isinstance(value, str) and value.lower() in {"true", "false"}:
                    return value.lower() == "true"
                raise ValueError("expected boolean")
            if definition.value_type is ValueType.INTEGER:
                if isinstance(value, bool):
                    raise ValueError("expected integer")
                return int(value)
            if definition.value_type in {ValueType.NUMBER, ValueType.DURATION, ValueType.BYTES}:
                if isinstance(value, bool):
                    raise ValueError("expected number")
                return float(value)
            if definition.value_type is ValueType.DATETIME:
                text = str(value)
                datetime.fromisoformat(text.replace("Z", "+00:00"))
                return text
            if definition.value_type is ValueType.ENUM:
                text = str(value)
                if text not in definition.choices:
                    raise ValueError(f"expected one of: {', '.join(definition.choices)}")
                return text
            if definition.value_type is ValueType.GEO:
                if not isinstance(value, Mapping):
                    raise ValueError("expected geo object")
                return dict(value)
            return str(value)
        except (TypeError, ValueError) as exc:
            raise QueryError(f"invalid value for {definition.key!r}: {value!r} ({exc})") from exc
