"""The local ONNX tagger: real inference, and timestamps on video claims.

The end-to-end tests build an actual ONNX model rather than mocking the
session, because the parts most likely to be wrong — tensor layout, label
alignment, which activation to apply — are exactly the parts a mock would
paper over.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mediaengine.errors import DependencyUnsatisfied
from mediaengine.models import CATALOG, catalog_for, entry, tasks
from mediaengine.plugins.builtin.local_tagger import (
    DEFAULT_NAMESPACE,
    LocalTaggerAnalyzer,
    _activate,
    _rank_providers,
    load_labels,
)

onnx = pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")


LABELS = ["beach", "sunset", "dog", "indoors"]


def _build_model(path: Path, *, labels: int = 4, channels_first: bool = True) -> Path:
    """A real ONNX model: global-average-pool the image, project to labels.

    Deterministic and tiny, but structurally identical to a classifier — same
    input rank, same output shape — so everything the analyzer infers from a
    model's signature is exercised for real.
    """
    from onnx import TensorProto, helper, numpy_helper

    shape = [1, 3, 8, 8] if channels_first else [1, 8, 8, 3]
    axes = [2, 3] if channels_first else [1, 2]
    weight = np.eye(3, labels, dtype=np.float32) * 8.0

    graph = helper.make_graph(
        [
            helper.make_node("ReduceMean", ["image", "axes"], ["pooled"], keepdims=0),
            helper.make_node("MatMul", ["pooled", "weight"], ["scores"]),
        ],
        "tagger",
        [helper.make_tensor_value_info("image", TensorProto.FLOAT, shape)],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [1, labels])],
        [
            numpy_helper.from_array(np.array(axes, dtype=np.int64), "axes"),
            numpy_helper.from_array(weight, "weight"),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)])
    model.ir_version = 9
    onnx.save(model, str(path))
    return path


class _Ctx:
    """The slice of AnalysisContext this analyzer reads."""

    def __init__(
        self,
        media_type: str,
        *,
        config: dict[str, Any],
        image: Any = None,
        keyframes: list[tuple[float, Path]] | None = None,
    ) -> None:
        self.media_type = media_type
        self.config = config
        self._image = image
        self._keyframes = keyframes or []

    def image(self, *, prefer_size: int = 512) -> Any:
        return self._image

    def keyframes(self, *, every: float | None = None) -> list[tuple[float, Path]]:
        return self._keyframes


def _frame(tmp_path: Path, name: str, colour: tuple[int, int, int]) -> Path:
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (32, 32), colour).save(path)
    return path


@pytest.fixture()
def model_and_labels(tmp_path: Path) -> tuple[Path, Path]:
    model = _build_model(tmp_path / "tagger.onnx")
    labels = tmp_path / "labels.txt"
    labels.write_text("\n".join(LABELS), encoding="utf-8")
    return model, labels


# ── label lists ──────────────────────────────────────────────────────────────


def test_label_lists_load_from_every_shape_repositories_publish(tmp_path: Path) -> None:
    """A label read from the wrong column mislabels the entire library."""
    plain = tmp_path / "labels.txt"
    plain.write_text("beach\nsunset\n\ndog\n", encoding="utf-8")
    assert load_labels(plain) == ["beach", "sunset", "dog"]

    listed = tmp_path / "labels.json"
    listed.write_text('["beach", "sunset"]', encoding="utf-8")
    assert load_labels(listed) == ["beach", "sunset"]

    mapped = tmp_path / "mapped.json"
    mapped.write_text('{"1": "sunset", "0": "beach", "10": "dog"}', encoding="utf-8")
    assert load_labels(mapped) == ["beach", "sunset", "dog"], "index order, not text order"

    # The WD tagger layout: id, name, category.
    wd = tmp_path / "selected_tags.csv"
    wd.write_text("tag_id,name,category\n1,beach,0\n2,sunset,0\n", encoding="utf-8")
    assert load_labels(wd) == ["beach", "sunset"]


def test_a_missing_label_list_is_named(tmp_path: Path) -> None:
    with pytest.raises(DependencyUnsatisfied, match="label list not found"):
        load_labels(tmp_path / "nope.txt")


# ── activation ───────────────────────────────────────────────────────────────


def test_scores_already_in_probability_space_are_not_squashed_again() -> None:
    """A model with its own sigmoid would otherwise have every score halved."""
    scores = np.array([0.9, 0.2, 0.05], dtype=np.float32)
    assert _activate(scores, "auto") == pytest.approx(scores)
    assert _activate(scores, "sigmoid")[0] == pytest.approx(0.7109495, abs=1e-5)


def test_multi_label_and_single_label_outputs_get_different_treatment() -> None:
    many = np.linspace(-4, 4, 200, dtype=np.float32)
    assert float(_activate(many, "auto").sum()) > 1.5, "sigmoid: labels are independent"
    few = np.array([2.0, 1.0, 0.1], dtype=np.float32)
    assert float(_activate(few, "auto").sum()) == pytest.approx(1.0), "softmax: one answer"


# ── configuration failures ───────────────────────────────────────────────────


def test_no_model_configured_says_what_to_do() -> None:
    analyzer = LocalTaggerAnalyzer()
    with pytest.raises(DependencyUnsatisfied, match="AI Model Store"):
        analyzer.analyze(_Ctx("image", config={}))


def test_a_label_list_beside_the_model_is_found_automatically(
    tmp_path: Path, model_and_labels: tuple[Path, Path]
) -> None:
    model, labels = model_and_labels
    labels.rename(model.with_name("selected_tags.csv"))
    model.with_name("selected_tags.csv").write_text(
        "tag_id,name\n1,beach\n2,sunset\n3,dog\n4,indoors\n", encoding="utf-8"
    )
    found_model, found_labels = LocalTaggerAnalyzer._paths({"model_path": str(model)})
    assert found_model == model
    assert found_labels.name == "selected_tags.csv"


def test_a_label_list_that_does_not_match_the_model_is_refused(tmp_path: Path) -> None:
    """Four outputs and five labels means every tag is off by one, silently."""
    model = _build_model(tmp_path / "m.onnx")
    labels = tmp_path / "labels.txt"
    labels.write_text("a\nb\nc\nd\ne", encoding="utf-8")
    analyzer = LocalTaggerAnalyzer()
    with pytest.raises(DependencyUnsatisfied, match="line up"):
        analyzer.analyze(
            _Ctx("image", config={"model_path": str(model), "labels_path": str(labels)})
        )


# ── inference ────────────────────────────────────────────────────────────────


def test_an_image_is_tagged_with_no_language_model_running(
    tmp_path: Path, model_and_labels: tuple[Path, Path]
) -> None:
    from PIL import Image

    model, labels = model_and_labels
    analyzer = LocalTaggerAnalyzer()
    found = analyzer.analyze(
        _Ctx(
            "image",
            config={"model_path": str(model), "labels_path": str(labels), "threshold": 0.0},
            image=Image.new("RGB", (32, 32), (255, 0, 0)),
        )
    )
    assert found, "a red frame must produce claims"
    assert all(item.namespace == DEFAULT_NAMESPACE for item in found)
    assert {item.label for item in found} <= set(LABELS)
    # A still has no time dimension, so nothing claims one.
    assert all(item.region is None for item in found)


def test_every_video_claim_carries_the_second_it_was_seen(
    tmp_path: Path, model_and_labels: tuple[Path, Path]
) -> None:
    """The whole point: tags on a video are located in the video."""
    model, labels = model_and_labels
    keyframes = [
        (0.0, _frame(tmp_path, "a.png", (255, 0, 0))),
        (12.5, _frame(tmp_path, "b.png", (0, 255, 0))),
        (61.25, _frame(tmp_path, "c.png", (0, 0, 255))),
    ]
    analyzer = LocalTaggerAnalyzer()
    found = analyzer.analyze(
        _Ctx(
            "video",
            config={"model_path": str(model), "labels_path": str(labels), "threshold": 0.0},
            keyframes=keyframes,
        )
    )

    timed = [item for item in found if item.region is not None]
    summary = [item for item in found if item.region is None]
    assert timed, "no timestamped claims"
    assert {item.region.frame_time for item in timed if item.region} == {0.0, 12.5, 61.25}
    assert all(item.value and item.value["at"] == item.region.frame_time for item in timed if item.region)
    assert summary, "a video must also be findable without knowing the timestamp"
    assert all(item.value and item.value["of"] == 3 for item in summary)


def test_a_glimpse_can_be_kept_out_of_the_facets(
    tmp_path: Path, model_and_labels: tuple[Path, Path]
) -> None:
    """One frame in forty is a glimpse; min_frames is what excludes it."""
    model, labels = model_and_labels
    keyframes = [
        (0.0, _frame(tmp_path, "r.png", (255, 0, 0))),
        (5.0, _frame(tmp_path, "g.png", (0, 255, 0))),
    ]
    config = {
        "model_path": str(model),
        "labels_path": str(labels),
        "threshold": 0.5,
        "min_frames": 2,
    }
    analyzer = LocalTaggerAnalyzer()
    found = analyzer.analyze(_Ctx("video", config=config, keyframes=keyframes))
    summary = [item for item in found if item.region is None]
    for item in summary:
        assert item.value and item.value["frames"] >= 2


def test_a_video_with_no_keyframes_still_gets_tagged(
    tmp_path: Path, model_and_labels: tuple[Path, Path]
) -> None:
    """Derivatives may have been skipped; the thumbnail beats refusing to run."""
    from PIL import Image

    model, labels = model_and_labels
    analyzer = LocalTaggerAnalyzer()
    found = analyzer.analyze(
        _Ctx(
            "video",
            config={"model_path": str(model), "labels_path": str(labels), "threshold": 0.0},
            image=Image.new("RGB", (32, 32), (10, 200, 10)),
            keyframes=[],
        )
    )
    assert found and all(item.region is None for item in found)


def test_frames_are_sampled_rather_than_all_decoded(
    tmp_path: Path, model_and_labels: tuple[Path, Path]
) -> None:
    """A two-hour video has hundreds of keyframes; tagging them all is waste."""
    model, labels = model_and_labels
    frame = _frame(tmp_path, "one.png", (120, 120, 120))
    keyframes = [(float(index) * 5.0, frame) for index in range(64)]
    analyzer = LocalTaggerAnalyzer()
    found = analyzer.analyze(
        _Ctx(
            "video",
            config={
                "model_path": str(model),
                "labels_path": str(labels),
                "threshold": 0.0,
                "max_frames": 6,
            },
            keyframes=keyframes,
        )
    )
    moments = {item.region.frame_time for item in found if item.region}
    assert 0 < len(moments) <= 6


def test_a_channels_last_model_is_fed_correctly(tmp_path: Path) -> None:
    """NHWC is as common as NCHW, and guessing wrong produces garbage silently."""
    from PIL import Image

    model = _build_model(tmp_path / "nhwc.onnx", channels_first=False)
    labels = tmp_path / "labels.txt"
    labels.write_text("\n".join(LABELS), encoding="utf-8")
    analyzer = LocalTaggerAnalyzer()
    found = analyzer.analyze(
        _Ctx(
            "image",
            config={"model_path": str(model), "labels_path": str(labels), "threshold": 0.0},
            image=Image.new("RGB", (64, 64), (200, 40, 40)),
        )
    )
    assert found


def test_the_session_is_loaded_once_and_reused(
    tmp_path: Path, model_and_labels: tuple[Path, Path]
) -> None:
    """Loading costs far more than running; per-asset reloads would dominate."""
    from PIL import Image

    model, labels = model_and_labels
    analyzer = LocalTaggerAnalyzer()
    config = {"model_path": str(model), "labels_path": str(labels), "threshold": 0.0}
    for _ in range(3):
        analyzer.analyze(_Ctx("image", config=config, image=Image.new("RGB", (8, 8), (1, 2, 3))))
    assert len(analyzer._models) == 1


# ── the catalogue ────────────────────────────────────────────────────────────


def test_the_catalogue_covers_every_kind_of_tagging_model() -> None:
    covered = set(tasks())
    assert {"tagging", "nsfw", "detection", "faces", "scenes", "speech", "audio", "text"} <= covered


def test_every_catalogue_entry_links_to_a_real_project() -> None:
    for item in CATALOG:
        assert item.url.startswith("https://github.com/"), item.id
        assert item.license and item.summary and item.namespace
        assert item.runtime in {"onnx", "torch", "http", "ffmpeg"}


def test_the_catalogue_can_be_filtered_the_way_the_store_filters_it() -> None:
    local = catalog_for(local_only=True)
    assert local and all(item.local for item in local)
    assert all(item.task == "nsfw" for item in catalog_for(task="nsfw"))
    assert any("timestamp" in item.summary.lower() or item.timestamps for item in CATALOG)
    assert catalog_for(search="whisper"), "search covers the summary text"
    assert entry("wd-tagger") is not None and entry("nope") is None


def test_models_that_run_in_process_say_what_they_need() -> None:
    for item in catalog_for(local_only=True):
        if item.runtime == "onnx":
            assert item.requires == "onnxruntime", item.id


# ── execution providers ──────────────────────────────────────────────────────


def test_the_gpu_is_preferred_and_the_azure_shim_is_never_used() -> None:
    """A stock Windows wheel lists a remote-inference provider first.

    Handing that list straight back is how a machine with a 4080 in it fails
    to use the 4080 — and worse, tries to send frames somewhere else.
    """
    available = ["AzureExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
    assert _rank_providers([], available) == ["CUDAExecutionProvider", "CPUExecutionProvider"]

    directml = ["AzureExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    assert _rank_providers([], directml) == ["DmlExecutionProvider", "CPUExecutionProvider"]


def test_cpu_remains_as_the_fallback_behind_every_accelerator() -> None:
    """A GPU provider that fails to initialise must degrade, not break."""
    ranked = _rank_providers([], ["CUDAExecutionProvider", "CPUExecutionProvider"])
    assert ranked[-1] == "CPUExecutionProvider"
    assert _rank_providers([], ["CPUExecutionProvider"]) == ["CPUExecutionProvider"]


def test_tensorrt_is_not_chosen_by_default() -> None:
    """It compiles an engine per model on first use, which reads as a hang."""
    ranked = _rank_providers(
        [], ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
    )
    assert ranked[0] == "CUDAExecutionProvider"
    assert "TensorrtExecutionProvider" not in ranked
    # ...but an operator who asks for it explicitly gets it.
    assert _rank_providers(
        ["TensorrtExecutionProvider"], ["TensorrtExecutionProvider", "CPUExecutionProvider"]
    ) == ["TensorrtExecutionProvider"]


def test_the_store_can_tell_you_whether_tagging_lands_on_the_gpu() -> None:
    from mediaengine.models import execution_providers

    report = execution_providers()
    assert isinstance(report["available"], list) and report["available"]
    assert report["gpu"] is bool(report["accelerator"])
