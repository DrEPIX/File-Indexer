"""The model inventory, downloader, and the video path of the LM Studio analyzer.

These cover the failures that made the integration look busy but useless: a
configured model id the server does not serve, a video sent as a filename
instead of frames, and a whole-library sweep started by a single button.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mediaengine.errors import PluginExecutionError, PluginUnavailable
from mediaengine.plugins.builtin.lm_studio import LmStudioAnalyzer, LmStudioClient
from mediaengine.plugins.models import CATALOG, InstalledModel, ModelLibrary, parse_progress

# ── download progress parsing ────────────────────────────────────────────────

#: One frame of the real `lms get` redraw, including its spinner and colours.
_CLI_FRAME = (
    "\x1b[?25l\x1b[0K   ⇣ To download: Qwen3 VL 4B Instruct Q4_K_M [GGUF] - 3.33 GB"
    "\x1b[0K\n⣷ Downloading... 1.40 GB / 3.33 GB\x1b[0K"
)


def test_progress_survives_terminal_redraw_frames() -> None:
    fraction, text = parse_progress(_CLI_FRAME)
    assert fraction == pytest.approx(1.40 / 3.33, rel=0.02)
    assert "\x1b" not in text
    assert "Downloading" in text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Downloading 47%", 0.47),
        ("  512 MB / 1024 MB  ", 0.5),
        ("2.0 GB / 4.0 GB", 0.5),
        ("100%", 1.0),
        ("999%", 1.0),
    ],
)
def test_progress_extracts_a_fraction(line: str, expected: float) -> None:
    assert parse_progress(line)[0] == pytest.approx(expected, rel=0.01)


def test_progress_is_indeterminate_when_the_cli_says_nothing_numeric() -> None:
    fraction, text = parse_progress("Resolving download plan...")
    assert fraction is None
    assert text == "Resolving download plan..."


# ── inventory and the merged store view ──────────────────────────────────────


class _FakeLibrary(ModelLibrary):
    """A ModelLibrary whose CLI answers are supplied by the test."""

    def __init__(self, listing: list[dict[str, Any]], loaded: set[str] | None = None) -> None:
        super().__init__(cli_path=Path(__file__))
        self._listing = listing
        self._loaded = loaded or set()

    def _run_cli(self, args: Any, *, timeout_s: float | None = None) -> str:
        del timeout_s
        if args[0] == "ls":
            return json.dumps(self._listing)
        if args[0] == "ps":
            return json.dumps([r for r in self._listing if r.get("modelKey") in self._loaded])
        raise AssertionError(f"unexpected CLI call: {args}")

    def served_ids(self) -> list[str]:
        return sorted(self._loaded)


def _row(key: str, *, vision: bool, size: int = 1000, kind: str = "llm") -> dict[str, Any]:
    return {
        "type": kind,
        "modelKey": key,
        "displayName": key.title(),
        "publisher": "tester",
        "sizeBytes": size,
        "architecture": "qwen3vl" if vision else "llama",
        "paramsString": "8B",
        "quantization": {"name": "Q4_K_M", "bits": 4},
        "vision": vision,
    }


def test_installed_models_are_parsed_from_the_cli() -> None:
    library = _FakeLibrary([_row("seer", vision=True), _row("scribe", vision=False)], {"seer"})
    installed = library.installed()
    assert [model.key for model in installed] == ["seer", "scribe"]
    assert installed[0].vision and installed[0].loaded
    assert not installed[1].vision and not installed[1].loaded


def test_store_merges_installed_models_with_the_download_catalog() -> None:
    library = _FakeLibrary([_row("seer", vision=True)])
    rows = library.store()
    keys = [row["key"] for row in rows]
    assert "seer" in keys
    assert all(entry.key in keys for entry in CATALOG)
    installed = {row["key"]: row["installed"] for row in rows}
    assert installed["seer"] is True
    assert installed[CATALOG[0].key] is False


def test_store_can_hide_models_that_cannot_see_video() -> None:
    library = _FakeLibrary([_row("scribe", vision=False), _row("seer", vision=True)])
    rows = library.store(vision_only=True)
    assert [row["key"] for row in rows if row["installed"]] == ["seer"]
    assert all(row["vision"] for row in rows)


def test_store_lists_vision_models_first() -> None:
    library = _FakeLibrary([_row("scribe", vision=False), _row("seer", vision=True)])
    rows = library.store()
    assert rows[0]["vision"] is True


def test_embedding_models_are_never_offered_for_tagging() -> None:
    library = _FakeLibrary([_row("embed", vision=False, kind="embedding")])
    assert "embed" not in [row["key"] for row in library.store()]


def test_recommended_vision_model_prefers_a_loaded_then_smaller_model() -> None:
    library = _FakeLibrary(
        [
            _row("big", vision=True, size=9_000),
            _row("small", vision=True, size=1_000),
            _row("text", vision=False, size=10),
        ]
    )
    assert library.recommended_vision_key() == "small"
    library._loaded = {"big"}
    assert library.recommended_vision_key() == "big"


def test_no_vision_model_installed_is_reported_as_none() -> None:
    assert _FakeLibrary([_row("text", vision=False)]).recommended_vision_key() is None


def test_install_without_a_cli_explains_itself() -> None:
    library = ModelLibrary(cli_path=None)
    library.cli_path = None
    with pytest.raises(PluginUnavailable, match="command line tool"):
        library.install("qwen/qwen3-vl-4b")


def test_install_rejects_an_empty_selection() -> None:
    with pytest.raises(PluginExecutionError):
        _FakeLibrary([]).install("   ")


def test_catalog_entries_are_complete_enough_to_render() -> None:
    for entry in CATALOG:
        assert "/" in entry.key, entry.key
        assert entry.summary.endswith(".")
        assert entry.approx_bytes > 0 and entry.vram_gb > 0
        assert entry.best_for
    assert any(entry.vision for entry in CATALOG)
    assert any(not entry.vision for entry in CATALOG)


def test_installed_model_dict_round_trips_for_the_ui() -> None:
    model = InstalledModel(
        key="k", display_name="K", publisher="p", size_bytes=5, architecture="a",
        parameters="8B", quantization="Q4", vision=True, kind="llm", loaded=True,
    )
    payload = model.as_dict()
    assert payload["installed"] is True and payload["loaded"] is True and payload["vision"] is True


# ── model selection ──────────────────────────────────────────────────────────


class _FakeClient(LmStudioClient):
    def __init__(self, ids: list[str]) -> None:
        super().__init__()
        self._ids = ids

    def models(self) -> list[dict[str, Any]]:
        return [{"id": value} for value in self._ids]


def test_a_configured_model_the_server_does_not_serve_is_reported() -> None:
    """The exact failure behind 113 'No models loaded' errors in the wild."""
    client = _FakeClient(["qwen3-vl-8b"])
    with pytest.raises(PluginUnavailable) as caught:
        client.choose_model("Qwen 3 Abl.")
    message = str(caught.value)
    assert "Qwen 3 Abl." in message
    assert "qwen3-vl-8b" in message


def test_a_configured_model_that_is_served_is_used() -> None:
    assert _FakeClient(["a", "b"]).choose_model("b") == "b"


def test_no_configured_model_falls_back_to_whatever_is_loaded() -> None:
    assert _FakeClient(["a", "b"]).choose_model(None) == "a"


def test_an_empty_server_names_the_fix() -> None:
    with pytest.raises(PluginUnavailable, match="Model Store"):
        _FakeClient([]).choose_model(None)


def test_a_configured_model_is_trusted_when_the_server_cannot_be_listed() -> None:
    """A listing failure must not block work the analyzer could still do."""
    assert _FakeClient([]).choose_model("mine") == "mine"


# ── video framing ────────────────────────────────────────────────────────────


class _FakeContext:
    """The slice of AnalysisContext the analyzer's message builder reads."""

    def __init__(self, media_type: str, frames: list[tuple[float, Path]], image: Any = None) -> None:
        self.media_type = media_type
        self.filename = f"clip.{media_type}"
        self.mime_type = "video/mp4"
        self.captured_at = None
        self.metadata: dict[str, Any] = {}
        self.text: str | None = None
        self._frames = frames
        self._image = image

    def annotations(self, namespace: str | None = None) -> list[dict[str, Any]]:
        del namespace
        return []

    def keyframes(self, *, every: float | None = None) -> list[tuple[float, Path]]:
        del every
        return self._frames

    def image(self, *, prefer_size: int | None = 512) -> Any:
        del prefer_size
        return self._image


