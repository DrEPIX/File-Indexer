"""Load and validate the agent-editable TOML change sheet."""

from __future__ import annotations

import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Mapping, cast

from .errors import SheetError
from .models import FilterDefinition, OperationDefinition, SortDefinition, ValueType


class SurfaceRegistry:
    """Immutable-by-convention registry of public filters, sorts, and operations."""

    def __init__(
        self,
        *,
        filters: Iterable[FilterDefinition],
        sorts: Iterable[SortDefinition],
        operations: Iterable[OperationDefinition],
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        self.filters = self._unique(filters, "key")
        self.sorts = self._unique(sorts, "key")
        self.operations = self._unique(operations, "operation_id")
        self.settings = dict(settings or {})
        self._aliases = self._build_aliases()
        self._validate_defaults()

    @staticmethod
    def _unique(items: Iterable[Any], key_name: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in items:
            key = str(getattr(item, key_name))
            if key in result:
                raise SheetError(f"duplicate {key_name}: {key!r}")
            result[key] = item
        return result

    def _build_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for definition in self.filters.values():
            for alias in definition.aliases:
                if alias in self.filters or alias in aliases:
                    raise SheetError(f"filter alias collides: {alias!r}")
                aliases[alias] = definition.key
        return aliases

    def _validate_defaults(self) -> None:
        search = self.settings.get("search", {})
        default_sort = search.get("default_sort") if isinstance(search, Mapping) else None
        if default_sort and default_sort not in self.sorts:
            raise SheetError(f"default_sort references unknown sort {default_sort!r}")
        default_page_size = search.get("default_page_size", 100) if isinstance(search, Mapping) else 100
        max_page_size = search.get("max_page_size", 500) if isinstance(search, Mapping) else 500
        max_boolean_depth = search.get("max_boolean_depth", 12) if isinstance(search, Mapping) else 12
        try:
            default_page_size = int(default_page_size)
            max_page_size = int(max_page_size)
            max_boolean_depth = int(max_boolean_depth)
        except (TypeError, ValueError) as exc:
            raise SheetError("search size and depth settings must be integers") from exc
        if not 1 <= default_page_size <= max_page_size:
            raise SheetError("default_page_size must be between 1 and max_page_size")
        if not 0 <= max_boolean_depth <= 64:
            raise SheetError("max_boolean_depth must be between 0 and 64")

    def filter(self, key_or_alias: str) -> FilterDefinition:
        """Resolve a canonical filter key or a compatibility alias."""

        key = self._aliases.get(key_or_alias, key_or_alias)
        try:
            return cast(FilterDefinition, self.filters[key])
        except KeyError as exc:
            raise SheetError(f"unknown filter: {key_or_alias!r}") from exc

    def manifest(self) -> dict[str, Any]:
        """Return the complete UI-discovery document."""

        return {
            "version": 1,
            "settings": self.settings,
            "filters": [asdict(value) for value in self.filters.values() if value.enabled],
            "sorts": [asdict(value) for value in self.sorts.values() if value.enabled],
            "operations": [asdict(value) for value in self.operations.values() if value.enabled],
        }

    @classmethod
    def from_toml(cls, path: str | Path) -> "SurfaceRegistry":
        """Load a registry from the low-parameter-agent change sheet."""

        sheet_path = Path(path)
        with sheet_path.open("rb") as stream:
            data = tomllib.load(stream)
        filters = [cls._parse_filter(item) for item in cls._array(data, "filters")]
        sorts = [cls._parse_sort(item) for item in cls._array(data, "sorts")]
        operations = [cls._parse_operation(item) for item in cls._array(data, "operations")]
        settings = {
            key: value
            for key, value in data.items()
            if key not in {"filters", "sorts", "operations"}
        }
        return cls(filters=filters, sorts=sorts, operations=operations, settings=settings)

    @staticmethod
    def _array(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            raise SheetError(f"{key} must be an array of tables")
        return value

    @staticmethod
    def _parse_filter(data: Mapping[str, Any]) -> FilterDefinition:
        try:
            return FilterDefinition(
                key=str(data["key"]),
                label=str(data["label"]),
                field=str(data["field"]),
                value_type=ValueType(str(data["type"])),
                operators=tuple(str(value) for value in data["operators"]),
                group=str(data.get("group", "General")),
                description=str(data.get("description", "")),
                widget=str(data.get("widget", "auto")),
                facet=bool(data.get("facet", False)),
                multiple=bool(data.get("multiple", False)),
                nullable=bool(data.get("nullable", False)),
                unit=str(data["unit"]) if data.get("unit") is not None else None,
                choices=tuple(str(value) for value in data.get("choices", [])),
                aliases=tuple(str(value) for value in data.get("aliases", [])),
                capability=str(data.get("capability", "structured")),
                enabled=bool(data.get("enabled", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SheetError(f"invalid filter declaration: {data!r}: {exc}") from exc

    @staticmethod
    def _parse_sort(data: Mapping[str, Any]) -> SortDefinition:
        try:
            return SortDefinition(
                key=str(data["key"]),
                label=str(data["label"]),
                field=str(data["field"]),
                directions=tuple(str(value) for value in data.get("directions", ["asc", "desc"])),
                default_direction=str(data.get("default_direction", "desc")),
                enabled=bool(data.get("enabled", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SheetError(f"invalid sort declaration: {data!r}: {exc}") from exc

    @staticmethod
    def _parse_operation(data: Mapping[str, Any]) -> OperationDefinition:
        try:
            return OperationDefinition(
                operation_id=str(data["id"]),
                method=str(data["method"]).upper(),
                path=str(data["path"]),
                summary=str(data["summary"]),
                capability=str(data.get("capability", "core")),
                enabled=bool(data.get("enabled", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SheetError(f"invalid operation declaration: {data!r}: {exc}") from exc
