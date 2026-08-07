from __future__ import annotations

from typing import Any

import pytest

from mediaengine.plugins.builtin import lm_studio
from mediaengine.plugins.builtin.lm_studio import LmStudioAnalyzer, LmStudioClient


class FakeContext:
    config = {
        "base_url": "http://localhost:1234",
        "model": "small-local-model",
        "structured_output": True,
        "send_image": False,
    }
    filename = "IMG_0001.jpg"
    media_type = "image"
    mime_type = "image/jpeg"
    captured_at = "2025-01-01T00:00:00Z"
    metadata = {"width": 1920, "height": 1080}
    text = None

    def annotations(self) -> list[dict[str, Any]]:
        return [{"namespace": "color.dominant", "label": "blue"}]


class FakeClient:
    last_request: dict[str, Any] | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def choose_model(self, preferred: str | None = None) -> str:
        return preferred or "auto-model"

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.last_request = payload
        FakeClient.last_request = payload
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"summary":"Blue car on a road","category":"Travel",'
                        '"tags":["car","road","car","!!!"],"confidence":0.82}'
                    }
                }
            ]
        }


def test_lm_studio_model_discovery() -> None:
    client = LmStudioClient()
    client._request_json = lambda method, path, payload=None: {  # type: ignore[method-assign]
        "data": [{"id": "qwen-local"}, {"id": "embedding-model"}]
    }
    assert client.choose_model() == "qwen-local"


def test_lm_studio_analyzer_emits_safe_searchable_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lm_studio, "LmStudioClient", FakeClient)
    output = list(LmStudioAnalyzer().analyze(FakeContext()))  # type: ignore[arg-type]
    assert [(item.namespace, item.label) for item in output] == [
        ("llm.summary", "generated"),
        ("llm.category", "travel"),
        ("llm.tag", "car"),
        ("llm.tag", "road"),
    ]
    assert output[0].value == {"text": "Blue car on a road", "model": "small-local-model"}
    assert FakeClient.last_request is not None
    assert FakeClient.last_request["response_format"]["type"] == "json_schema"
    assert FakeClient.last_request["stream"] is False
