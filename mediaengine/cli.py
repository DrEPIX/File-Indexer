"""Command-line interface.

``mediaengine [--config PATH] <command> [options]``

Every command is a small function taking parsed arguments and an engine; the
parser is built declaratively below. Output defaults to human-readable text
with ``--json`` available on every command that returns data, because this CLI
is used both by a person at a terminal and by the container entrypoint.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .config import Config, load_config
from .core.control import ProgressEvent
from .errors import ConfigError, MediaEngineError, NotFoundError
from .util import human_bytes, setup_logging

if TYPE_CHECKING:  # pragma: no cover - importing the engine here would defeat
    from .engine import MediaEngine  # the lazy import in main()

__all__ = ["main", "build_parser"]

Handler = Callable[[argparse.Namespace, "MediaEngine"], int]


def _print(payload: Any, *, as_json: bool) -> None:
    """Emit a result as JSON or as readable text."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    if isinstance(payload, str):
        print(payload)
    elif isinstance(payload, dict):
        _print_mapping(payload)
    elif isinstance(payload, list):
        for item in payload:
            _print(item, as_json=False)
            print()
    else:
        print(payload)


def _print_mapping(mapping: dict[str, Any], indent: int = 0) -> None:
    pad = "  " * indent
    width = max((len(str(k)) for k in mapping), default=0)
    for key, value in mapping.items():
        if isinstance(value, dict):
            print(f"{pad}{key}:")
            _print_mapping(value, indent + 1)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"{pad}{key}:")
            for item in value:
                _print_mapping(item, indent + 1)
                print()
        else:
            print(f"{pad}{str(key).ljust(width)}  {value}")