def _jpeg(path: Path, colour: tuple[int, int, int]) -> Path:
    from PIL import Image

    Image.new("RGB", (64, 48), colour).save(path, format="JPEG")
    return path


def _images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content = messages[-1]["content"]
    if isinstance(content, str):
        return []
    return [part for part in content if part.get("type") == "image_url"]


def test_video_keyframes_are_sent_to_the_model(tmp_path: Path) -> None:
    frames = [(float(i), _jpeg(tmp_path / f"f{i}.jpg", (i * 20, 40, 90))) for i in range(6)]
    ctx = _FakeContext("video", frames)
    messages = LmStudioAnalyzer._messages(ctx, {"send_image": True, "frame_count": 4})
    images = _images(messages)
    assert len(images) == 4
    assert all(part["image_url"]["url"].startswith("data:image/jpeg;base64,") for part in images)
    assert "keyframes sampled across one video" in messages[-1]["content"][0]["text"]


def test_keyframes_are_sampled_across_the_whole_clip(tmp_path: Path) -> None:
    """Front-loaded sampling would describe the title card, not the video."""
    frames = [(float(i), _jpeg(tmp_path / f"f{i}.jpg", (i, i, i))) for i in range(9)]
    ctx = _FakeContext("video", frames)
    messages = LmStudioAnalyzer._messages(ctx, {"send_image": True, "frame_count": 3})
    captions = [
        part["text"]
        for part in messages[-1]["content"]
        if part.get("type") == "text" and part["text"].startswith("frame at")
    ]
    assert captions == ["frame at 0s", "frame at 4s", "frame at 8s"]


