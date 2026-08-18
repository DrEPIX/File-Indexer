"""Tag pixels with a local ONNX model — no language model, no server.

The language-model path is powerful and slow: one HTTP round trip per frame,
a model that must be loaded and served, and an answer that has to be parsed
back into a vocabulary. For *tagging*, a plain vision model is the better
tool. It is two orders of magnitude faster, it runs in this process, it cannot
hallucinate a label outside its own list, and it keeps working when nothing
else is running. The LLM is then free to do what only it can do — answer
questions in the assistant.

This analyzer is deliberately model-agnostic. It drives any ONNX image model
whose output is one score per label:

* multi-label taggers (sigmoid over thousands of tags),
* single-label classifiers (softmax over a closed set),
* NSFW raters, scene classifiers, anything else shaped that way.

Point it at a ``.onnx`` file and a label list. Everything else — input size,
layout, colour order — is read from the model's own signature, because a user
downloading a model from a repository does not know its tensor layout and
should not have to.

**Videos are tagged over time.** Keyframes already exist with their timestamps
(the scan extracts them), so each frame is tagged separately and its claims are
recorded against ``frame_time``. That is what makes "where in this video is the
beach" answerable, rather than just "this video contains a beach".
"""

from __future__ import annotations

import csv
import json
import logging
import threading
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ...errors import DependencyUnsatisfied, PluginExecutionError
from ..context import AnalysisContext
from ..contract import Annotation, PluginInfo, Region

__all__ = ["LocalTaggerAnalyzer", "LoadedModel", "load_labels"]

_LOG = logging.getLogger(__name__)

#: Default namespace. Overridable per configuration, because the same analyzer
#: pointed at an NSFW model should not file its answers under "tag".
DEFAULT_NAMESPACE = "content.tag"

#: How many of a frame's scores are ever recorded, however low the threshold.
#: A 6,000-tag model will happily return 6,000 weak claims per frame; a library
#: with a million frames cannot afford that and no one would read it.
DEFAULT_TOP_K = 12

#: ImageNet normalisation. Applied only when the model asks for it, because
#: taggers trained on raw 0-1 pixels are just as common.
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)


def load_labels(path: Path) -> list[str]:
    """Read a label list from the three shapes model repositories publish.

    ``.txt`` one per line, ``.json`` as a list or an index->name mapping, and
    ``.csv`` as used by the WD taggers, whose ``name`` column sits beside a
    numeric id and a category. Guessing this correctly matters more than it
    looks: labels off by one row means every tag in the library is wrong by one
    row, and nothing about the output looks broken.
    """
    if not path.is_file():
        raise DependencyUnsatisfied(f"label list not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return [str(payload[key]) for key in sorted(payload, key=lambda k: int(k))]
        return [str(item) for item in payload]
    if suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        if not rows:
            return []
        header = [column.strip().lower() for column in rows[0]]
        column = header.index("name") if "name" in header else (1 if len(header) > 1 else 0)
        body = rows[1:] if "name" in header else rows
        return [row[column].strip() for row in body if len(row) > column]
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class LoadedModel:
    """One ONNX session plus everything needed to feed and read it.

    Held open for the life of the process: loading a session costs far more
    than running it, and the analyzer is called once per asset.
    """

    def __init__(self, model_path: Path, labels: Sequence[str], *, providers: Sequence[str] | None = None) -> None:
        try:
            import onnxruntime
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise DependencyUnsatisfied(
                "onnxruntime is not installed. Install it with "
                "`pip install onnxruntime-gpu` (NVIDIA) or `pip install onnxruntime` (CPU)."
            ) from exc

        # CUDA and cuDNN are commonly installed as pip wheels rather than a
        # system toolkit, and their DLLs are then not on PATH. onnxruntime can
        # find them, but only if asked before the first session is built.
        preload = getattr(onnxruntime, "preload_dlls", None)
        if callable(preload):
            try:
                preload()
            except Exception as exc:  # noqa: BLE001 - a GPU that will not load is not fatal
                _LOG.debug("could not preload GPU libraries: %s", exc)

        available = list(onnxruntime.get_available_providers())
        chosen = _rank_providers(providers or available, available)
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=chosen
        )
        self.providers: list[str] = list(self.session.get_providers())
        self.labels = list(labels)
        self.path = model_path

        spec = self.session.get_inputs()[0]
        self.input_name = spec.name
        shape = list(spec.shape)
        # A model states its own layout; a user who downloaded it does not know
        # it. NCHW and NHWC are told apart by which axis is 3.
        self.channels_first = len(shape) == 4 and _as_int(shape[1]) == 3
        if len(shape) == 4:
            height = _as_int(shape[2] if self.channels_first else shape[1])
            width = _as_int(shape[3] if self.channels_first else shape[2])
        else:  # pragma: no cover - non-image model
            height = width = None
        self.size = (width or 448, height or 448)
        self.dtype = np.float16 if "float16" in str(spec.type) else np.float32
        self.output_name = self.session.get_outputs()[0].name

        outputs = self.session.get_outputs()[0].shape
        declared = _as_int(outputs[-1]) if outputs else None
        if declared and self.labels and declared != len(self.labels):
            raise DependencyUnsatisfied(
                f"{model_path.name} produces {declared} scores but the label list has "
                f"{len(self.labels)} entries. They must line up exactly, or every tag is wrong."
            )

    def prepare(self, image: Image.Image, *, normalize: bool, bgr: bool, scale: bool) -> np.ndarray:
        """Turn a PIL image into the batch tensor this model expects."""
        frame = image.convert("RGB").resize(self.size, Image.Resampling.BILINEAR)
        array = np.asarray(frame, dtype=np.float32)
        if scale:
            array /= 255.0
        if normalize:
            array = (array - np.array(_IMAGENET_MEAN, dtype=np.float32)) / np.array(
                _IMAGENET_STD, dtype=np.float32
            )
        if bgr:
            array = array[:, :, ::-1]
        if self.channels_first:
            array = array.transpose(2, 0, 1)
        return np.ascontiguousarray(array[None, ...], dtype=self.dtype)

    def run(self, batch: np.ndarray) -> np.ndarray:
        raw = self.session.run([self.output_name], {self.input_name: batch})[0]
        return np.asarray(raw, dtype=np.float32).reshape(-1)


