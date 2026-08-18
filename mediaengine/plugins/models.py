"""Local model inventory and installation for the LM Studio analyzer.

The analyzer itself only knows how to talk to an OpenAI-compatible endpoint.
This module is the piece that answers the operator-facing questions the
analyzer cannot: *which models exist on this machine*, *which of them can see
pixels*, and *how do I get one that can*.

Installation shells out to LM Studio's own ``lms`` CLI rather than downloading
GGUF files directly.  That keeps model storage, quantization selection, and
hardware fit as LM Studio's problem, and it means a model installed from here
is indistinguishable from one the user installed themselves.

Nothing here writes to the library database, and nothing downloads without an
explicit call to :meth:`ModelLibrary.install`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.procs import kill_process_tree, popen
from ..errors import PluginExecutionError, PluginUnavailable

__all__ = [
    "CATALOG",
    "CatalogEntry",
    "InstalledModel",
    "ModelLibrary",
    "find_lms_cli",
    "parse_progress",
]

_LOG = logging.getLogger(__name__)

PLUGIN_ID = "local.lm-studio"

#: Terminal control sequences the CLI emits for its spinner and progress bar.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_RATIO = re.compile(
    r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)\s*/\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)",
    re.IGNORECASE,
)
_UNITS = {"B": 1.0, "KB": 1024.0, "MB": 1024.0**2, "GB": 1024.0**3, "TB": 1024.0**4}


@dataclass(frozen=True, slots=True)
class InstalledModel:
    """One model already present on disk, as reported by ``lms ls``."""

    key: str
    display_name: str
    publisher: str
    size_bytes: int
    architecture: str
    parameters: str
    quantization: str
    vision: bool
    kind: str
    loaded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "publisher": self.publisher,
            "size_bytes": self.size_bytes,
            "architecture": self.architecture,
            "parameters": self.parameters,
            "quantization": self.quantization,
            "vision": self.vision,
            "kind": self.kind,
            "loaded": self.loaded,
            "installed": True,
        }


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """A model Studio offers to install, described for a non-expert reader."""

    key: str
    name: str
    summary: str
    vision: bool
    approx_bytes: int
    #: Rough working-set size; the store greys out entries that will not fit.
    vram_gb: float
    best_for: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.name,
            "summary": self.summary,
            "vision": self.vision,
            "size_bytes": self.approx_bytes,
            "vram_gb": self.vram_gb,
            "best_for": list(self.best_for),
            "installed": False,
            "loaded": False,
            "kind": "llm",
        }


_GB = 1024**3

#: Curated starting points, resolved by LM Studio's own catalog at install
#: time.  Sizes are approximate because LM Studio picks the quantization that
#: fits the machine; the store labels them as such.
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        key="qwen/qwen3-vl-4b",
        name="Qwen3-VL 4B",
        summary=(
            "Small vision model. Watches video keyframes and photos and returns categories, "
            "tags, and a one-line summary. The best first choice on most GPUs."
        ),
        vision=True,
        approx_bytes=round(3.3 * _GB),
        vram_gb=5.0,
        best_for=("video", "photos"),
    ),
    CatalogEntry(
        key="qwen/qwen3-vl-8b",
        name="Qwen3-VL 8B",
        summary=(
            "Larger vision model with noticeably better scene and object naming. "
            "Recommended when you have 10 GB or more of VRAM."
        ),
        vision=True,
        approx_bytes=round(6.6 * _GB),
        vram_gb=10.0,
        best_for=("video", "photos"),
    ),
    CatalogEntry(
        key="qwen/qwen3-vl-30b",
        name="Qwen3-VL 30B",
        summary=(
            "Highest-quality local vision tagging. Slower per file and needs a large GPU, "
            "but reads fine detail and on-screen text well."
        ),
        vision=True,
        approx_bytes=round(18.0 * _GB),
        vram_gb=22.0,
        best_for=("video", "photos"),
    ),
    CatalogEntry(
        key="google/gemma-3-4b",
        name="Gemma 3 4B",
        summary=(
            "Compact vision model with strong everyday captions. A good alternative "
            "to Qwen3-VL 4B if you prefer Google's phrasing."
        ),
        vision=True,
        approx_bytes=round(3.3 * _GB),
        vram_gb=5.0,
        best_for=("video", "photos"),
    ),
    CatalogEntry(
        key="google/gemma-3-12b",
        name="Gemma 3 12B",
        summary="Mid-size vision model. Richer descriptions of complex scenes.",
        vision=True,
        approx_bytes=round(8.1 * _GB),
        vram_gb=12.0,
        best_for=("video", "photos"),
    ),
    CatalogEntry(
        key="mistralai/mistral-small-3.2",
        name="Mistral Small 3.2",
        summary="Vision-capable general model; strong at documents and screenshots.",
        vision=True,
        approx_bytes=round(14.0 * _GB),
        vram_gb=16.0,
        best_for=("documents", "photos"),
    ),
    CatalogEntry(
        key="qwen/qwen3-4b-2507",
        name="Qwen3 4B",
        summary=(
            "Text-only. Categorises documents and filenames quickly and cheaply; "
            "it cannot look at video or photos."
        ),
        vision=False,
        approx_bytes=round(2.5 * _GB),
        vram_gb=4.0,
        best_for=("documents",),
    ),
    CatalogEntry(
        key="openai/gpt-oss-20b",
        name="GPT-OSS 20B",
        summary="Text-only, high quality. Best for document-heavy libraries.",
        vision=False,
        approx_bytes=round(12.0 * _GB),
        vram_gb=14.0,
        best_for=("documents",),
    ),
)


def find_lms_cli() -> Path | None:
    """Locate LM Studio's CLI.

    ``lms`` installs itself under the user profile and is frequently absent
    from ``PATH`` on Windows, so a plain ``shutil.which`` is not enough.
    """
    from shutil import which

    found = which("lms")
    if found:
        return Path(found)
    executable = "lms.exe" if os.name == "nt" else "lms"
    candidates = [
        Path.home() / ".lmstudio" / "bin" / executable,
        Path.home() / ".cache" / "lm-studio" / "bin" / executable,
    ]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "LM-Studio" / "bin" / executable)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def parse_progress(line: str) -> tuple[float | None, str]:
    """Extract ``(fraction, cleaned_text)`` from one CLI output line.

    The CLI redraws a spinner in place, so this is deliberately forgiving: any
    percentage or ``downloaded / total`` pair anywhere in the line counts, and
    a line with neither simply reports indeterminate progress.
    """
    cleaned = _ANSI.sub("", line).replace("\r", " ").strip()
    cleaned = re.sub(r"[⠀-⣿─-╿■-◿]", "", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    ratio = _RATIO.search(cleaned)
    if ratio:
        done = float(ratio.group(1)) * _UNITS[ratio.group(2).upper()]
        total = float(ratio.group(3)) * _UNITS[ratio.group(4).upper()]
        if total > 0:
            return max(0.0, min(1.0, done / total)), cleaned
    percent = _PERCENT.search(cleaned)
    if percent:
        return max(0.0, min(1.0, float(percent.group(1)) / 100.0)), cleaned
    return None, cleaned


@dataclass(slots=True)
class _Download:
    """Mutable handle for one in-flight install, so the UI can cancel it."""

    process: subprocess.Popen[bytes] | None = None
    cancelled: bool = False
    log: list[str] = field(default_factory=list)


class ModelLibrary:
    """Inventory, install, and load local models through the ``lms`` CLI."""

    def __init__(
        self,
        *,
        cli_path: Path | str | None = None,
        base_url: str = "http://127.0.0.1:1234",
        timeout_s: float = 20.0,
    ) -> None:
        self.cli_path = Path(cli_path) if cli_path else find_lms_cli()
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._download = _Download()

    # ── availability ────────────────────────────────────────────────────────

    @property
    def cli_available(self) -> bool:
        """Whether installation is possible at all on this machine."""
        return self.cli_path is not None and self.cli_path.is_file()

    def _run_cli(self, args: Sequence[str], *, timeout_s: float | None = None) -> str:
        if self.cli_path is None:
            raise PluginUnavailable(
                "LM Studio's command line tool was not found. Install LM Studio, then "
                "run 'lms bootstrap' once so Studio can manage models.",
                plugin_id=PLUGIN_ID,
            )
        process = popen([str(self.cli_path), *args], stdin=subprocess.DEVNULL)
        try:
            stdout, stderr = process.communicate(timeout=timeout_s or self.timeout_s)
        except subprocess.TimeoutExpired:
            kill_process_tree(process)
            raise PluginUnavailable(
                f"LM Studio did not answer 'lms {args[0]}' within {timeout_s or self.timeout_s:.0f}s",
                plugin_id=PLUGIN_ID,
            ) from None
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:400]
            raise PluginExecutionError(
                f"lms {' '.join(args)} failed: {detail or 'unknown error'}",
                plugin_id=PLUGIN_ID,
            )
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _parse_rows(payload: str) -> list[dict[str, Any]]:
        """Parse CLI JSON, tolerating banner text printed before the array."""
        start = payload.find("[")
        if start < 0:
            return []
        try:
            value = json.loads(payload[start:])
        except json.JSONDecodeError:
            return []
        return [dict(row) for row in value if isinstance(row, Mapping)]

    # ── inventory ───────────────────────────────────────────────────────────

    @staticmethod
    def _model(row: Mapping[str, Any], *, loaded: bool = False) -> InstalledModel:
        quantization = row.get("quantization")
        name = quantization.get("name") if isinstance(quantization, Mapping) else quantization
        return InstalledModel(
            key=str(row.get("modelKey") or row.get("identifier") or ""),
            display_name=str(row.get("displayName") or row.get("modelKey") or "Model"),
            publisher=str(row.get("publisher") or ""),
            size_bytes=int(row.get("sizeBytes") or 0),
            architecture=str(row.get("architecture") or ""),
            parameters=str(row.get("paramsString") or ""),
            quantization=str(name or ""),
            vision=bool(row.get("vision")),
            kind=str(row.get("type") or "llm"),
            loaded=loaded,
        )

    def loaded_keys(self) -> set[str]:
        """Model keys currently resident in memory, or an empty set on failure."""
        try:
            rows = self._parse_rows(self._run_cli(["ps", "--json"]))
        except (PluginUnavailable, PluginExecutionError) as exc:
            _LOG.debug("lms ps unavailable: %s", exc)
            return set()
        return {str(row.get("modelKey") or row.get("identifier") or "") for row in rows}

    def installed(self) -> list[InstalledModel]:
        """Every model on disk. Raises only if the CLI itself is unusable."""
        rows = self._parse_rows(self._run_cli(["ls", "--json"]))
        loaded = self.loaded_keys()
        models = [self._model(row, loaded=str(row.get("modelKey") or "") in loaded) for row in rows]
        return [model for model in models if model.key]

    def served_ids(self) -> list[str]:
        """Model ids the running server will accept, via the OpenAI endpoint.

        This is the authority for what the analyzer may put in a request, and
        it is how Studio can warn about a stale configured model id without
        burning a full inference call to find out.
        """
        try:
            import httpx
        except ImportError:
            return []
        try:
            with httpx.Client(timeout=min(10.0, self.timeout_s)) as client:
                response = client.get(f"{self.base_url}/v1/models")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - any failure means "cannot tell"
            _LOG.debug("LM Studio model listing failed: %s", exc)
            return []
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            return []
        return [str(row["id"]) for row in rows if isinstance(row, Mapping) and row.get("id")]

    # ── the store view ──────────────────────────────────────────────────────

    def store(self, *, vision_only: bool = False) -> list[dict[str, Any]]:
        """Installed models first, then catalog entries not yet on disk.

        Merging the two lists in one place is what lets the UI render a single
        scrolling shelf where "installed" is a state of a model rather than a
        separate screen.
        """
        try:
            installed = self.installed()
        except (PluginUnavailable, PluginExecutionError) as exc:
            _LOG.info("model inventory unavailable: %s", exc)
            installed = []
        by_key = {model.key: model for model in installed}
        rows: list[dict[str, Any]] = []
        for model in installed:
            if model.kind != "llm":
                continue
            row = model.as_dict()
            row["summary"] = self._describe(model)
            row["best_for"] = ["video", "photos"] if model.vision else ["documents"]
            rows.append(row)
        for entry in CATALOG:
            if entry.key in by_key:
                continue
            rows.append(entry.as_dict())
        if vision_only:
            rows = [row for row in rows if row.get("vision")]
        rows.sort(key=lambda row: (not row["vision"], not row["installed"], str(row["display_name"])))
        return rows

    @staticmethod
    def _describe(model: InstalledModel) -> str:
        parts = [
            "Sees images and video frames" if model.vision else "Text only — cannot look at video",
        ]
        if model.parameters:
            parts.append(f"{model.parameters} parameters")
        if model.quantization:
            parts.append(model.quantization)
        if model.publisher:
            parts.append(f"by {model.publisher}")
        return " · ".join(parts)

    def recommended_vision_key(self) -> str | None:
        """The best already-installed vision model, smallest first.

        Smallest-first is deliberate: a model that runs is worth more than a
        model that is better on paper and swaps to disk on every frame.
        """
        try:
            candidates = [m for m in self.installed() if m.vision and m.kind == "llm"]
        except (PluginUnavailable, PluginExecutionError):
            return None
        if not candidates:
            return None
        candidates.sort(key=lambda model: (not model.loaded, model.size_bytes))
        return candidates[0].key

    # ── installation ────────────────────────────────────────────────────────

    def install(
        self,
        key: str,
        *,
        on_progress: Callable[[float | None, str], None] | None = None,
    ) -> str:
        """Download ``key`` through LM Studio, streaming progress as it goes.

        ``key`` may be an LM Studio catalog name (``qwen/qwen3-vl-4b``), a
        specific quantization (``...@q4_k_m``), or a full Hugging Face URL —
        whatever ``lms get`` accepts. Returns the final CLI line on success.
        """
        cleaned = key.strip()
        if not cleaned:
            raise PluginExecutionError("no model was selected", plugin_id=PLUGIN_ID)
        if self.cli_path is None:
            raise PluginUnavailable(
                "LM Studio's command line tool was not found, so Studio cannot install "
                "models for you. Install LM Studio and run 'lms bootstrap' once.",
                plugin_id=PLUGIN_ID,
            )
        self._download = _Download()
        process = popen(
            [str(self.cli_path), "get", cleaned, "--yes"],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        self._download.process = process
        last = ""
        try:
            for fraction, text in self._stream(process):
                if text:
                    last = text
                    self._download.log.append(text)
                    del self._download.log[:-40]
                if on_progress is not None:
                    on_progress(fraction, text or f"Downloading {cleaned}…")
            returncode = process.wait(timeout=30)
        finally:
            if process.poll() is None:
                kill_process_tree(process)
        if self._download.cancelled:
            raise PluginUnavailable(f"download of {cleaned} was cancelled", plugin_id=PLUGIN_ID)
        if returncode != 0:
            tail = " ".join(self._download.log[-4:]) or "no output"
            raise PluginExecutionError(
                f"LM Studio could not download {cleaned}: {tail}", plugin_id=PLUGIN_ID
            )
        return last or cleaned

    def _stream(self, process: subprocess.Popen[bytes]) -> Iterator[tuple[float | None, str]]:
        """Yield progress from the CLI's in-place redraws.

        The CLI separates frames with carriage returns rather than newlines, so
        reading by line would block until the download finished. Reading small
        chunks and splitting on either terminator keeps the bar moving.
        """
        stream = process.stdout
        if stream is None:  # pragma: no cover - popen always gives us a pipe
            return
        buffer = b""
        previous = ""
        while True:
            chunk = stream.read(256)
            if not chunk:
                break
            buffer += chunk
            parts = re.split(rb"[\r\n]", buffer)
            buffer = parts.pop()
            for part in parts:
                fraction, text = parse_progress(part.decode("utf-8", errors="replace"))
                if not text or text == previous:
                    continue
                previous = text
                yield fraction, text
        if buffer:
            yield parse_progress(buffer.decode("utf-8", errors="replace"))

    def cancel_install(self) -> None:
        """Stop the current download. LM Studio keeps resumable partial files."""
        self._download.cancelled = True
        process = self._download.process
        if process is not None and process.poll() is None:
            kill_process_tree(process)

    def load(self, key: str, *, timeout_s: float = 300.0) -> None:
        """Bring a model into memory so the first analysis is not a cold start."""
        self._run_cli(["load", key, "--yes"], timeout_s=timeout_s)
