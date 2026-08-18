from __future__ import annotations

from typing import Any

import pytest

from mediaengine.errors import PluginUnavailable
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


# ── finding a server that moved ──────────────────────────────────────────────


def test_a_server_on_another_port_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """LM Studio reuses its last port, so 1234 goes stale on its own."""
    import socket

    from mediaengine.plugins.builtin import lm_studio

    class _Probe:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_Probe":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def settimeout(self, value: float) -> None:
            return None

        def connect_ex(self, address: tuple[str, int]) -> int:
            return 0 if address[1] == 1235 else 1

    monkeypatch.setattr(socket, "socket", _Probe)
    monkeypatch.setattr(lm_studio, "_cli_port", lambda **_kwargs: None)
    assert lm_studio.discover_base_url("http://127.0.0.1:1234") == "http://127.0.0.1:1235/v1"


def test_nothing_listening_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from mediaengine.plugins.builtin import lm_studio

    class _Closed:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_Closed":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def settimeout(self, value: float) -> None:
            return None

        def connect_ex(self, address: tuple[str, int]) -> int:
            return 1

    monkeypatch.setattr(socket, "socket", _Closed)
    monkeypatch.setattr(lm_studio, "_cli_port", lambda **_kwargs: None)
    assert lm_studio.discover_base_url("http://127.0.0.1:1234") is None


def test_the_cli_is_asked_before_ports_are_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from mediaengine.plugins.builtin import lm_studio

    asked: list[str] = []

    class _Only4321:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_Only4321":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def settimeout(self, value: float) -> None:
            return None

        def connect_ex(self, address: tuple[str, int]) -> int:
            return 0 if address[1] == 4321 else 1

    monkeypatch.setattr(socket, "socket", _Only4321)
    monkeypatch.setattr(
        lm_studio, "_cli_port", lambda **_kwargs: (asked.append("cli"), 4321)[1]
    )
    # A port nobody would have guessed is found because the CLI knows it.
    assert lm_studio.discover_base_url("http://127.0.0.1:1234") == "http://127.0.0.1:4321/v1"
    assert asked == ["cli"]


def test_a_token_requirement_is_not_reported_as_a_dead_server() -> None:
    """"Not reachable" sends someone off restarting an app that was fine."""
    import httpx

    client = LmStudioClient("http://127.0.0.1:1234")

    def refuse(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "token required"}})

    transport = httpx.MockTransport(lambda request: refuse())
    original = httpx.Client

    class _Patched(original):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: object) -> None:
            kwargs["transport"] = transport
            super().__init__(**kwargs)  # type: ignore[arg-type]

    httpx.Client = _Patched  # type: ignore[misc]
    try:
        with pytest.raises(PluginUnavailable, match="requires an API token"):
            client.models()
    finally:
        httpx.Client = original  # type: ignore[misc]
