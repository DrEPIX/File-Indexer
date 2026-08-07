"""Caffe adapter tests with fake OpenCV DNN objects and no model weights."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from vision_model import CaffeDetector, configured_caffe_detectors


class FakeNet:
    def __init__(self, output: np.ndarray) -> None:
        self.output = output
        self.input: object | None = None

    def setInput(self, value: object) -> None:  # noqa: N802 - OpenCV API
        self.input = value

    def forward(self) -> np.ndarray:
        return self.output


class FakeDnn:
    @staticmethod
    def blobFromImage(*args: object, **kwargs: object) -> np.ndarray:  # noqa: N802
        return np.zeros((1, 3, 300, 300), dtype=np.float32)


class FakeCv2:
    dnn = FakeDnn()


class CaffeDetectorTests(unittest.TestCase):
    def test_ssd7_output_maps_labels_boxes_and_model_provenance(self) -> None:
        output = np.array([[[[0, 1, 0.9, 0.1, 0.2, 0.6, 0.8]]]], dtype=np.float32)
        detector = CaffeDetector(
            prototxt="unused.prototxt",
            model="unused.caffemodel",
            labels=["background", "custom-object"],
            model_id="custom-one",
            net=FakeNet(output),
            cv2_module=FakeCv2(),
        )
        detected = detector.detect(Image.new("RGB", (200, 100)), threshold=0.5, maximum=10)
        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0].label, "custom-object")
        self.assertEqual(detected[0].backend, "caffe")
        self.assertEqual(detected[0].model_id, "custom-one")
        self.assertAlmostEqual(detected[0].box[0], 20.0, places=4)
        self.assertAlmostEqual(detected[0].box[3], 80.0, places=4)

    def test_multi_model_config_isolates_invalid_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.prototxt").write_text("name: 'fixture'", encoding="utf-8")
            (root / "valid.caffemodel").write_bytes(b"fixture")
            config = root / "models.json"
            config.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "id": "valid",
                                "prototxt": "valid.prototxt",
                                "model": "valid.caffemodel",
                                "labels": ["background", "thing"],
                            },
                            {"id": "broken", "prototxt": "missing.prototxt"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"VISION_CAFFE_MODELS_CONFIG": str(config)}):
                # The module constant is read at import; patch it for this
                # focused loader test without re-importing heavyweight modules.
                with patch("vision_model.CAFFE_MODELS_CONFIG", str(config)):
                    detectors, errors = configured_caffe_detectors()
        self.assertEqual([item.model_id for item in detectors], ["valid"])
        self.assertEqual(len(errors), 1)
        self.assertIn("broken", errors[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
