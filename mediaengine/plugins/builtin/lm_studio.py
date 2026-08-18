"""LM Studio bridge for local, configurable LLM/VLM enrichment.

The adapter uses LM Studio's OpenAI-compatible endpoints and returns ordinary
producer-attributed annotations. It cannot mutate files, identities, or user
labels. Operators explicitly enable the network capability even though the
default endpoint is loopback.

For video the analyzer sends *keyframes*, not just the filename. A model that
only reads ``birthday_2019.mp4`` is guessing; a model that sees four frames
spread across the clip is describing. Frames come from derivatives the scan
already produced, so enabling this costs inference time and no re-decoding.
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...errors import PluginExecutionError, PluginUnavailable
from ..contract import Annotation, PluginInfo
from ..context import AnalysisContext

if TYPE_CHECKING:
    from PIL.Image import Image

__all__ = ["LmStudioAnalyzer", "LmStudioClient", "message_text", "strip_reasoning"]

PLUGIN_ID = "local.lm-studio"
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_SYSTEM_PROMPT = (
    "You organize a private media library. Describe only observable content and supplied "
    "metadata. Never infer identity, ethnicity, religion, health, sexuality, guilt, or other "
    "sensitive traits. Return concise JSON matching the requested schema."
)
#: Media types whose pixels are worth sending when a vision model is selected.
VISUAL_TYPES = frozenset({"image", "video"})
DEFAULT_FRAME_COUNT = 4
MAX_FRAME_COUNT = 8
_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9 _.,+&'()/-]+")


_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _base_url(value: object) -> str:
    base = str(value or DEFAULT_BASE_URL).rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def strip_reasoning(text: str) -> str:
    """Remove inline ``<think>`` blocks some models emit around their answer."""
    return _THINK_BLOCK.sub("", text).strip()


def message_text(response: Mapping[str, Any], *, plugin_id: str) -> str:
    """The assistant's answer, wherever this particular model chose to put it.

    Reasoning models — Qwen3 with thinking on, DeepSeek-R1, gpt-oss, and
    friends — return an empty ``content`` and put everything, including the
    final JSON, in ``reasoning_content``. Treating that as "no reply" fails
    every single asset against an otherwise perfectly good local model, so the
    fallback is not a nicety.
    """
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PluginExecutionError("LM Studio response has no choices", plugin_id=plugin_id)
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    finish = str(first.get("finish_reason") or "") if isinstance(first, Mapping) else ""
    for field in ("content", "reasoning_content"):
        value = message.get(field) if isinstance(message, Mapping) else None
        if isinstance(value, str) and strip_reasoning(value):
            return strip_reasoning(value)
    if finish == "length":
        # Three different causes share this finish reason — a long answer, a
        # reasoning model narrating, and a context window with no room left —
        # and guessing between them produces confidently wrong advice. Report
        # the numbers and name the remedies.
        usage = response.get("usage")
        prompt_tokens = completion_tokens = 0
        if isinstance(usage, Mapping):
            try:
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                prompt_tokens = completion_tokens = 0
        raise PluginExecutionError(
            f"the reply was cut off before it finished ({prompt_tokens} prompt tokens, "
            f"{completion_tokens} generated). Either the answer needed more room than "
            "max_tokens allows, or the model's context window is too small — reload it in "
            "LM Studio with a larger context length, 16384 or more",
            plugin_id=plugin_id,
        )
    raise PluginExecutionError(
        "LM Studio response has no message content", plugin_id=plugin_id
    )


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
        """Resolve the model id to send, validating a configured one first.

        A configured id that the server does not serve is the single most
        common way this integration silently does nothing, so it is caught
        here — with the list of ids that *would* have worked — instead of
        surfacing later as an opaque HTTP 400 on every asset in the library.
        """
        available = [
            str(row["id"])
            for row in self.models()
            if isinstance(row.get("id"), str) and row["id"]
        ]
        if preferred:
            if not available or preferred in available:
                return preferred
            offered = ", ".join(available[:6]) or "none"
            raise PluginUnavailable(
                f"LM Studio is not serving a model called {preferred!r}. "
                f"Currently loaded: {offered}. Pick one in Studio's Model Store, "
                "or clear the model setting to use whatever is loaded.",
                plugin_id=PLUGIN_ID,
            )
        if not available:
            raise PluginUnavailable(
                "LM Studio has no model loaded. Open Studio's Model Store and choose "
                "'Use for tagging', or load a model in LM Studio's Developer tab.",
                plugin_id=PLUGIN_ID,
            )
        return available[0]

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", "/chat/completions", payload)


class LmStudioAnalyzer:
    """Generate safe summaries, categories, and tags through a local model."""

    info = PluginInfo(
        id=PLUGIN_ID,
        # 1.1.0 sends pixels for images and keyframes for video. The version
        # bump is what supersedes the filename-only claims made by 1.0.0.
        version="1.1.0",
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
        # A vision request that carried no pixels is worth reporting rather
        # than answering from the filename: silent text-only fallback is what
        # made earlier runs look busy while producing nothing useful.
        if self._wants_pixels(ctx, config) and isinstance(messages[-1]["content"], str):
            raise PluginUnavailable(
                f"no preview frames are available for {ctx.filename or ctx.media_type}; "
                "run a library refresh so Studio can build thumbnails first",
                plugin_id=PLUGIN_ID,
            )
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
    def _wants_pixels(ctx: AnalysisContext, config: Mapping[str, Any]) -> bool:
        """Whether this asset should be sent as imagery.

        ``send_image`` defaults to on: the overwhelmingly common reason to
        point a library at a local VLM is to have it look at the library.
        """
        return bool(config.get("send_image", True)) and ctx.media_type in VISUAL_TYPES

    @staticmethod
    def _encode(image: "Image", quality: int) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=max(40, min(95, quality)))
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    @classmethod
    def _frames(cls, ctx: AnalysisContext, config: Mapping[str, Any]) -> list[tuple[str, str]]:
        """``(caption, data-uri)`` pairs describing what the model should see.

        Video frames are sampled evenly across the whole clip rather than taken
        from the front, because the first seconds of a recording are usually a
        title card, a black frame, or a desktop before anything happens.
        """
        size = max(128, min(int(config.get("frame_pixels", 512)), 1536))
        quality = int(config.get("frame_quality", 85))
        if ctx.media_type == "image":
            image = ctx.image(prefer_size=size)
            return [("the photo", cls._encode(image, quality))] if image is not None else []

        wanted = max(1, min(int(config.get("frame_count", DEFAULT_FRAME_COUNT)), MAX_FRAME_COUNT))
        keyframes = ctx.keyframes()
        chosen: list[tuple[float, Path]] = []
        if keyframes:
            if len(keyframes) <= wanted:
                chosen = list(keyframes)
            else:
                step = (len(keyframes) - 1) / (wanted - 1) if wanted > 1 else 0.0
                seen: set[int] = set()
                for index in range(wanted):
                    position = round(index * step) if wanted > 1 else len(keyframes) // 2
                    if position not in seen:
                        seen.add(position)
                        chosen.append(keyframes[position])
        frames: list[tuple[str, str]] = []
        for moment, path in chosen:
            encoded = cls._encode_path(path, size, quality)
            if encoded:
                frames.append((f"frame at {moment:.0f}s", encoded))
        if frames:
            return frames
        image = ctx.image(prefer_size=size)
        return [("the cover frame", cls._encode(image, quality))] if image is not None else []

    @classmethod
    def _encode_path(cls, path: Path, size: int, quality: int) -> str | None:
        from PIL import Image as PILImage

        try:
            with PILImage.open(path) as opened:
                frame = opened.convert("RGB")
                frame.thumbnail((size, size))
                return cls._encode(frame, quality)
        except Exception:  # noqa: BLE001 - an unreadable frame is not fatal
            return None

    @classmethod
    def _messages(cls, ctx: AnalysisContext, config: Mapping[str, Any]) -> list[dict[str, Any]]:
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
        frames = cls._frames(ctx, config) if cls._wants_pixels(ctx, config) else []
        if frames and ctx.media_type == "video":
            lead = (
                f"These {len(frames)} images are keyframes sampled across one video, in order. "
                "Describe the video as a whole, not each frame separately. "
            )
        elif frames:
            lead = "Describe what is visible in this image. "
        else:
            lead = "No preview is available, so rely on the metadata below. "
        instruction = (
            lead
            + "Create one useful summary, one broad category, and up to 12 short search tags. "
            # Without this, a vision model dutifully turns the metadata block
            # into tags — "h264", "aac audio", "157 seconds" — which are
            # already structured columns and crowd out the subject matter that
            # only a model looking at the frames could have supplied.
            "Tags must describe subject matter: what is shown, who or what is in it, "
            "where it happens, and what kind of thing it is. Never tag technical "
            "details such as codec, container, resolution, aspect ratio, duration, "
            "file size, bitrate, colour statistics, or file extension. "
            "Do not fabricate facts. The metadata below is context, not tag material:\n"
            + json.dumps(facts, ensure_ascii=False, default=str)
        )
        user_content: str | list[dict[str, Any]] = instruction
        if frames:
            parts: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
            for caption, encoded in frames:
                parts.append({"type": "text", "text": caption})
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                    }
                )
            user_content = parts
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
        return message_text(response, plugin_id=PLUGIN_ID)

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
