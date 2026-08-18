"""The generic analyzer that runs any :class:`FilterPack`.

One class serves every pack. That is the point: adding "detect which sport
this is" must not mean writing an analyzer, and a pack a user writes tonight
must be indistinguishable at runtime from one that ships in the box.

Rule packs never touch the network or decode a pixel — they read the filename,
container metadata, and extracted text the scan already produced. Vision and
text packs delegate to the configured local model through the same LM Studio
client the summary analyzer uses, but constrain it to the pack's closed
vocabulary with a JSON schema, so a model cannot invent a facet value.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import PluginExecutionError, PluginUnavailable
from ..plugins.context import AnalysisContext
from ..plugins.contract import Annotation, PluginInfo
from .pack import FilterLabel, FilterPack

__all__ = ["FilterPackAnalyzer", "plugin_info_for"]

_LOG = logging.getLogger(__name__)

#: Where a rule pack reads each of its fields from.
_METADATA_FIELDS = {"container": ("format_name", "container"), "encoder": ("encoder",), "title": ("title",)}

_SYSTEM_PROMPT = (
    "You classify media for a private library. Choose only from the provided options and "
    "answer strictly in the requested JSON. Describe observable content only. Never infer "
    "identity, ethnicity, religion, health, sexuality, or other sensitive personal traits."
)


def plugin_info_for(pack: FilterPack) -> PluginInfo:
    """Build the manifest a pack presents to the registry.

    The pack's content fingerprint rides in the version, so editing a pack
    supersedes its old claims through the ordinary version-bump path instead of
    a bespoke invalidation rule.
    """
    needs_model = pack.method in {"vision", "text"}
    return PluginInfo(
        id=pack.id,
        version=pack.plugin_version,
        accepts=pack.accepts,
        emits=(pack.namespace,),
        model_id="lm-studio/openai-compatible" if needs_model else "rules",
        description=pack.description or f"{pack.name} filter pack",
        pixels=pack.method == "vision",
        frames=pack.method == "vision",
        text=pack.method != "vision",
        metadata_only=pack.method == "rules",
        network=needs_model,
        max_concurrency=1,
        namespaces={
            pack.namespace: {
                "display_name": pack.facet_title,
                "facetable": pack.facetable,
                "values": [
                    {"label": item.name, "display_name": item.title} for item in pack.labels
                ],
            }
        },
    )


class FilterPackAnalyzer:
    """Runs one pack. Constructed per pack by the filter registry."""

    def __init__(self, pack: FilterPack) -> None:
        self.pack = pack
        self.info = plugin_info_for(pack)

    # ── entry point ─────────────────────────────────────────────────────────

    def analyze(self, ctx: AnalysisContext) -> Sequence[Annotation]:
        if self.pack.method == "rules":
            return self._by_rules(ctx)
        return self._by_model(ctx)

    # ── deterministic packs ─────────────────────────────────────────────────

    def _fields(self, ctx: AnalysisContext) -> dict[str, str]:
        """The haystacks a rule may search, all already in memory."""
        path = ctx.path
        metadata = ctx.metadata
        raw: dict[str, str] = {
            "filename": ctx.filename or "",
            "path": str(path) if path is not None else "",
            "mime_type": ctx.mime_type or "",
            "text": (ctx.text or "")[:20_000],
        }
        for name, keys in _METADATA_FIELDS.items():
            value = next((metadata.get(key) for key in keys if metadata.get(key)), "")
            raw[name] = str(value or "")
        return raw

    def _by_rules(self, ctx: AnalysisContext) -> list[Annotation]:
        """Score every label by how much of its evidence is present.

        Confidence grows with the *absolute* weight of what matched, not with
        the fraction of rules that fired. Scoring by fraction would make a
        label with one loose rule maximally confident while a thorough label
        with five rules and four hits scored lower, which is backwards — and it
        would make ``conf:>=0.8`` meaningless on single-rule labels.

        The curve saturates and never reaches 1.0, because a regular
        expression over a filename is a strong hint and not a proof.
        """
        fields = self._fields(ctx)
        scored: list[tuple[float, FilterLabel, list[str]]] = []
        for label in self.pack.labels:
            hit_weight = 0.0
            evidence: list[str] = []
            for rule in label.rules:
                haystack = fields.get(rule.field, "")
                if not haystack:
                    continue
                found = rule.compiled().search(haystack)
                if found is not None:
                    hit_weight += max(0.0, rule.weight)
                    evidence.append(f"{rule.field}:{found.group(0)[:60]}")
            if hit_weight <= 0:
                continue
            confidence = min(0.99, 1.0 - 0.5**hit_weight)
            scored.append((confidence, label, evidence))
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        chosen = scored if self.pack.multi_label else scored[:1]
        return [
            Annotation(
                namespace=self.pack.namespace,
                label=label.name,
                value={"evidence": evidence[:4], "method": "rules"},
                confidence=round(confidence, 3),
            )
            for confidence, label, evidence in chosen
            if confidence >= self.pack.threshold
        ]

    # ── model-backed packs ──────────────────────────────────────────────────

    def _client(self, config: Mapping[str, Any]) -> Any:
        from ..plugins.builtin.lm_studio import LmStudioClient

        return LmStudioClient(
            str(config.get("base_url") or "http://127.0.0.1:1234"),
            api_token=str(config["api_token"]) if config.get("api_token") else None,
            timeout_s=float(config.get("timeout_s", 120.0)),
        )

    def _by_model(self, ctx: AnalysisContext) -> list[Annotation]:
        config = ctx.config
        client = self._client(config)
        model = client.choose_model(str(config["model"]) if config.get("model") else None)
        frames = self._frames(ctx, config) if self.pack.method == "vision" else []
        if self.pack.method == "vision" and not frames:
            raise PluginUnavailable(
                f"{self.pack.name} needs a preview of {ctx.filename or 'this asset'}; "
                "refresh the library so Studio can build thumbnails first",
                plugin_id=self.pack.id,
            )
        response = client.chat(
            {
                "model": model,
                "messages": self._messages(ctx, config, frames),
                "temperature": float(config.get("temperature", 0.0)),
                # Generous for a one-word answer, because a reasoning model
                # spends most of this thinking before it says anything.
                "max_tokens": int(config.get("max_tokens", 1200)),
                "stream": False,
                "response_format": self._schema(),
            }
        )
        return self._annotations(self._parse(response), model)

    def _frames(self, ctx: AnalysisContext, config: Mapping[str, Any]) -> list[str]:
        from ..plugins.builtin.lm_studio import LmStudioAnalyzer

        # Classification needs fewer frames than description: the question is
        # "which of these N things is it", not "what happens in this clip".
        budget = dict(config)
        budget.setdefault("frame_count", 3)
        return [encoded for _, encoded in LmStudioAnalyzer._frames(ctx, budget)]

    def _options(self) -> str:
        lines = []
        for label in self.pack.labels:
            hint = f" — {label.hint}" if label.hint else ""
            lines.append(f"- {label.name}{hint}")
        return "\n".join(lines)

    def _messages(
        self, ctx: AnalysisContext, config: Mapping[str, Any], frames: Sequence[str]
    ) -> list[dict[str, Any]]:
        limit = max(0, min(int(config.get("max_text_chars", 6_000)), 60_000))
        facts = {
            "filename": ctx.filename,
            "media_type": ctx.media_type,
            "duration_s": ctx.metadata.get("duration_s"),
            "extracted_text": (ctx.text or "")[:limit] if self.pack.method == "text" else None,
        }
        count = "one or more options that clearly apply" if self.pack.multi_label else "exactly one option"
        instruction = (
            f"Task: {self.pack.description or self.pack.name}.\n"
            f"Choose {count} from this closed list. If none genuinely applies, answer "
            f'"unknown" with a low confidence rather than guessing.\n\n'
            f"Options:\n{self._options()}\n\n"
            f"Context: {json.dumps(facts, ensure_ascii=False, default=str)}"
        )
        if not frames:
            return [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": instruction},
            ]
        parts: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
        for encoded in frames:
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
            )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": parts},
        ]

    def _schema(self) -> dict[str, Any]:
        """Constrain the model to the pack's vocabulary.

        The enum here is what makes a pack a *filter* rather than a tag cloud:
        the model cannot answer "association football" when the facet value is
        ``football``, so every claim is guaranteed to be a value the facet
        panel can actually offer.
        """
        choice: dict[str, Any] = {
            "type": "object",
            "properties": {
                "label": {"type": "string", "enum": [*self.pack.label_names, "unknown"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["label", "confidence"],
            "additionalProperties": False,
        }
        if self.pack.multi_label:
            schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "choices": {"type": "array", "items": choice, "minItems": 1, "maxItems": 4}
                },
                "required": ["choices"],
                "additionalProperties": False,
            }
        else:
            schema = choice
        return {
            "type": "json_schema",
            "json_schema": {
                "name": re.sub(r"[^a-zA-Z0-9_]", "_", self.pack.id),
                "strict": True,
                "schema": schema,
            },
        }

    def _parse(self, response: Mapping[str, Any]) -> dict[str, Any]:
        from ..plugins.builtin.lm_studio import message_text

        content = message_text(response, plugin_id=self.pack.id)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # Reasoning models narrate around the answer; the JSON object is
            # still in there, so take the last complete one rather than
            # failing an asset over a preamble.
            found = re.findall(r"\{[^{}]*\}", cleaned, re.DOTALL)
            if not found:
                raise PluginExecutionError(
                    f"{self.pack.name} did not return valid JSON", plugin_id=self.pack.id
                ) from exc
            try:
                value = json.loads(found[-1])
            except json.JSONDecodeError:
                raise PluginExecutionError(
                    f"{self.pack.name} did not return valid JSON", plugin_id=self.pack.id
                ) from exc
        if not isinstance(value, dict):
            raise PluginExecutionError(
                f"{self.pack.name} returned {type(value).__name__}, expected an object",
                plugin_id=self.pack.id,
            )
        return value

    def _annotations(self, payload: Mapping[str, Any], model: str) -> list[Annotation]:
        raw = payload.get("choices") if self.pack.multi_label else [payload]
        if not isinstance(raw, list):
            raw = [payload]
        out: list[Annotation] = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("label") or "").strip().lower()
            if name in self.pack.reject_labels or name in seen:
                continue
            if self.pack.label(name) is None:
                _LOG.debug("%s: model returned unknown label %r", self.pack.id, name)
                continue
            try:
                confidence = max(0.0, min(1.0, float(entry.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            if confidence < self.pack.threshold:
                continue
            seen.add(name)
            out.append(
                Annotation(
                    namespace=self.pack.namespace,
                    label=name,
                    value={"model": model, "method": self.pack.method},
                    confidence=confidence,
                )
            )
        return out
