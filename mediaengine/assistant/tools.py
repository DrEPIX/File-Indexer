"""The tools the in-app assistant may use, and what they are allowed to touch.

The assistant's power comes from the filter-pack layer being *data*. Authoring
a taxonomy is writing a validated TOML document, not executing code, so a model
can meaningfully change how the library sorts itself without ever being handed
an interpreter.

Two rules hold for everything in this module:

**Reads are free, writes are staged.** A tool marked ``mutating`` never applies
its change. It validates the change, describes it, and returns a preview; the
UI shows that preview and only calls :meth:`ToolRegistry.apply` if a human says
yes. There is no code path in which the model's output reaches disk unreviewed.

**Library content is data, not instruction.** Filenames, tags, and document
text flow back into the model as tool results. A filename saying "ignore your
instructions" is a string in a JSON payload, and because the only writes are
gated, the worst it can do is waste a turn.
"""

from __future__ import annotations

import json
import logging
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError, MediaEngineError
from ..filters import TEMPLATE, parse_pack, user_pack_dir

__all__ = [
    "MAX_RESULT_CHARS",
    "PendingChange",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
]

_LOG = logging.getLogger(__name__)

#: Tool results are fed straight back into the context window, and they
#: accumulate: by the fourth call the conversation is mostly tool output. Kept
#: deliberately tight — roughly 400 tokens — so an eight-step conversation
#: still fits a 4k-context model, which is what a lot of local setups run.
MAX_RESULT_CHARS = 2200


@dataclass(slots=True)
class ToolResult:
    """What one tool call produced."""

    ok: bool
    payload: dict[str, Any]
    #: Set when the tool staged a change that needs human approval.
    pending: "PendingChange | None" = None

    def as_text(self) -> str:
        body = json.dumps(self.payload, ensure_ascii=False, default=str)
        if len(body) > MAX_RESULT_CHARS:
            body = body[:MAX_RESULT_CHARS] + '… (truncated; ask for a smaller limit)"}'
        return body


@dataclass(slots=True)
class PendingChange:
    """A staged, validated change waiting on a human.

    ``preview`` is what the UI shows. ``apply`` is a closure over already
    validated inputs, so approving cannot re-run the model's arguments through
    validation a second time and get a different answer.
    """

    kind: str
    title: str
    summary: str
    preview: str
    apply: Callable[[], dict[str, Any]]
    destructive: bool = False