#: Execution providers in the order we want them, fastest first. Ordering is
#: not cosmetic: ``get_available_providers()`` lists whatever the build has,
#: and on a stock Windows wheel that puts ``AzureExecutionProvider`` — a
#: remote-inference shim — ahead of the local CPU one. Handing that list back
#: unsorted is how a machine with a 4080 in it ends up not using the 4080.
#: CUDA before TensorRT deliberately. TensorRT is faster once warm, but it
#: compiles an engine for each new model and shape on first use, which turns
#: "tag this video" into a multi-minute stall the first time and looks like a
#: hang. CPU is last and always present, so a GPU provider that fails to
#: initialise degrades to slow rather than to broken.
PROVIDER_PREFERENCE: tuple[str, ...] = (
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "ROCMExecutionProvider",
    "CoreMLExecutionProvider",
    "CPUExecutionProvider",
)


def _rank_providers(requested: Sequence[str], available: Sequence[str]) -> list[str]:
    """Order the usable providers by preference, GPU first, CPU last.

    Ordering is not cosmetic. ``get_available_providers()`` returns whatever
    the build offers, and a stock Windows wheel lists
    ``AzureExecutionProvider`` — a remote-inference shim — first. Handing that
    list straight back is how a machine with a 4080 in it fails to use the
    4080, so anything not named here is dropped rather than ranked.
    """
    usable = [name for name in requested if name in available] or list(available)
    ranked = [name for name in PROVIDER_PREFERENCE if name in usable]
    return ranked or list(usable)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sigmoid(scores: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-scores))


def _softmax(scores: np.ndarray) -> np.ndarray:
    shifted = scores - scores.max()
    exponentiated = np.exp(shifted)
    return exponentiated / max(float(exponentiated.sum()), 1e-9)


def _activate(scores: np.ndarray, mode: str) -> np.ndarray:
    """Turn raw outputs into probabilities without being told which they are.

    ``auto`` looks at the numbers: values already inside 0..1 that do not sum
    to 1 came from a sigmoid layer the model applied itself, and squashing them
    again would halve every confidence in the library.
    """
    if mode == "sigmoid":
        return _sigmoid(scores)
    if mode == "softmax":
        return _softmax(scores)
    if mode == "none":
        return scores
    inside_unit = bool(scores.min() >= 0.0 and scores.max() <= 1.0)
    if inside_unit:
        return scores
    return _sigmoid(scores) if scores.size > 100 else _softmax(scores)


