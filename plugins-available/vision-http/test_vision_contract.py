"""Engine-free contract tests using a tiny deterministic fake model."""

from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from fastapi.testclient import TestClient
from PIL import Image

import vision_server as server
from vision_model import FaceResult, FrameResult, ObjectResult


class FakeAnalyzer:
    embedding_dim = 512
    detail = "fake face+object models on cuda"

    def analyze(
        self,
        images: Sequence[Image.Image],
        *,
        face_threshold: float,
        object_threshold: float,
        max_faces: int,
        max_objects: int,
    ) -> list[FrameResult]:
        del face_threshold, object_threshold, max_faces, max_objects
        vector = [1.0] + [0.0] * 511
        return [
            FrameResult(
                faces=[FaceResult(0.99, (-2.0, 1.0, 6.0, 7.0), vector)],
                objects=[ObjectResult("cat", 0.95, (2.0, 2.0, 8.0, 6.0))],
            )
            for _ in images
        ]


def jpeg_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (10, 8), (20, 40, 60)).save(output, format="JPEG")
    return output.getvalue()


def work_item(path: Path, *, media_type: str = "image") -> dict[str, object]:
    derivatives: dict[str, object]
    if media_type == "video":
        derivatives = {"keyframes": [{"time": 2.5, "path": str(path)}]}
    else:
        derivatives = {"thumbnails": {"512": {"path": str(path)}}}
    return {
        "protocol": server.PROTOCOL,
        "request_id": "vision-1",
        "deadline_s": 10.0,
        "asset": {"asset_id": 1, "media_type": media_type, "filename": path.name},
        "derivatives": derivatives,
        "config": {},
    }


class VisionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        server.runtime.install_for_test(FakeAnalyzer())
        self.client = TestClient(server.app)

    def test_manifest_advertises_optional_gpu_and_face_dimension(self) -> None:
        manifest = self.client.get("/manifest").json()
        self.assertEqual(manifest["protocol"], "mediaengine.analyzer/1")
        self.assertEqual(manifest["embedding_dim"], 512)
        self.assertFalse(manifest["requires"]["gpu"])
        self.assertEqual(manifest["namespaces"]["vision.face"]["embedding_dim"], 512)

    def test_image_emits_clamped_face_vector_and_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.jpg"
            image.write_bytes(jpeg_bytes())
            response = self.client.post("/analyze", json=work_item(image))
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        face, detected = payload["annotations"]
        self.assertEqual(face["namespace"], "vision.face")
        self.assertEqual(len(face["embedding"]), 512)
        self.assertEqual(face["region"]["x"], 0.0)
        self.assertEqual(face["region"]["w"], 0.6)
        self.assertEqual(detected["label"], "cat")
        self.assertNotIn("embedding", detected)

    def test_video_regions_retain_frame_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(jpeg_bytes())
            response = self.client.post("/analyze", json=work_item(image, media_type="video"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(all(item["region"]["frame_time"] == 2.5 for item in response.json()["annotations"]))

    def test_inline_transfer(self) -> None:
        payload = work_item(Path("unused.jpg"))
        payload["derivatives"] = {
            "thumbnails": {"512": {"content_b64": base64.b64encode(jpeg_bytes()).decode("ascii")}}
        }
        response = self.client.post("/analyze", json=payload)
        self.assertEqual(response.status_code, 200, response.text)

    def test_invalid_threshold_is_permanent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.jpg"
            image.write_bytes(jpeg_bytes())
            payload = work_item(image)
            payload["config"] = {"face_threshold": 1.5}
            response = self.client.post("/analyze", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["error"]["retryable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