def test_frame_count_is_clamped_to_the_supported_range(tmp_path: Path) -> None:
    frames = [(float(i), _jpeg(tmp_path / f"f{i}.jpg", (i, i, i))) for i in range(40)]
    ctx = _FakeContext("video", frames)
    assert len(_images(LmStudioAnalyzer._messages(ctx, {"frame_count": 99}))) == 8
    assert len(_images(LmStudioAnalyzer._messages(ctx, {"frame_count": 0}))) == 1


def test_a_video_with_no_keyframes_falls_back_to_its_thumbnail(tmp_path: Path) -> None:
    from PIL import Image

    ctx = _FakeContext("video", [], image=Image.new("RGB", (32, 32), (10, 20, 30)))
    messages = LmStudioAnalyzer._messages(ctx, {"send_image": True})
    assert len(_images(messages)) == 1
    assert "cover frame" in str(messages[-1]["content"])


def test_a_video_with_no_pixels_at_all_says_so_rather_than_guessing(tmp_path: Path) -> None:
    ctx = _FakeContext("video", [])
    messages = LmStudioAnalyzer._messages(ctx, {"send_image": True})
    assert isinstance(messages[-1]["content"], str)
    assert "No preview is available" in messages[-1]["content"]


def test_unreadable_keyframes_are_skipped_not_fatal(tmp_path: Path) -> None:
    good = _jpeg(tmp_path / "good.jpg", (1, 2, 3))
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    ctx = _FakeContext("video", [(0.0, broken), (5.0, good)])
    assert len(_images(LmStudioAnalyzer._messages(ctx, {"frame_count": 2}))) == 1


def test_send_image_off_keeps_the_request_text_only(tmp_path: Path) -> None:
    frames = [(0.0, _jpeg(tmp_path / "f.jpg", (1, 2, 3)))]
    ctx = _FakeContext("video", frames)
    messages = LmStudioAnalyzer._messages(ctx, {"send_image": False})
    assert isinstance(messages[-1]["content"], str)


def test_pixels_are_sent_by_default(tmp_path: Path) -> None:
    """The reason to run a local VLM is to have it look at the library."""
    frames = [(0.0, _jpeg(tmp_path / "f.jpg", (1, 2, 3)))]
    assert LmStudioAnalyzer._wants_pixels(_FakeContext("video", frames), {})
    assert not LmStudioAnalyzer._wants_pixels(_FakeContext("document", []), {})


def test_analyzer_version_advertises_the_vision_upgrade() -> None:
    """A version bump is what supersedes filename-only claims from 1.0.0."""
    assert LmStudioAnalyzer.info.version != "1.0.0"
    assert "video" in LmStudioAnalyzer.info.accepts
