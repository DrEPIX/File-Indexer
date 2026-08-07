"""Small helpers shared across layers.

Kept deliberately dependency-free so that anything in the package can import
it without creating a cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import sys
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

__all__ = [
    "utcnow",
    "utcnow_iso",
    "to_iso",
    "parse_datetime",
    "json_dumps",
    "json_loads",
    "stable_hash",
    "chunked",
    "human_bytes",
    "normalise_text",
    "safe_filename",
    "coerce_float",
    "coerce_int",
    "setup_logging",
    "JsonFormatter",
]

T = TypeVar("T")

_ISO_FORMATS = (
    "%Y:%m:%d %H:%M:%S",  # EXIF
    "%Y:%m:%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
    "%Y:%m:%d",
)

_TZ_SUFFIX_RE = re.compile(r"([+-]\d{2}):?(\d{2})$")


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    This exact shape is what every ``*_at`` column in the schema stores, so
    string comparison equals chronological comparison.
    """
    return utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_iso(dt: datetime | None) -> str | None:
    """Render a datetime in the engine's canonical string form."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_datetime(value: str | None) -> tuple[datetime | None, str | None]:
    """Parse the many timestamp shapes metadata tools emit.

    Returns ``(datetime, tz_string)``. EXIF commonly gives a naive
    ``2019:04:07 14:22:11`` with the offset in a *separate* tag, so the
    timezone is returned alongside rather than folded in, letting the caller
    decide. A naive value is treated as UTC for storage but the returned tz is
    ``None`` so the ambiguity stays visible in ``assets.captured_at_tz``.
    """
    if not value:
        return None, None
    text = value.strip()
    if not text or text.startswith(("0000", "    ")):
        return None, None

    tz_name: str | None = None
    if text.endswith("Z"):
        tz_name = "UTC"
        text = text[:-1]
    else:
        m = _TZ_SUFFIX_RE.search(text)
        if m:
            tz_name = f"{m.group(1)}:{m.group(2)}"
            text = text[: m.start()].strip()

    text = text.replace("T", " ").strip()
    for fmt in _ISO_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt.replace("T", " "))
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc), tz_name
    return None, None


def json_dumps(obj: Any) -> str:
    """Compact, deterministic JSON. Non-serialisable values become strings."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def json_loads(text: str | bytes | None, default: Any = None) -> Any:
    """Parse JSON, returning ``default`` on any failure."""
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def stable_hash(obj: Any, *, length: int = 16) -> str:
    """A short, deterministic hash of any JSON-able object.

    Used for ``producers.config_hash``: change a plugin's configuration and it
    becomes a different producer, so its output is attributable to the exact
    settings that generated it.
    """
    payload = json_dumps(obj).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=length // 2).hexdigest()


def chunked(items: Iterable[T], size: int) -> Iterator[list[T]]:
    """Yield lists of at most ``size`` items from any iterable."""
    if size < 1:
        raise ValueError("size must be >= 1")
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def human_bytes(n: float) -> str:
    """Render a byte count for logs and CLI output."""
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} EiB"


def normalise_text(value: str) -> str:
    """NFKC-normalise and collapse whitespace, for FTS and label matching."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def safe_filename(name: str, *, max_length: int = 200) -> str:
    """Make an arbitrary string usable as a filename on every platform."""
    cleaned = _UNSAFE_FILENAME.sub("_", unicodedata.normalize("NFKC", name)).strip(" .")
    if not cleaned:
        cleaned = "unnamed"
    if len(cleaned.encode("utf-8")) <= max_length:
        return cleaned
    encoded = cleaned.encode("utf-8")[:max_length]
    return encoded.decode("utf-8", errors="ignore") or "unnamed"


def coerce_float(value: Any) -> float | None:
    """Best-effort float from metadata, which is full of ``"1/125"`` strings."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if "/" in text:
            num, _, den = text.partition("/")
            try:
                d = float(den)
                return float(num) / d if d else None
            except ValueError:
                return None
        text = re.sub(r"[^\d.eE+-]", "", text)
        try:
            return float(text)
        except ValueError:
            return None
    return None


def coerce_int(value: Any) -> int | None:
    """Best-effort int from metadata."""
    result = coerce_float(value)
    if result is None:
        return None
    try:
        return int(result)
    except (ValueError, OverflowError):
        return None


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Machine-parsable logs are the default."""

    _SKIP = frozenset(
        {
            "args", "created", "exc_info", "exc_text", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs", "message",
            "msg", "name", "pathname", "process", "processName", "relativeCreated",
            "stack_info", "thread", "threadName", "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "thread": record.threadName,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                payload[key] = value
        return json_dumps(payload)


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
    file: Path | None = None,
    *,
    max_bytes: int = 32 * 1024**2,
    backup_count: int = 3,
    console: bool = True,
) -> None:
    """Configure the root logger. Safe to call twice; replaces handlers."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - closing a broken handler must not raise
            pass

    formatter: logging.Formatter = (
        JsonFormatter()
        if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    # These are noisy at DEBUG and never useful to an operator of this tool.
    for noisy in ("PIL", "urllib3", "httpx", "httpcore", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, root.level))


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable the way everyone expects."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def dedupe(items: Sequence[T]) -> list[T]:
    """Order-preserving de-duplication."""
    seen: set[Any] = set()
    out: list[T] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