class _ProgressPrinter:
    """A one-line, in-place progress display for a terminal.

    Silent when stderr is not a TTY, so piping the CLI into a log file does not
    produce ten thousand lines of carriage returns.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self._started = time.monotonic()
        self._totals: dict[str, int] = {}

    def __call__(self, event: ProgressEvent) -> None:
        self._totals[event.stage] = event.done
        if not self.enabled:
            return
        elapsed = max(0.001, time.monotonic() - self._started)
        done = sum(self._totals.values())
        summary = "  ".join(f"{stage}={count}" for stage, count in sorted(self._totals.items()))
        sys.stderr.write(f"\r{summary}  ({done / elapsed:,.0f}/s)".ljust(100))
        sys.stderr.flush()

    def finish(self) -> None:
        if self.enabled:
            sys.stderr.write("\r".ljust(100) + "\r")
            sys.stderr.flush()


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_migrate(args: argparse.Namespace, engine: "MediaEngine") -> int:
    version = engine.migrate()
    _print({"schema_version": version, "database": str(engine.config.storage.db_path)},
           as_json=args.json)
    return 0


def cmd_scan(args: argparse.Namespace, engine: "MediaEngine") -> int:
    roots: list[Path] = [Path(p) for p in (args.paths or [])]
    if not roots and not engine.config.library.roots:
        print(
            "no paths given and library.roots is empty in the config.\n"
            "Either pass a directory:   mediaengine scan /photos\n"
            "or set library.roots in your config.yaml.",
            file=sys.stderr,
        )
        return 2

    printer = _ProgressPrinter(enabled=not args.quiet)
    try:
        results = engine.scan(
            roots or None,
            resume=not args.no_resume,
            rehash=args.rehash,
            generate_derivatives=not args.no_derivatives,
            progress=printer,
        )
    finally:
        printer.finish()

    if args.json:
        _print([r.as_dict() for r in results], as_json=True)
        return 0

    for result in results:
        print(f"\n{result.root}")
        print(f"  seen        {result.files_seen:,}")
        print(f"  new         {result.files_new:,}")
        print(f"  updated     {result.files_updated:,}")
        print(f"  unchanged   {result.files_skipped:,}")
        print(f"  failed      {result.files_failed:,}")
        print(f"  assets new  {result.assets_created:,}")
        print(f"  hashed      {human_bytes(result.bytes_hashed)}")
        print(f"  derivatives {result.derivatives_written:,} ({human_bytes(result.derivative_bytes)})")
        if result.missing_marked:
            print(f"  now missing {result.missing_marked:,}")
        links = result.links
        if any(links.get(k) for k in ("raw_pairs", "motion_pairs", "sidecars", "embedded_motion")):
            print(
                f"  companions  raw={links.get('raw_pairs', 0)} "
                f"motion={links.get('motion_pairs', 0)} "
                f"sidecar={links.get('sidecars', 0)} "
                f"embedded={links.get('embedded_motion', 0)}"
            )
        if result.walk.get("errors"):
            print(f"  walk errors {result.walk['errors']:,}")
        print(f"  took        {result.duration_s:.1f}s")
        if result.cancelled:
            print("  CANCELLED — rerun to resume from the last completed directory")
        for warning in result.warnings[:5]:
            print(f"  ! {warning}")
    return 1 if any(r.cancelled for r in results) else 0


def cmd_stat(args: argparse.Namespace, engine: "MediaEngine") -> int:
    stats = engine.stats()
    if args.json:
        _print(stats, as_json=True)
        return 0

    database = stats.get("database", {})
    by_type = stats.get("assets_by_type", {})
    print("Library")
    print(f"  assets        {database.get('assets', 0):,}")
    for media_type, count in sorted(by_type.items()):
        print(f"    {media_type:<10} {count:,}")
    print(f"  files         {database.get('files', 0):,}")
    print(f"  annotations   {stats.get('annotations', {}).get('annotations_live', 0):,} live")
    print(f"  namespaces    {stats.get('annotations', {}).get('namespaces', 0):,}")
    print(f"  located       {stats.get('located_assets', 0):,}")
    print(f"  indexed docs  {stats.get('indexed_documents', 0):,}")
    derivatives = stats.get("derivatives", {})
    print(
        f"  derivatives   {sum(derivatives.get('count_by_kind', {}).values()):,} "
        f"({human_bytes(derivatives.get('total_bytes', 0))})"
    )
    print(f"  database      {human_bytes(database.get('db_bytes', 0))} "
          f"(schema v{stats.get('schema_version', 0)})")

    tasks = stats.get("tasks", {})
    if tasks:
        print("Tasks")
        for state, count in sorted(tasks.items()):
            print(f"  {state:<12} {count:,}")

    print("Capabilities")
    for name, available in sorted(stats.get("capabilities", {}).items()):
        print(f"  {name:<12} {'yes' if available else 'no'}")
    return 0


def cmd_info(args: argparse.Namespace, engine: "MediaEngine") -> int:
    try:
        record = engine.asset(args.asset_id)
    except NotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        _print(record, as_json=True)
        return 0

    asset = record["asset"]
    print(f"asset {asset['id']}  {asset['media_type']}  {human_bytes(asset['size_bytes'])}")
    print(f"  hash        {asset['content_hash']}")
    print(f"  mime        {asset.get('mime_type')}")
    print(f"  captured    {asset.get('captured_at')} ({asset.get('captured_at_source')})")
    print(f"  imported    {asset.get('imported_at')}")
    if asset.get("perceptual_hash"):
        print(f"  phash       {asset['perceptual_hash']}")
    print("  paths")
    for file_row in record["files"]:
        print(f"    [{file_row['status']}] {file_row['path']}")
    technical = record.get("technical_metadata") or {}
    interesting = {
        k: v
        for k, v in technical.items()
        if v is not None and k not in ("asset_id", "raw", "extracted_at")
    }
    if interesting:
        print("  technical")
        for key, value in sorted(interesting.items()):
            print(f"    {key:<14} {value}")
    if record.get("annotations"):
        print(f"  annotations {len(record['annotations'])}")
        for annotation in record["annotations"][:20]:
            marker = "*" if annotation.get("source") == "user" else " "
            print(
                f"   {marker} {annotation['namespace']}={annotation['label']} "
                f"({annotation.get('confidence')}) by {annotation.get('plugin_id')}"
            )
    if record.get("derivatives"):
        print(f"  derivatives {len(record['derivatives'])}")
    for relation in record.get("relations", []):
        print(
            f"  relation    {relation['parent_asset_id']} -{relation['relation']}-> "
            f"{relation['child_asset_id']}"
        )
    return 0


def cmd_scans(args: argparse.Namespace, engine: "MediaEngine") -> int:
    sessions = engine.scans(args.limit)
    if args.json:
        _print(sessions, as_json=True)
        return 0
    if not sessions:
        print("no scans recorded")
        return 0
    for session in sessions:
        print(
            f"#{session['id']:<5} {session['state']:<10} {session['root_path']}\n"
            f"       seen={session['files_seen']:,} new={session['files_new']:,} "
            f"errors={session['errors']:,} started={session['started_at']}"
        )
        if session["state"] == "running" and session.get("cursor"):
            print(f"       resumable at {session['cursor']}")
    return 0


def cmd_errors(args: argparse.Namespace, engine: "MediaEngine") -> int:
    rows = engine.errors(args.limit, scope=args.scope)
    if args.json:
        _print(rows, as_json=True)
        return 0
    if not rows:
        print("no errors recorded")
        return 0
    for row in rows:
        print(f"[{row['occurred_at']}] {row['kind']} ({row.get('scope')})")
        print(f"  {row.get('ref_text') or ''}")
        print(f"  {row.get('message')}")
    return 0


def cmd_doctor(args: argparse.Namespace, engine: "MediaEngine") -> int:
    """Report on the environment and the database's health."""
    report: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "config": str(engine.config.source_path or "<defaults>"),
        "capabilities": engine.capabilities(),
        "integrity": engine.integrity_check(),
        "schema_version": engine.db.schema_version,
    }
    problems: list[str] = []
    if report["integrity"] != ["ok"]:
        problems.append("database integrity check failed")
    if not engine.config.library.roots:
        problems.append("no library roots configured")
    for name, available in report["capabilities"].items():
        if not available:
            problems.append(f"optional component unavailable: {name}")
    report["problems"] = problems

    if args.json:
        _print(report, as_json=True)
    else:
        _print_mapping(report)
    # Missing optional components are informational, not a failure; only a
    # genuine integrity problem should make this command exit non-zero.
    return 1 if report["integrity"] != ["ok"] else 0