class LocalTaggerAnalyzer:
    """Run a local ONNX vision model over stills and video keyframes."""

    info = PluginInfo(
        id="local.vision-tagger",
        version="1.0.0",
        accepts=("image", "video"),
        emits=(DEFAULT_NAMESPACE,),
        description=(
            "Tag images and video frames with a local ONNX model — fast, offline, and with "
            "a timestamp on every claim made about a video."
        ),
        pixels=True,
        frames=True,
        gpu=True,
        max_concurrency=1,
        namespaces={DEFAULT_NAMESPACE: {"display_name": "Content tag"}},
    )

    def __init__(self) -> None:
        # One session per (model, labels) pair, shared across assets. The
        # analyzer is constructed once by the registry, so this is process-wide.
        self._models: dict[tuple[str, str], LoadedModel] = {}
        self._lock = threading.Lock()

    # ── configuration ───────────────────────────────────────────────────────

    @staticmethod
    def _paths(config: dict[str, Any]) -> tuple[Path, Path]:
        model = str(config.get("model_path") or "").strip()
        labels = str(config.get("labels_path") or "").strip()
        if not model:
            raise DependencyUnsatisfied(
                "no model chosen. Open the AI Model Store, pick a local tagging model, and "
                "point this analyzer at its .onnx file."
            )
        model_path = Path(model).expanduser()
        if not model_path.is_file():
            raise DependencyUnsatisfied(f"model file not found: {model_path}")
        if labels:
            return model_path, Path(labels).expanduser()
        # Model repositories almost always ship the labels beside the weights.
        for candidate in ("selected_tags.csv", "labels.txt", "classes.txt", "labels.json"):
            beside = model_path.with_name(candidate)
            if beside.is_file():
                return model_path, beside
        raise DependencyUnsatisfied(
            f"no label list given, and none found beside {model_path.name}. A model's scores "
            "are meaningless without the list of what they are scores for."
        )

    def _model(self, config: dict[str, Any]) -> LoadedModel:
        model_path, labels_path = self._paths(config)
        key = (str(model_path), str(labels_path))
        with self._lock:
            loaded = self._models.get(key)
            if loaded is None:
                loaded = LoadedModel(
                    model_path,
                    load_labels(labels_path),
                    providers=config.get("providers") or None,
                )
                _LOG.info(
                    "loaded %s (%d labels) on %s",
                    model_path.name,
                    len(loaded.labels),
                    ", ".join(loaded.providers),
                )
                self._models[key] = loaded
            return loaded

    # ── inference ───────────────────────────────────────────────────────────

    def _score(
        self, model: LoadedModel, image: Image.Image, config: dict[str, Any]
    ) -> list[tuple[str, float]]:
        batch = model.prepare(
            image,
            normalize=bool(config.get("normalize", False)),
            bgr=bool(config.get("bgr", False)),
            scale=bool(config.get("scale", True)),
        )
        scores = _activate(model.run(batch), str(config.get("activation", "auto")))
        if scores.size != len(model.labels):
            raise PluginExecutionError(
                f"{model.path.name} returned {scores.size} scores for {len(model.labels)} labels",
                plugin_id=self.info.id,
            )
        threshold = float(config.get("threshold", 0.35))
        top_k = int(config.get("top_k", DEFAULT_TOP_K))
        order = np.argsort(scores)[::-1][: max(1, top_k)]
        return [
            (model.labels[index], float(scores[index]))
            for index in order
            if float(scores[index]) >= threshold
        ]

    def _frames(self, ctx: AnalysisContext, config: dict[str, Any]) -> list[tuple[float | None, Image.Image]]:
        """The frames to tag, each with the second it was taken from."""
        if ctx.media_type != "video":
            image = ctx.image(prefer_size=512)
            return [(None, image)] if image is not None else []
        limit = int(config.get("max_frames", 12))
        every = config.get("every_seconds")
        frames: list[tuple[float | None, Image.Image]] = []
        keyframes = ctx.keyframes(every=float(every) if every else None)
        if not keyframes:
            # No keyframes extracted (a still-image-like video, or derivatives
            # were skipped). The thumbnail is better than refusing to tag.
            image = ctx.image(prefer_size=512)
            return [(None, image)] if image is not None else []
        step = max(1, len(keyframes) // max(1, limit))
        for seconds, path in keyframes[::step][:limit]:
            try:
                with Image.open(path) as handle:
                    frames.append((float(seconds), handle.convert("RGB")))
            except OSError as exc:
                _LOG.debug("keyframe %s unreadable: %s", path, exc)
        return frames

    def analyze(self, ctx: AnalysisContext) -> Sequence[Annotation]:
        config = dict(ctx.config)
        model = self._model(config)
        namespace = str(config.get("namespace") or DEFAULT_NAMESPACE)
        frames = self._frames(ctx, config)
        if not frames:
            return []

        annotations: list[Annotation] = []
        best: dict[str, float] = defaultdict(float)
        seen_in: dict[str, int] = defaultdict(int)
        for seconds, image in frames:
            for label, score in self._score(model, image, config):
                best[label] = max(best[label], score)
                seen_in[label] += 1
                if seconds is not None:
                    # The timestamped claim: this label, in this video, here.
                    annotations.append(
                        Annotation(
                            namespace=namespace,
                            label=label,
                            confidence=score,
                            value={"at": round(seconds, 2), "model": model.path.name},
                            region=Region(frame_time=round(seconds, 2), kind="frame"),
                        )
                    )

        # Plus one claim per label for the asset as a whole, so a search for a
        # tag finds the video without having to know which second to ask about.
        # A label seen in a single frame of forty is a glimpse, not a subject:
        # `min_frames` is what keeps a facet from filling with them.
        minimum = int(config.get("min_frames", 1)) if len(frames) > 1 else 1
        for label, confidence in best.items():
            if seen_in[label] < minimum:
                continue
            annotations.append(
                Annotation(
                    namespace=namespace,
                    label=label,
                    confidence=confidence,
                    value={
                        "frames": seen_in[label],
                        "of": len(frames),
                        "model": model.path.name,
                    },
                )
            )
        return annotations