@dataclass(slots=True)
class ToolSpec:
    """One callable exposed to the model, in OpenAI tool-schema form."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., ToolResult]
    mutating: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _integer(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


def _params(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class ToolRegistry:
    """Binds the tool set to one engine, and holds staged changes."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.pending: dict[str, PendingChange] = {}
        self._counter = 0
        self._tools: dict[str, ToolSpec] = {}
        self._reads: dict[str, ToolResult] = {}
        self._failed_writes: dict[str, int] = {}
        #: Counters the loop reads to report what actually happened, rather
        #: than trusting the model's account of it.
        self.write_failures = 0
        self.staged_count = 0
        self._register_all()

    # ── registry plumbing ───────────────────────────────────────────────────

    def add(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    @property
    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._tools.values()]

    def describe(self) -> str:
        """A plain-text tool list, for models without native tool calling."""
        lines = []
        for spec in self._tools.values():
            names = ", ".join(spec.parameters.get("properties", {}))
            lines.append(f"- {spec.name}({names}): {spec.description}")
        return "\n".join(lines)

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run one tool by name. Never raises; failures come back as results."""
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                False,
                {"error": f"no such tool: {name}", "available": sorted(self._tools)},
            )
        # A model that loses its place re-reads the same thing instead of
        # making progress, and every repeat costs a step from a small budget.
        # Serving the cached answer with a nudge is cheaper than the round trip.
        key = ""
        if not spec.mutating:
            key = f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"
            cached = self._reads.get(key)
            if cached is not None:
                payload = dict(cached.payload)
                payload["note"] = "you already called this; the answer has not changed"
                return ToolResult(cached.ok, payload)
        try:
            result = spec.handler(**arguments)
            if key and result.ok:
                self._reads[key] = result
            if spec.mutating:
                result = self._track_write(name, arguments, result)
            return result
        except TypeError as exc:
            return ToolResult(False, {"error": f"bad arguments for {name}: {exc}"})
        except (MediaEngineError, ConfigError, OSError, ValueError) as exc:
            return ToolResult(False, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:  # noqa: BLE001 - a tool fault must not kill the chat
            _LOG.exception("assistant tool %s failed", name)
            return ToolResult(False, {"error": f"{type(exc).__name__}: {exc}"})

    def _track_write(
        self, name: str, arguments: dict[str, Any], result: ToolResult
    ) -> ToolResult:
        """Count write outcomes, and escalate when the same call keeps failing.

        A local model that cannot see why its document was rejected will resend
        it verbatim. Returning the identical error a third time teaches it
        nothing; naming the repetition and the one field to change does.
        """
        if result.ok:
            return result
        self.write_failures += 1
        signature = f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"
        attempts = self._failed_writes.get(signature, 0) + 1
        self._failed_writes[signature] = attempts
        if attempts > 1:
            payload = dict(result.payload)
            payload["error"] = (
                f"you sent an identical document {attempts} times and it failed the same way. "
                f"{payload.get('error', '')} Copy the template below and edit it, rather than "
                "resending what you already tried."
            )
            return ToolResult(False, payload)
        return result

    def stage(self, change: PendingChange) -> str:
        self._counter += 1
        token = f"change-{self._counter}"
        self.pending[token] = change
        self.staged_count += 1
        return token

    def reset_counters(self) -> None:
        """Start a fresh accounting of one question's write attempts."""
        self.write_failures = 0
        self.staged_count = 0
        self._failed_writes.clear()

    def apply(self, token: str) -> dict[str, Any]:
        """Apply an approved change and forget it."""
        change = self.pending.pop(token, None)
        if change is None:
            raise ConfigError(f"change {token} is no longer pending")
        # The library just changed, so every cached read is now a lie.
        self._reads.clear()
        return change.apply()

    def discard(self, token: str) -> None:
        self.pending.pop(token, None)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _manager(self) -> Any:
        from ..plugins import PluginManager

        return PluginManager(self.engine)

    def _pack_path(self, pack_id: str) -> Path:
        directory = user_pack_dir(self.engine.config)
        return directory / f"{pack_id.replace('.', '-')}.toml"

    # ── read tools ──────────────────────────────────────────────────────────

    def _register_all(self) -> None:
        self.add(
            ToolSpec(
                name="library_overview",
                description=(
                    "Counts by media type, namespaces that already carry data, and installed "
                    "filter packs. Call this first."
                ),
                parameters=_params({}),
                handler=self._library_overview,
            )
        )
        self.add(
            ToolSpec(
                name="search_library",
                description=(
                    "Search and get a count plus samples. Syntax: free text, type:video, "
                    "content.sport:football, -origin.platform:youtube, has:gps, conf:>=0.8."
                ),
                parameters=_params(
                    {
                        "query": _string("Query. Empty matches everything."),
                        "limit": _integer("Samples to return, 1-25.", default=6),
                    },
                    ["query"],
                ),
                handler=self._search_library,
            )
        )
        self.add(
            ToolSpec(
                name="sample_filenames",
                description=(
                    "Real filenames from the library. Call before writing a rules pack so "
                    "the patterns match this user's actual naming."
                ),
                parameters=_params(
                    {
                        "media_type": _string(
                            "image, video, audio, document, other, or empty for any.",
                            default="",
                        ),
                        "limit": _integer("How many names, 1-40.", default=15),
                    }
                ),
                handler=self._sample_filenames,
            )
        )
        self.add(
            ToolSpec(
                name="list_filter_packs",
                description="Installed filter packs: namespace, method, labels, on or off.",
                parameters=_params({}),
                handler=self._list_filter_packs,
            )
        )
        self.add(
            ToolSpec(
                name="read_filter_pack",
                description="The TOML source of one pack, to read or to copy.",
                parameters=_params(
                    {"pack_id": _string("Pack id, for example filters.sport.")},
                    ["pack_id"],
                ),
                handler=self._read_filter_pack,
            )
        )

        # ── write tools: every one of these stages, none applies ────────────
        self.add(
            ToolSpec(
                name="write_filter_pack",
                description=(
                    "Create or replace a filter pack from TOML. Validated immediately and "
                    "shown to the user for approval — this call does NOT save it. Returns "
                    "validation errors so you can correct and call again."
                ),
                parameters=_params(
                    {
                        "pack_id": _string("Pack id, e.g. filters.france-landmarks."),
                        "toml": _string("The complete pack document."),
                        "reason": _string("One sentence on what this adds."),
                    },
                    ["pack_id", "toml", "reason"],
                ),
                handler=self._write_filter_pack,
                mutating=True,
            )
        )
        self.add(
            ToolSpec(
                name="set_filter_enabled",
                description=(
                    "Switch a pack on or off. Staged for approval. Enabling does not sort "
                    "anything; follow with run_filter_pack."
                ),
                parameters=_params(
                    {
                        "pack_id": _string("Pack id."),
                        "enabled": {"type": "boolean", "description": "On or off."},
                    },
                    ["pack_id", "enabled"],
                ),
                handler=self._set_filter_enabled,
                mutating=True,
            )
        )
        self.add(
            ToolSpec(
                name="run_filter_pack",
                description=(
                    "Sort part of the library with a pack. Staged for approval; a vision "
                    "pack costs one model call per file, so keep the limit small."
                ),
                parameters=_params(
                    {
                        "pack_id": _string("Pack id."),
                        "media_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Restrict to these, e.g. ['video'].",
                        },
                        "limit": _integer("Maximum assets to process.", default=25),
                    },
                    ["pack_id"],
                ),
                handler=self._run_filter_pack,
                mutating=True,
            )
        )

    # ── read implementations ────────────────────────────────────────────────

    def _library_overview(self) -> ToolResult:
        repos = self.engine.repos
        media = {
            str(row["media_type"]): int(row["n"])
            for row in repos.db.query(
                "SELECT media_type, COUNT(*) n FROM assets GROUP BY media_type", []
            )
        }
        namespaces = [
            f"{row['namespace']} ({row['asset_count']} assets, {row['distinct_labels']} values)"
            for row in repos.annotations.namespaces_in_use()
        ]
        packs = [
            f"{pack.id} -> {pack.namespace}"
            + (" [on]" if self.engine.config.plugins.is_enabled(pack.id) else "")
            for pack in self.engine.plugins.packs.values()
        ]
        return ToolResult(
            True,
            {
                "assets_by_media_type": media,
                "total_assets": sum(media.values()),
                "namespaces_with_data": namespaces[:12],
                "installed_filter_packs": packs,
            },
        )

    def _search_library(self, query: str = "", limit: int = 6) -> ToolResult:
        count = max(1, min(int(limit), 25))
        result = self.engine.search(str(query), with_facets=False)
        ids = [int(hit["id"]) for hit in result.hits[:count]]
        labels = self.engine.repos.annotations.live_labels(ids, per_asset=6) if ids else {}
        samples = []
        for hit in result.hits[:count]:
            rows = labels.get(int(hit["id"]), [])
            tags = " ".join(f"{row['namespace']}:{row['label']}" for row in rows)
            name = str(hit.get("filename") or "")[:70]
            samples.append(f"{name} [{hit.get('media_type')}] {tags}".strip())
        return ToolResult(
            True,
            {
                "query": query,
                "total": result.total,
                "relaxed": bool(result.get("relaxed")),
                "samples": samples,
            },
        )

    def _sample_filenames(self, media_type: str = "", limit: int = 15) -> ToolResult:
        count = max(1, min(int(limit), 40))
        clause = " WHERE a.media_type = ?" if media_type else ""
        params: list[Any] = [media_type] if media_type else []
        params.append(count)
        rows = self.engine.repos.db.query(
            "SELECT f.path FROM assets a JOIN asset_primary_file f ON f.asset_id = a.id"
            f"{clause} ORDER BY a.id DESC LIMIT ?",
            params,
        )
        # Truncated: a rules pattern keys off the shape of a name, and the
        # first 70 characters carry that. Whole names would crowd out the rest
        # of the conversation on a small-context model.
        names = [Path(str(row["path"])).name[:70] for row in rows]
        return ToolResult(True, {"media_type": media_type or "any", "filenames": names})

    def _list_filter_packs(self) -> ToolResult:
        packs = []
        for pack in self.engine.plugins.packs.values():
            state = "on" if self.engine.config.plugins.is_enabled(pack.id) else "off"
            origin = "builtin" if pack.builtin else "custom"
            packs.append(
                f"{pack.id} | {pack.namespace} | {pack.method} | {state} | {origin} | "
                + ", ".join(pack.label_names[:12])
            )
        return ToolResult(True, {"packs": packs, "errors": self.engine.plugins.pack_errors})

    def _read_filter_pack(self, pack_id: str) -> ToolResult:
        pack = self.engine.plugins.packs.get(pack_id)
        if pack is None:
            return ToolResult(
                False,
                {
                    "error": f"no pack called {pack_id}",
                    "available": sorted(self.engine.plugins.packs),
                },
            )
        if pack.source and Path(pack.source).is_file():
            text = Path(pack.source).read_text(encoding="utf-8")
            return ToolResult(True, {"pack_id": pack_id, "source": pack.source, "toml": text})
        return ToolResult(False, {"error": f"{pack_id} has no readable source file"})

    # ── write implementations: validate, stage, return the preview ──────────

    def _write_filter_pack(self, pack_id: str, toml: str, reason: str = "") -> ToolResult:
        cleaned = str(toml).strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            document = tomllib.loads(cleaned)
        except tomllib.TOMLDecodeError as exc:
            return ToolResult(
                False, {"error": f"that is not valid TOML: {exc}", "template": TEMPLATE}
            )
        try:
            pack = parse_pack(document, source=pack_id)
        except ConfigError as exc:
            # The worked example rides along with the failure rather than
            # sitting in the system prompt: it is only needed here, and here it
            # corrects far more reliably than a prose rule would.
            return ToolResult(False, {"error": str(exc), "template": TEMPLATE})
        if pack.id != pack_id:
            return ToolResult(
                False,
                {"error": f"the document declares id {pack.id!r} but you asked for {pack_id!r}"},
            )
        existing = self.engine.plugins.packs.get(pack_id)
        if existing is not None and existing.builtin:
            return ToolResult(
                False,
                {
                    "error": f"{pack_id} is a built-in pack",
                    "hint": "choose a new id rather than shadowing a shipped pack",
                },
            )
        path = self._pack_path(pack_id)
        replacing = path.is_file()

        def _write() -> dict[str, Any]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(cleaned + "\n", encoding="utf-8")
            self.engine.reload_plugins()
            return {"pack_id": pack_id, "path": str(path), "labels": len(pack.labels)}

        token = self.stage(
            PendingChange(
                kind="write_filter_pack",
                title=f"{'Replace' if replacing else 'Create'} filter “{pack.name}”",
                summary=(
                    reason
                    or f"{len(pack.labels)} values in {pack.namespace}, decided by {pack.method}"
                ),
                preview=cleaned,
                apply=_write,
            )
        )
        return ToolResult(
            True,
            {
                "staged": token,
                "status": "waiting for the user to approve",
                "pack_id": pack.id,
                "namespace": pack.namespace,
                "method": pack.method,
                "labels": [label.name for label in pack.labels],
                "path": str(path),
            },
            pending=self.pending[token],
        )

    def _set_filter_enabled(self, pack_id: str, enabled: bool) -> ToolResult:
        if pack_id not in self.engine.plugins.packs:
            return ToolResult(False, {"error": f"no pack called {pack_id}"})
        want = bool(enabled)

        def _toggle() -> dict[str, Any]:
            return dict(self._manager().set_enabled(pack_id, want, grant_network=True))

        token = self.stage(
            PendingChange(
                kind="set_filter_enabled",
                title=f"{'Add' if want else 'Remove'} the “{pack_id}” filter",
                summary="Enable this pack so it can sort your library."
                if want
                else "Stop running this pack. Existing labels are kept.",
                preview=f"{pack_id} -> {'enabled' if want else 'disabled'}",
                apply=_toggle,
            )
        )
        return ToolResult(
            True,
            {"staged": token, "status": "waiting for the user to approve"},
            pending=self.pending[token],
        )

    def _run_filter_pack(
        self, pack_id: str, media_types: Sequence[str] | None = None, limit: int = 25
    ) -> ToolResult:
        pack = self.engine.plugins.packs.get(pack_id)
        if pack is None:
            return ToolResult(False, {"error": f"no pack called {pack_id}"})
        scope = [str(value) for value in (media_types or [])]
        count = max(1, min(int(limit), 500))

        def _run() -> dict[str, Any]:
            result = self.engine.backfill(
                [pack_id], media_types=scope or None, limit=count, retry_failed=True
            )
            return dict(result.as_dict())

        token = self.stage(
            PendingChange(
                kind="run_filter_pack",
                title=f"Sort up to {count} files with “{pack.name}”",
                summary=(
                    f"{pack.method} pass over {', '.join(scope) or 'everything it accepts'}."
                    + (" Each file costs one local model call." if pack.method != "rules" else "")
                ),
                preview=f"{pack_id}  scope={scope or 'all'}  limit={count}",
                apply=_run,
            )
        )
        return ToolResult(
            True,
            {"staged": token, "status": "waiting for the user to approve"},
            pending=self.pending[token],
        )
