#!/usr/bin/env python3
"""Black-box contract harness for the example JSONL subprocess plugin."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO


ROOT = Path(__file__).resolve().parent
PROTOCOL = "mediaengine.analyzer/1"


def send(stream: TextIO, value: dict[str, Any]) -> None:
    """Send one JSONL message."""

    stream.write(json.dumps(value) + "\n")
    stream.flush()


def receive(stream: TextIO) -> dict[str, Any]:
    """Receive and validate one JSON object from the child."""

    line = stream.readline()
    assert line, "plugin closed stdout before responding"
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


def work_item(thumbnail_path: Path, *, request_id: str, force_error: bool = False) -> dict[str, Any]:
    """Build a synthetic host message matching CONTRACTS.md section 2."""

    return {
        "type": "analyze",
        "protocol": PROTOCOL,
        "request_id": request_id,
        "deadline_s": 5.0,
        "asset": {
            "asset_id": 7,
            "content_hash": "sha256:demo",
            "media_type": "image",
            "mime_type": "image/webp",
            "size_bytes": thumbnail_path.stat().st_size,
            "captured_at": None,
            "filename": "fixture.webp",
        },
        "metadata": {"width": 1, "height": 1},
        "derivatives": {"thumbnails": {"512": {"path": str(thumbnail_path.resolve())}}},
        "text": None,
        "prior_annotations": [
            {"namespace": "core.demo", "label": "prior", "producer": "core.demo@1.0.0"}
        ],
        "config": {"force_error": force_error},
    }


def main() -> int:
    """Exercise handshake, success, error, and graceful shutdown over pipes."""

    with tempfile.TemporaryDirectory() as temp_dir:
        thumbnail = Path(temp_dir) / "thumb_512.webp"
        thumbnail.write_bytes(b"synthetic derivative")
        process = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "run.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert process.stdin is not None
        assert process.stdout is not None

        send(process.stdin, {"type": "hello", "protocol": PROTOCOL, "config": {}})
        ready = receive(process.stdout)
        assert ready["type"] == "ready"
        assert ready["manifest"]["protocol"] == PROTOCOL
        assert ready["manifest"]["id"] == "example.subprocess"

        send(process.stdin, work_item(thumbnail, request_id="success-1"))
        result = receive(process.stdout)
        assert result["type"] == "result"
        assert result["request_id"] == "success-1"
        assert len(result["annotations"]) == 2
        assert result["annotations"][0]["namespace"] == "example.subprocess"
        assert result["annotations"][0]["value"]["thumbnail_available"] is True
        assert result["annotations"][0]["value"]["prior_annotation_count"] == 1

        send(process.stdin, work_item(thumbnail, request_id="error-1", force_error=True))
        error = receive(process.stdout)
        assert error == {
            "type": "error",
            "request_id": "error-1",
            "kind": "demonstration_error",
            "message": "config.force_error requested the documented error path",
            "retryable": False,
        }

        send(process.stdin, {"type": "shutdown"})
        return_code = process.wait(timeout=5.0)
        assert return_code == 0
        assert process.stdout.read() == "", "stdout contained non-protocol output"

    print("subprocess plugin contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