def cmd_purge(args: argparse.Namespace, engine: "MediaEngine") -> int:
    if args.what == "derivatives":
        if args.asset_id is None:
            print("purge derivatives requires --asset-id", file=sys.stderr)
            return 2
        removed = engine.purge_asset_derivatives(args.asset_id)
        _print({"removed_files": removed}, as_json=args.json)
        return 0
    if args.what == "biometrics":
        if not args.yes:
            print(
                "This permanently deletes every face region, template, cluster and\n"
                "identity link in the library. Re-run with --yes to confirm.",
                file=sys.stderr,
            )
            return 2
        counts = engine.purge_biometrics()
        _print(counts, as_json=args.json)
        return 0
    print(f"unknown purge target: {args.what}", file=sys.stderr)
    return 2


def cmd_optimize(args: argparse.Namespace, engine: "MediaEngine") -> int:
    engine.optimize()
    if args.vacuum:
        engine.vacuum()
    _print({"optimized": True, "vacuumed": bool(args.vacuum)}, as_json=args.json)
    return 0


# ── parser ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="mediaengine",
        description="Local-first, plugin-driven media indexing engine.",
    )
    parser.add_argument("--config", metavar="PATH", help="path to config.yaml")
    parser.add_argument("--version", action="version", version=f"mediaengine {__version__}")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="override logging.level for this run",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("migrate", help="apply pending schema migrations").set_defaults(
        handler=cmd_migrate
    )

    scan = sub.add_parser("scan", help="index one or more directories")
    scan.add_argument("paths", nargs="*", help="directories to scan (default: library.roots)")
    scan.add_argument(
        "--no-resume",
        action="store_true",
        help="start a fresh session instead of continuing an interrupted one",
    )
    scan.add_argument(
        "--rehash",
        action="store_true",
        help="re-read every file even if size and mtime are unchanged",
    )
    scan.add_argument(
        "--no-derivatives", action="store_true", help="skip thumbnail and keyframe generation"
    )
    scan.set_defaults(handler=cmd_scan)

    sub.add_parser("stat", help="library counters and capabilities").set_defaults(handler=cmd_stat)

    info = sub.add_parser("info", help="everything known about one asset")
    info.add_argument("asset_id", type=int)
    info.set_defaults(handler=cmd_info)

    scans = sub.add_parser("scans", help="recent scan sessions")
    scans.add_argument("--limit", type=int, default=20)
    scans.set_defaults(handler=cmd_scans)

    errors = sub.add_parser("errors", help="recorded errors")
    errors.add_argument("--limit", type=int, default=50)
    errors.add_argument("--scope", choices=["file", "asset", "plugin", "scan", "system"])
    errors.set_defaults(handler=cmd_errors)

    sub.add_parser("doctor", help="check environment and database health").set_defaults(
        handler=cmd_doctor
    )

    purge = sub.add_parser("purge", help="delete derived data")
    purge.add_argument("what", choices=["derivatives", "biometrics"])
    purge.add_argument("--asset-id", type=int)
    purge.add_argument("--yes", action="store_true", help="confirm a destructive purge")
    purge.set_defaults(handler=cmd_purge)

    optimize = sub.add_parser("optimize", help="compact indexes")
    optimize.add_argument("--vacuum", action="store_true", help="also rewrite the database file")
    optimize.set_defaults(handler=cmd_optimize)

    return parser


def _load(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    if args.log_level:
        config.logging.level = args.log_level
    if args.quiet and config.logging.level == "INFO":
        config.logging.level = "WARNING"
    return config


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``mediaengine`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "handler", None) is None:
        parser.print_help()
        return 2

    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(
        level=config.logging.level,
        fmt=config.logging.format,
        file=config.logging.file,
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
        console=config.logging.console and not args.quiet,
    )

    from .engine import MediaEngine

    engine = MediaEngine(config)
    try:
        engine.start()
        handler: Handler = args.handler
        return handler(args, engine)
    except KeyboardInterrupt:
        engine.cancel("interrupted at the keyboard")
        print("\ninterrupted; progress is saved and the next run will resume", file=sys.stderr)
        return 130
    except MediaEngineError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
