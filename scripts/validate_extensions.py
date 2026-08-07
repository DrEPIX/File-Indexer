#!/usr/bin/env python3
"""Validate MediaEngine's declarative extension and QoL surfaces.

This intentionally depends only on Python 3.11's standard library. It is an
authoring guardrail: a small agent can edit TOML values, run this file, and get
stable JSON-addressable errors before the engine ever imports the change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PLUGIN_ID = re.compile(r"[a-z0-9._-]{1,64}\Z")
MEDIA_TYPES = {"image", "video", "audio", "document", "other"}
TRANSPORTS = {"in_process", "subprocess", "http"}
TRANSFERS = {"paths", "inline", "both"}
NAMESPACE_TYPES = {"categorical", "numeric", "text", "geo", "boolean"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "WS"}


@dataclass(frozen=True, slots=True)
class Issue:
    """One deterministic validation failure suitable for humans or tooling."""

    file: str
    path: str
    message: str


class Validator:
    """Collect failures instead of stopping at the first bad variable."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.issues: list[Issue] = []

    def fail(self, path: str, message: str) -> None:
        self.issues.append(Issue(str(self.source), path, message))

    def table(self, value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.fail(path, "must be a TOML table")
            return {}
        return value

    def strings(self, value: Any, path: str, *, nonempty: bool = True) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            self.fail(path, "must be an array of strings")
            return []
        if nonempty and not value:
            self.fail(path, "must not be empty")
        return value

    def positive_int(self, value: Any, path: str) -> int | None:
        # bool is an int subclass, but never a meaningful dimension/concurrency.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            self.fail(path, "must be a positive integer")
            return None
        return value


def load_toml(path: Path, validator: Validator) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            decoded = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        validator.fail("$", f"cannot read TOML: {exc}")
        return {}
    return validator.table(decoded, "$")


def validate_plugin(path: Path) -> list[Issue]:
    """Check the frozen ``mediaengine.analyzer/1`` manifest contract."""

    check = Validator(path)
    root = load_toml(path, check)
    plugin = check.table(root.get("plugin"), "plugin")

    plugin_id = plugin.get("id")
    if not isinstance(plugin_id, str) or PLUGIN_ID.fullmatch(plugin_id) is None:
        check.fail("plugin.id", "must match [a-z0-9._-]+ and be at most 64 characters")
    version = plugin.get("version")
    if not isinstance(version, str) or not version.strip():
        check.fail("plugin.version", "must be a non-empty string")
    transport = plugin.get("transport")
    if transport not in TRANSPORTS:
        check.fail("plugin.transport", f"must be one of {sorted(TRANSPORTS)}")

    accepts = check.strings(plugin.get("accepts"), "plugin.accepts")
    unknown_media = sorted(set(accepts) - MEDIA_TYPES)
    if unknown_media:
        check.fail("plugin.accepts", f"contains unsupported media types: {unknown_media}")
    emits = check.strings(plugin.get("emits"), "plugin.emits")
    check.strings(plugin.get("depends_on"), "plugin.depends_on", nonempty=False)
    if plugin.get("transfer", "paths") not in TRANSFERS:
        check.fail("plugin.transfer", f"must be one of {sorted(TRANSFERS)}")
    if "embedding_dim" in plugin:
        check.positive_int(plugin["embedding_dim"], "plugin.embedding_dim")

    requires = check.table(plugin.get("requires"), "plugin.requires")
    flags = ("pixels", "frames", "audio", "text", "metadata_only", "gpu", "network")
    for flag in flags:
        if not isinstance(requires.get(flag), bool):
            check.fail(f"plugin.requires.{flag}", "must be a boolean")
    check.positive_int(requires.get("max_concurrency"), "plugin.requires.max_concurrency")
    if requires.get("metadata_only") is True and any(requires.get(flag) is True for flag in ("pixels", "frames", "audio", "text")):
        check.fail("plugin.requires.metadata_only", "cannot be true when derivative inputs are requested")

    namespaces = check.table(plugin.get("namespaces"), "plugin.namespaces")
    for emitted_namespace in emits:
        if emitted_namespace not in namespaces:
            check.fail("plugin.emits", f"{emitted_namespace!r} has no matching plugin.namespaces table")
    for name, raw_namespace in namespaces.items():
        namespace_table = check.table(raw_namespace, f"plugin.namespaces.{name}")
        if name not in emits:
            check.fail(f"plugin.namespaces.{name}", "is declared but absent from plugin.emits")
        if namespace_table.get("value_type") not in NAMESPACE_TYPES:
            check.fail(f"plugin.namespaces.{name}.value_type", f"must be one of {sorted(NAMESPACE_TYPES)}")
        if not isinstance(namespace_table.get("facetable"), bool):
            check.fail(f"plugin.namespaces.{name}.facetable", "must be a boolean")
        if "embedding_dim" in namespace_table:
            check.positive_int(namespace_table["embedding_dim"], f"plugin.namespaces.{name}.embedding_dim")

    if transport == "subprocess":
        subprocess_table = check.table(plugin.get("subprocess"), "plugin.subprocess")
        check.strings(subprocess_table.get("command"), "plugin.subprocess.command")
    elif transport == "http":
        http = check.table(plugin.get("http"), "plugin.http")
        base_url = http.get("base_url")
        parsed = urlparse(base_url) if isinstance(base_url, str) else None
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            check.fail("plugin.http.base_url", "must be an absolute HTTP(S) URL")
        for key in ("health_path", "manifest_path", "analyze_path"):
            value = http.get(key)
            if not isinstance(value, str) or not value.startswith("/"):
                check.fail(f"plugin.http.{key}", "must be an absolute URL path")
        timeout = http.get("timeout_s")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            check.fail("plugin.http.timeout_s", "must be a positive number")

    return check.issues


def validate_change_sheet(path: Path) -> list[Issue]:
    """Check uniqueness and cross-references in the low-context change sheet."""

    check = Validator(path)
    root = load_toml(path, check)
    search = check.table(root.get("search"), "search")
    filters = root.get("filters")
    sorts = root.get("sorts")
    operations = root.get("operations")
    if not isinstance(filters, list):
        check.fail("filters", "must contain [[filters]] entries")
        filters = []
    if not isinstance(sorts, list):
        check.fail("sorts", "must contain [[sorts]] entries")
        sorts = []
    if not isinstance(operations, list):
        check.fail("operations", "must contain [[operations]] entries")
        operations = []

    used_filter_names: dict[str, str] = {}
    for index, raw in enumerate(filters):
        item = check.table(raw, f"filters[{index}]")
        key = item.get("key")
        if not isinstance(key, str) or not key.strip():
            check.fail(f"filters[{index}].key", "must be a non-empty string")
            continue
        aliases = check.strings(item.get("aliases", []), f"filters[{index}].aliases", nonempty=False)
        for name in (key, *aliases):
            previous = used_filter_names.get(name)
            if previous is not None:
                check.fail(f"filters[{index}].key", f"name {name!r} already belongs to {previous!r}")
            else:
                used_filter_names[name] = key
        for field in ("label", "field", "type", "widget"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                check.fail(f"filters[{index}].{field}", "must be a non-empty string")
        check.strings(item.get("operators"), f"filters[{index}].operators")
        if item.get("type") == "enum":
            check.strings(item.get("choices"), f"filters[{index}].choices")

    sort_keys: set[str] = set()
    for index, raw in enumerate(sorts):
        item = check.table(raw, f"sorts[{index}]")
        key = item.get("key")
        if not isinstance(key, str) or not key:
            check.fail(f"sorts[{index}].key", "must be a non-empty string")
        elif key in sort_keys:
            check.fail(f"sorts[{index}].key", f"duplicate sort key {key!r}")
        else:
            sort_keys.add(key)
        if item.get("default_direction") not in {"asc", "desc"}:
            check.fail(f"sorts[{index}].default_direction", "must be 'asc' or 'desc'")
    if search.get("default_sort") not in sort_keys:
        check.fail("search.default_sort", "must reference a declared sort key")
    default_page = search.get("default_page_size")
    max_page = search.get("max_page_size")
    if not isinstance(default_page, int) or not isinstance(max_page, int) or not 0 < default_page <= max_page:
        check.fail("search.default_page_size", "must be positive and no greater than max_page_size")

    operation_ids: set[str] = set()
    for index, raw in enumerate(operations):
        item = check.table(raw, f"operations[{index}]")
        operation_id = item.get("id")
        if not isinstance(operation_id, str) or not operation_id:
            check.fail(f"operations[{index}].id", "must be a non-empty string")
        elif operation_id in operation_ids:
            check.fail(f"operations[{index}].id", f"duplicate operation id {operation_id!r}")
        else:
            operation_ids.add(operation_id)
        if item.get("method") not in METHODS:
            check.fail(f"operations[{index}].method", f"must be one of {sorted(METHODS)}")
        route = item.get("path")
        if not isinstance(route, str) or not route.startswith("/api/"):
            check.fail(f"operations[{index}].path", "must start with /api/")

    return check.issues


def discover(root: Path) -> tuple[list[Path], Path]:
    manifests = sorted((root / "plugins-available").glob("*/plugin.toml"))
    manifests.extend(sorted((root / "examples").glob("*/plugin.toml")))
    return manifests, root / "qol_contract" / "change_sheet.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="emit a stable JSON result for automation")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    manifests, sheet = discover(root)
    issues = [issue for manifest in manifests for issue in validate_plugin(manifest)]
    issues.extend(validate_change_sheet(sheet))
    result = {"ok": not issues, "plugin_manifests": len(manifests), "issues": [asdict(issue) for issue in issues]}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif issues:
        for issue in issues:
            print(f"{issue.file}:{issue.path}: {issue.message}", file=sys.stderr)
    else:
        print(f"extension contracts: PASS ({len(manifests)} plugin manifests + QoL sheet)")
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
