"""Validation and export utilities for the declarative contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .registry import SurfaceRegistry
from .schema import search_request_schema


def build_parser() -> argparse.ArgumentParser:
    """Build the small maintenance CLI."""

    parser = argparse.ArgumentParser(prog="mediaengine-qol")
    parser.add_argument("--sheet", type=Path, required=True, help="Path to change_sheet.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the sheet")
    export = subparsers.add_parser("export", help="Export discovery manifest and request schema")
    export.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run validation or deterministic schema export."""

    args = build_parser().parse_args(argv)
    registry = SurfaceRegistry.from_toml(args.sheet)
    if args.command == "validate":
        print(
            f"valid: {len(registry.filters)} filters, "
            f"{len(registry.sorts)} sorts, {len(registry.operations)} operations"
        )
        return 0
    payload = {
        "surface": registry.manifest(),
        "search_request_schema": search_request_schema(registry),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

