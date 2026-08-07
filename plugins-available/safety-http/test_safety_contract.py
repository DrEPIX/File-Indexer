"""Deterministic protocol tests; no model download or unsafe fixture required."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from fastapi.testclient import TestClient
from PIL import Image

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import safety_server as server


class FakeClassifier:
    detail = "fake safety classifier on cpu"

    def __init__(self, scores: list[float]) -> None:
        self.scores = scores

    def classify(self, images: Sequence[Image.Image]) -> list[float]:
        return self.scores[: len(images)]


def jpeg_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), (30, 60, 90)).save(output, format="JPEG")
    return output.getvalue()


def work(path: Path, *, video: bool = False) -> dict[str, object]:
    derivatives: dict[str, object]
    media_type = "video" if video else "image"
    if video:
        derivatives = {
            "keyframes": [
                {"time": 0.0, "path": str(path)},
                {"time": 5.0, "path": str(path)},
                {"time": 10.0, "path": str(path)},
            ]
        }
    else:
        derivatives = {"thumbnails": {"512": {"path": str(path)}}}
    return {
        "protocol": server.PROTOCOL,
        "request_id": "safety-1",
        "deadline_s": 10.0,
        "asset": {"asset_id": 1, "media_type": media_type},
        "derivatives": derivatives,
        "config": {},
    }


class SafetyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(server.app)

    def test_manifest_is_local_advisory_classifier(self) -> None:
        payload = self.client.get("/manifest").json()
        self.assertEqual(payload["id"], "local.safety")
        self.assertIn("safety.nsfw", payload["emits"])
        self.assertFalse(payload["requires"]["gpu"])

    def test_safe_image_gets_explicit_safe_rating(self) -> None:
        server.runtime.install_for_test(FakeClassifier([0.05]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe.jpg"
            path.write_bytes(jpeg_bytes())
            response = self.client.post("/analyze", json=work(path))
        self.assertEqual(response.status_code, 200, response.text)
        rating = response.json()["annotations"][0]
        self.assertEqual(rating["label"], "safe")
        self.assertAlmostEqual(rating["confidence"], 0.95)

    def test_video_uses_max_risk_and_keeps_timestamp_evidence(self) -> None:
        server.runtime.install_for_test(FakeClassifier([0.1, 0.55, 0.9]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.jpg"
            path.write_bytes(jpeg_bytes())
            response = self.client.post("/analyze", json=work(path, video=True))
        self.assertEqual(response.status_code, 200, response.text)
        emitted = response.json()["annotations"]
        self.assertEqual(emitted[0]["label"], "flagged")
        self.assertEqual(emitted[0]["value"]["review_frames"], 2)
        self.assertEqual([item["region"]["frame_time"] for item in emitted[1:]], [5.0, 10.0])

    def test_invalid_thresholds_are_permanent(self) -> None:
        server.runtime.install_for_test(FakeClassifier([0.1]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.jpg"
            path.write_bytes(jpeg_bytes())
            payload = work(path)
            payload["config"] = {"review_threshold": 0.9, "flag_threshold": 0.5}
            response = self.client.post("/analyze", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["error"]["retryable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
