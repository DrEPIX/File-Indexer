"""Small immutable models shared by GUI, HTTP, and backend adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .errors import QueryError, SheetError


JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class ValueType(StrEnum):
    """Portable value types understood by every client."""

    TEXT = "text"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    ENUM = "enum"
    PATH = "path"
    GEO = "geo"
    DURATION = "duration"
    BYTES = "bytes"


class GroupOperator(StrEnum):
    """Boolean operation for a query group."""

    ALL = "all"
    ANY = "any"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class FilterDefinition:
    """One declaratively exposed search filter."""

    key: str
    label: str
    field: str
    value_type: ValueType
    operators: tuple[str, ...]
    group: str = "General"
    description: str = ""
    widget: str = "auto"
    facet: bool = False
    multiple: bool = False
    nullable: bool = False
    unit: str | None = None
    choices: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    capability: str = "structured"
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.key or any(char.isspace() for char in self.key):
            raise SheetError(f"invalid filter key: {self.key!r}")
        if not self.label.strip():
            raise SheetError(f"filter {self.key!r} has no label")
        if not self.field:
            raise SheetError(f"filter {self.key!r} has no backend field")
        if not self.operators:
            raise SheetError(f"filter {self.key!r} has no operators")
        if any(not value.strip() for value in self.operators):
            raise SheetError(f"filter {self.key!r} has an empty operator")
        if len(set(self.operators)) != len(self.operators):
            raise SheetError(f"filter {self.key!r} has duplicate operators")
        if self.value_type is ValueType.ENUM and not self.choices:
            raise SheetError(f"enum filter {self.key!r} must declare choices")
        if len(set(self.choices)) != len(self.choices):
            raise SheetError(f"filter {self.key!r} has duplicate choices")
        if any(not alias or any(char.isspace() for char in alias) for alias in self.aliases):
            raise SheetError(f"filter {self.key!r} has an invalid alias")
        if len(set(self.aliases)) != len(self.aliases):
            raise SheetError(f"filter {self.key!r} has duplicate aliases")
        if not self.capability.strip():
            raise SheetError(f"filter {self.key!r} has no capability")


@dataclass(frozen=True, slots=True)
class SortDefinition:
    """A stable public sort name mapped to a backend field."""

    key: str
    label: str
    field: str
    directions: tuple[str, ...] = ("asc", "desc")
    default_direction: str = "desc"
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.key or any(char.isspace() for char in self.key):
            raise SheetError(f"invalid sort key: {self.key!r}")
        if not self.label.strip() or not self.field.strip():
            raise SheetError(f"sort {self.key!r} requires a label and backend field")
        if not self.directions or any(value not in {"asc", "desc"} for value in self.directions):
            raise SheetError(f"sort {self.key!r} has invalid directions")
        if len(set(self.directions)) != len(self.directions):
            raise SheetError(f"sort {self.key!r} has duplicate directions")
        if self.default_direction not in self.directions:
            raise SheetError(f"sort {self.key!r} default direction is not allowed")


@dataclass(frozen=True, slots=True)
class OperationDefinition:
    """One public operation shared by the Python and HTTP surfaces."""

    operation_id: str
    method: str
    path: str
    summary: str
    capability: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.operation_id or any(char.isspace() for char in self.operation_id):
            raise SheetError(f"invalid operation id: {self.operation_id!r}")
        if self.method not in {"DELETE", "GET", "PATCH", "POST", "PUT", "WS"}:
            raise SheetError(f"operation {self.operation_id!r} has an invalid transport method")
        if not self.path.startswith("/") or any(char.isspace() for char in self.path):
            raise SheetError(f"operation {self.operation_id!r} has an invalid path")
        if not self.summary.strip() or not self.capability.strip():
            raise SheetError(f"operation {self.operation_id!r} requires summary and capability")


@dataclass(frozen=True, slots=True)
class FilterClause:
    """One leaf predicate in a search expression."""

    key: str
    operator: str
    value: JSONValue = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "FilterClause":
        """Parse a client mapping without silently accepting missing keys."""

        if not isinstance(data, Mapping):
            raise QueryError("filter clause must be an object")
        unexpected = set(data) - {"key", "operator", "value"}
        if unexpected:
            raise QueryError(
                "filter clause contains unknown fields: "
                + ", ".join(sorted(str(key) for key in unexpected))
            )
        try:
            key = data["key"]
            operator = data["operator"]
            if not isinstance(key, str) or not key.strip():
                raise QueryError("filter clause key must be a non-empty string")
            if not isinstance(operator, str) or not operator.strip():
                raise QueryError("filter clause operator must be a non-empty string")
            return cls(
                key=key,
                operator=operator,
                value=data.get("value"),
            )
        except KeyError as exc:
            raise QueryError(f"filter clause is missing {exc.args[0]!r}") from exc


@dataclass(frozen=True, slots=True)
class QueryGroup:
    """Recursive AND/OR/NOT-like composition over filter clauses."""

    operator: GroupOperator = GroupOperator.ALL
    clauses: tuple[FilterClause, ...] = ()
    groups: tuple["QueryGroup", ...] = ()

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        _depth: int = 0,
    ) -> "QueryGroup":
        """Parse a recursive mapping supplied by HTTP or a GUI."""

        if not isinstance(data, Mapping):
            raise QueryError("query group must be an object")
        unexpected = set(data) - {"operator", "clauses", "groups"}
        if unexpected:
            raise QueryError(
                "query group contains unknown fields: "
                + ", ".join(sorted(str(key) for key in unexpected))
            )
        # Enforce an absolute parser safety ceiling before recursion. The
        # registry's usually-smaller max_boolean_depth is enforced by the
        # planner after parsing.
        if _depth > 64:
            raise QueryError("query nesting exceeds the parser safety limit of 64 levels")
        try:
            operator = GroupOperator(str(data.get("operator", "all")))
        except ValueError as exc:
            raise QueryError(f"unknown group operator: {data.get('operator')!r}") from exc
        clauses_raw = data.get("clauses", [])
        groups_raw = data.get("groups", [])
        if not isinstance(clauses_raw, Sequence) or isinstance(clauses_raw, (str, bytes)):
            raise QueryError("clauses must be an array")
        if not isinstance(groups_raw, Sequence) or isinstance(groups_raw, (str, bytes)):
            raise QueryError("groups must be an array")
        clauses: list[FilterClause] = []
        for index, item in enumerate(clauses_raw):
            if not isinstance(item, Mapping):
                raise QueryError(f"clauses[{index}] must be an object")
            clauses.append(FilterClause.from_mapping(item))
        groups: list[QueryGroup] = []
        for index, item in enumerate(groups_raw):
            if not isinstance(item, Mapping):
                raise QueryError(f"groups[{index}] must be an object")
            try:
                groups.append(cls.from_mapping(item, _depth=_depth + 1))
            except QueryError as exc:
                raise QueryError(f"groups[{index}]: {exc}") from exc
        return cls(
            operator=operator,
            clauses=tuple(clauses),
            groups=tuple(groups),
        )


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """Frontend-safe search request independent of SQL or FastAPI."""

    text: str | None = None
    where: QueryGroup = field(default_factory=QueryGroup)
    sort: str | None = None
    direction: str | None = None
    page_size: int | None = None
    cursor: str | None = None
    include_facets: bool = True
    facet_namespaces: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SearchRequest":
        """Parse a stable JSON-shaped request."""

        if not isinstance(data, Mapping):
            raise QueryError("search request must be an object")
        unexpected = set(data) - {
            "text",
            "where",
            "sort",
            "direction",
            "page_size",
            "cursor",
            "include_facets",
            "facet_namespaces",
        }
        if unexpected:
            raise QueryError(
                "search request contains unknown fields: "
                + ", ".join(sorted(str(key) for key in unexpected))
            )
        where_raw = data.get("where", {})
        if not isinstance(where_raw, Mapping):
            raise QueryError("where must be an object")
        namespaces = data.get("facet_namespaces", [])
        if not isinstance(namespaces, Sequence) or isinstance(namespaces, (str, bytes)):
            raise QueryError("facet_namespaces must be an array")
        if any(not isinstance(value, str) or not value.strip() for value in namespaces):
            raise QueryError("facet_namespaces must contain non-empty strings")
        if len(namespaces) > 100:
            raise QueryError("facet_namespaces cannot contain more than 100 values")

        text = data.get("text")
        sort = data.get("sort")
        direction = data.get("direction")
        cursor = data.get("cursor")
        for field_name, value in (
            ("text", text),
            ("sort", sort),
            ("direction", direction),
            ("cursor", cursor),
        ):
            if value is not None and not isinstance(value, str):
                raise QueryError(f"{field_name} must be a string or null")

        raw_page_size = data.get("page_size")
        if raw_page_size is not None and (
            isinstance(raw_page_size, bool) or not isinstance(raw_page_size, int)
        ):
            raise QueryError("page_size must be an integer or null")
        include_facets = data.get("include_facets", True)
        if not isinstance(include_facets, bool):
            raise QueryError("include_facets must be a boolean")
        return cls(
            text=text,
            where=QueryGroup.from_mapping(where_raw),
            sort=sort,
            direction=direction,
            page_size=raw_page_size,
            cursor=cursor,
            include_facets=include_facets,
            facet_namespaces=tuple(dict.fromkeys(value.strip() for value in namespaces)),
        )


@dataclass(frozen=True, slots=True)
class PlannedClause:
    """Validated backend-facing predicate."""

    public_key: str
    backend_field: str
    operator: str
    value: JSONValue
    capability: str


@dataclass(frozen=True, slots=True)
class PlannedGroup:
    """Validated recursive query group."""

    operator: GroupOperator
    clauses: tuple[PlannedClause, ...]
    groups: tuple["PlannedGroup", ...]


@dataclass(frozen=True, slots=True)
class SearchPlan:
    """Normalized request handed to a concrete MediaEngine adapter."""

    text: str | None
    where: PlannedGroup
    sort_field: str
    direction: str
    page_size: int
    cursor: str | None
    include_facets: bool
    facet_namespaces: tuple[str, ...]
    required_capabilities: frozenset[str]
