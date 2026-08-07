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
        if not self.field:
            raise SheetError(f"filter {self.key!r} has no backend field")
        if not self.operators:
            raise SheetError(f"filter {self.key!r} has no operators")
        if self.value_type is ValueType.ENUM and not self.choices:
            raise SheetError(f"enum filter {self.key!r} must declare choices")


@dataclass(frozen=True, slots=True)
class SortDefinition:
    """A stable public sort name mapped to a backend field."""

    key: str
    label: str
    field: str
    directions: tuple[str, ...] = ("asc", "desc")
    default_direction: str = "desc"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class OperationDefinition:
    """One public operation shared by the Python and HTTP surfaces."""

    operation_id: str
    method: str
    path: str
    summary: str
    capability: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class FilterClause:
    """One leaf predicate in a search expression."""

    key: str
    operator: str
    value: JSONValue = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "FilterClause":
        """Parse a client mapping without silently accepting missing keys."""

        try:
            return cls(
                key=str(data["key"]),
                operator=str(data["operator"]),
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
    def from_mapping(cls, data: Mapping[str, Any]) -> "QueryGroup":
        """Parse a recursive mapping supplied by HTTP or a GUI."""

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
        return cls(
            operator=operator,
            clauses=tuple(FilterClause.from_mapping(item) for item in clauses_raw),
            groups=tuple(cls.from_mapping(item) for item in groups_raw),
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

        where_raw = data.get("where", {})
        if not isinstance(where_raw, Mapping):
            raise QueryError("where must be an object")
        namespaces = data.get("facet_namespaces", [])
        if not isinstance(namespaces, Sequence) or isinstance(namespaces, (str, bytes)):
            raise QueryError("facet_namespaces must be an array")
        return cls(
            text=str(data["text"]) if data.get("text") is not None else None,
            where=QueryGroup.from_mapping(where_raw),
            sort=str(data["sort"]) if data.get("sort") is not None else None,
            direction=str(data["direction"]) if data.get("direction") is not None else None,
            page_size=int(data["page_size"]) if data.get("page_size") is not None else None,
            cursor=str(data["cursor"]) if data.get("cursor") is not None else None,
            include_facets=bool(data.get("include_facets", True)),
            facet_namespaces=tuple(str(value) for value in namespaces),
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
