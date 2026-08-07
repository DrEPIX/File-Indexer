"""LM Studio bridge for local, configurable LLM/VLM enrichment.

The adapter uses LM Studio's OpenAI-compatible endpoints and returns ordinary
producer-attributed annotations. It cannot mutate files, identities, or user
labels. Operators explicitly enable the network capability even though the
default endpoint is loopback.
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ...errors import PluginExecutionError, PluginUnavailable
from ..contract import Annotation, PluginInfo
from ..context import AnalysisContext

__all__ = ["LmStudioAnalyzer", "LmStudioClient"]

PLUGIN_ID = "local.lm-studio"
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_SYSTEM_PROMPT = (
    "You organize a private media library. Describe only observable content and supplied "
    "metadata. Never infer identity, ethnicity, religion, health, sexuality, guilt, or other "
    "sensitive traits. Return concise JSON matching the requested schema."
)
_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9 _.,+&'()/-]+")


def _base_url(value: object) -> str:
    base = str(value or DEFAULT_BASE_URL).rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


class LmStudioClient:
    """Small dependency-light client shared by the analyzer and setup UI."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_token: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.base_url = _base_url(base_url)
        self.api_token = api_token or None
        self.timeout_s = timeout_s

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:
            raise PluginExecutionError(
                "LM Studio integration requires the 'remote' optional dependency",
                plugin_id=PLUGIN_ID,
            ) from exc
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            with httpx.Client(timeout=self.timeout_s, headers=headers) as client:
                response = client.request(
                    method, f"{self.base_url}{path}", json=payload
                )
            response.raise_for_status()
            value = response.json()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PluginUnavailable(
                f"LM Studio is not reachable at {self.base_url}: {exc}",
                plugin_id=PLUGIN_ID,
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise PluginExecutionError(
                f"LM Studio returned HTTP {exc.response.status_code}: {detail}",
                plugin_id=PLUGIN_ID,
            ) from exc
        except (ValueError, TypeError) as exc:
            raise PluginExecutionError(
                "LM Studio returned invalid JSON", plugin_id=PLUGIN_ID
            ) from exc
        if not isinstance(value, dict):
            raise PluginExecutionError(
                "LM Studio response must be a JSON object", plugin_id=PLUGIN_ID
            )
        return value

    def models(self) -> list[dict[str, Any]]:
        payload = self._request_json("GET", "/models")
        rows = payload.get("data")
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def choose_model(self, preferred: str | None = None) -> str:
        if preferred:
            return preferred
        models = self.models()
        if not models:
            raise PluginUnavailable(
                "LM Studio has no available model; load one in its Developer tab",
                plugin_id=PLUGIN_ID,
            )
        model_id = models[0].get("id")
        if not isinstance(model_id, str) or not model_id:
            raise PluginExecutionError(
                "LM Studio model list has no usable id", plugin_id=PLUGIN_ID
            )
        return model_id

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", "/chat/completions", payload)


class LmStudioAnalyzer:
    """Generate safe summaries, categories, and tags through a local model."""

    info = PluginInfo(
        id=PLUGIN_ID,
        version="1.0.0",
        accepts=("image", "video", "audio", "document", "other"),
        emits=("llm.summary", "llm.category", "llm.tag"),
        model_id="lm-studio/openai-compatible",
        description="Local LM Studio summaries, categories, and tags with optional vision.",
        pixels=True,
        text=True,
        network=True,
        max_concurrency=1,
        namespaces={
            "llm.summary": {
                "display_name": "AI summary",
                "value_type": "text",
                "facetable": False,
            },
            "llm.category": {"display_name": "AI category", "facetable": True},
            "llm.tag": {"display_name": "AI tag", "facetable": True},
        },
    )

    def analyze(self, ctx: AnalysisContext) -> Sequence[Annotation]:
        config = ctx.config
        client = LmStudioClient(
            _base_url(config.get("base_url")),
            api_token=str(config["api_token"]) if config.get("api_token") else None,
            timeout_s=float(config.get("timeout_s", 120.0)),
        )
        model = client.choose_model(str(config["model"]) if config.get("model") else None)
        messages = self._messages(ctx, config)
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": float(config.get("temperature", 0.1)),
            "max_tokens": int(config.get("max_tokens", 700)),
            "stream": False,
        }
        if bool(config.get("structured_output", True)):
            request["response_format"] = self._response_format()
        response = client.chat(request)
        content = self._content(response)
        parsed = self._parse_json(content)
        return self._annotations(parsed, model)

    @staticmethod
    def _messages(ctx: AnalysisContext, config: Mapping[str, Any]) -> list[dict[str, Any]]:
        maximum = max(0, min(int(config.get("max_text_chars", 12_000)), 100_000))
        existing = [
            f"{item.get('namespace')}:{item.get('label')}"
            for item in ctx.annotations()
            if item.get("namespace") and item.get("label")
        ][:50]
        facts = {
            "filename": ctx.filename,
            "media_type": ctx.media_type,
            "mime_type": ctx.mime_type,
            "captured_at": ctx.captured_at,
            "technical_metadata": ctx.metadata,
            "existing_annotations": existing,
            "extracted_text": (ctx.text or "")[:maximum],
        }
        instruction = (
            "Create one useful summary, one broad category, and up to 12 short search tags. "
            "Do not repeat filename extensions or fabricate facts. Input:\n"
            + json.dumps(facts, ensure_ascii=False, default=str)
        )
        user_content: str | list[dict[str, Any]] = instruction
        if bool(config.get("send_image", False)) and ctx.media_type == "image":
            image = ctx.image(prefer_size=512)
            if image is not None:
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=85)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                user_content = [
                    {"type": "text", "text": instruction},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                    },
                ]
        return [
            {
                "role": "system",
                "content": str(config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
            },
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _response_format() -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "media_library_enrichment",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "maxLength": 1200},
                        "category": {"type": "string", "maxLength": 80},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 80},
                            "maxItems": 12,
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["summary", "category", "tags", "confidence"],
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _content(response: Mapping[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PluginExecutionError(
                "LM Studio response has no choices", plugin_id=PLUGIN_ID
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise PluginExecutionError(
                "LM Studio response has no message content", plugin_id=PLUGIN_ID
            )
        return content.strip()

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        cleaned = content
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise PluginExecutionError(
                "LM Studio did not return valid structured JSON", plugin_id=PLUGIN_ID
            ) from exc
        if not isinstance(value, dict):
            raise PluginExecutionError(
                "LM Studio structured output must be an object", plugin_id=PLUGIN_ID
            )
        return value

    @staticmethod
    def _clean_label(value: object, fallback: str) -> str:
        cleaned = _SAFE_LABEL.sub("", str(value)).strip().lower()
        return (cleaned or fallback)[:80]

    @classmethod
    def _annotations(cls, value: Mapping[str, Any], model: str) -> list[Annotation]:
        summary = str(value.get("summary") or "").strip()[:1200]
        category = cls._clean_label(value.get("category"), "uncategorized")
        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        output = [
            Annotation(
                namespace="llm.summary",
                label="generated",
                value={"text": summary, "model": model},
                confidence=confidence,
            ),
            Annotation(
                namespace="llm.category", label=category, confidence=confidence
            ),
        ]
        tags = value.get("tags")
        seen: set[str] = set()
        if isinstance(tags, list):
            for raw in tags[:12]:
                label = cls._clean_label(raw, "")
                if not label or label in seen:
                    continue
                seen.add(label)
                output.append(
                    Annotation(namespace="llm.tag", label=label, confidence=confidence)
                )
        return output
