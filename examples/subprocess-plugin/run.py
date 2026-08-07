#!/usr/bin/env python3
"""Reference MediaEngine JSONL subprocess analyzer.

This file intentionally uses only the Python standard library. Copy it to start
a real plugin, then replace :func:`analyze` while preserving the transport loop.

Protocol rule: stdout is reserved for one JSON object per line. Human logs go
to stderr. Run with ``python -u run.py`` so a host never waits on buffering.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn, TextIO


PROTOCOL = "mediaengine.analyzer/1"
MANIFEST: dict[str, Any] = {
    "protocol": PROTOCOL,
    "id": "example.subprocess",
    "version": "1.0.0",
    "model_id": "hardcoded-demo-v1",
    "accepts": ["image", "video", "document", "other"],
    "emits": ["example.subprocess"],
    "depends_on": [],
    "transfer": "paths",
    "requires": {
        "pixels": True,
        "frames": False,
        "audio": False,
        "text": False,
        "metadata_only": False,
        "gpu": False,
        "network": False,
        "max_concurrency": 2,
    },
    "namespaces": {
        "example.subprocess": {
            "display_name": "Subprocess example",
            "value_type": "categorical",
            "facetable": True,
        }
    },
}


def log(message: str) -> None:
    """Write a human-readable diagnostic without corrupting stdout JSONL."""

    print(message, file=sys.stderr, flush=True)


def send(payload: dict[str, Any], stream: TextIO = sys.stdout) -> None:
    """Send exactly one compact, newline-terminated protocol message."""

    stream.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    stream.write("\n")
    stream.flush()


def fatal(message: str, *, exit_code: int = 2) -> NoReturn:
    """Fail loudly on a broken protocol stream instead of desynchronizing."""

    log(f"fatal protocol error: {message}")
    raise SystemExit(exit_code)


def thumbnail_facts(work: dict[str, Any]) -> dict[str, Any]:
    """Demonstrate safe access to the requested 512px derivative path."""

    derivatives = work.get("derivatives")
    if not isinstance(derivatives, dict):
        return {"thumbnail_available": False}
    thumbnails = derivatives.get("thumbnails")
    if not isinstance(thumbnails, dict):
        return {"thumbnail_available": False}
    thumbnail = thumbnails.get("512")
    if not isinstance(thumbnail, dict) or not isinstance(thumbnail.get("path"), str):
        return {"thumbnail_available": False}

    # A real plugin would decode this path. Plugins never modify derivatives.
    path = Path(thumbnail["path"])
    try:
        return {
            "thumbnail_available": True,
            "thumbnail_filename": path.name,
            "thumbnail_size_bytes": path.stat().st_size,
        }
    except OSError as exc:
        log(f"thumbnail unavailable at {path}: {exc}")
        return {"thumbnail_available": False, "thumbnail_filename": path.name}


def analyze(message: dict[str, Any]) -> dict[str, Any]:
    """Return two deterministic annotations, or a deliberate error example."""

    request_id = message.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        fatal("analyze message requires a non-empty request_id")

    config = message.get("config")
    if isinstance(config, dict) and config.get("force_error") is True:
        # Errors belong on the protocol channel as JSON, not as Python tracebacks.
        return {
            "type": "error",
            "request_id": request_id,
            "kind": "demonstration_error",
            "message": "config.force_error requested the documented error path",
            "retryable": False,
        }

    asset_value = message.get("asset")
    asset: dict[str, Any] = asset_value if isinstance(asset_value, dict) else {}
    prior = message.get("prior_annotations")
    prior_annotations = prior if isinstance(prior, list) else []
    facts = thumbnail_facts(message)
    facts.update(
        {
            "asset_id": asset.get("asset_id"),
            "filename": asset.get("filename"),
            "prior_annotation_count": len(prior_annotations),
        }
    )

    annotations: list[dict[str, Any]] = [
        {
            "namespace": "example.subprocess",
            "label": "processed",
            "value": facts,
            "confidence": 1.0,
        },
        {
            "namespace": "example.subprocess",
            "label": "has-prior-context" if prior_annotations else "no-prior-context",
            "value": {"producer_values_seen": len(prior_annotations)},
            "confidence": 1.0,
        },
    ]
    return {"type": "result", "request_id": request_id, "annotations": annotations}


def handle(message: dict[str, Any], *, ready: bool) -> tuple[bool, bool]:
    """Handle one message and return ``(still_running, now_ready)``."""

    message_type = message.get("type")
    if message_type == "hello":
        if ready:
            fatal("received a second hello after handshake")
        if message.get("protocol") != PROTOCOL:
            fatal(f"unsupported protocol {message.get('protocol')!r}")
        send({"type": "ready", "manifest": MANIFEST})
        log("handshake complete")
        return True, True
    if not ready:
        fatal("first message must be hello")
    if message_type == "analyze":
        send(analyze(message))
        return True, ready
    if message_type == "shutdown":
        log("clean shutdown requested")
        return False, ready
    fatal(f"unknown message type {message_type!r}")


def main() -> int:
    """Read complete JSON objects from stdin until shutdown or EOF."""

    ready = False
    for line_number, raw_line in enumerate(sys.stdin, start=1):
        line = raw_line.strip()
        if not line:
            fatal(f"empty JSONL record at line {line_number}")
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            fatal(f"malformed JSON at line {line_number}: {exc}")
        if not isinstance(decoded, dict):
            fatal(f"line {line_number} must contain a JSON object")
        running, ready = handle(decoded, ready=ready)
        if not running:
            return 0
    log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
