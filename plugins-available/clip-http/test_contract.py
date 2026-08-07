"""Engine-free black-box tests for the CLIP HTTP analyzer contract."""

from __future__ import annotations

import io
import threading
import time
import unittest
from pathlib import Path
from typing import Sequence

from fastapi.testclient import TestClient
from PIL import Image

import server
from model import EncodedBatch


class FakeEncoder:
    """Small deterministic model that avoids network and heavyweight imports."""

    embedding_dim = 512
    detail = "fake ViT-B-32 on cpu"

    def encode(self, images: Sequence[Image.Image], labels: Sequence[str]) -> EncodedBatch:
        vectors = [[1.0 if index == 0 else 0.0 for index in range(self.embedding_dim)] for _ in images]
        score = 1.0 / len(labels) if labels else 0.0
        scores = [{label: score for label in labels} for _ in images]
        return EncodedBatch(embeddings=vectors, label_scores=scores)


def jpeg_bytes() -> bytes:
    """Create a tiny valid JPEG fixture in memory."""

    output = io.BytesIO()
    Image.new("RGB", (4, 3), (200, 100, 50)).save(output, format="JPEG")
    return output.getvalue()


def work_item(path: Path, *, media_type: str = "image", request_id: str = "request-1") -> dict[str, object]:
    """Return a minimal frozen-protocol work item."""

    return {
        "protocol": server.PROTOCOL,
        "request_id": request_id,
        "deadline_s": 10.0,
        "asset": {
            "asset_id": 1,
            "content_hash": "sha256:test",
            "media_type": media_type,
            "mime_type": "image/jpeg",
            "size_bytes": path.stat().st_size,
            "captured_at": None,
            "filename": path.name,
        },
        "metadata": {"width": 4, "height": 3},
        "derivatives": {"thumbnails": {"512": {"path": str(path.resolve())}}},
        "text": None,
        "prior_annotations": [],
        "config": {"prompts": []},
    }


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        server.runtime.install_for_test(FakeEncoder())
        self.client = TestClient(server.app)

    def test_manifest_matches_frozen_shape(self) -> None:
        manifest = self.client.get("/manifest").json()
        self.assertEqual(manifest["protocol"], server.PROTOCOL)
        self.assertEqual(manifest["id"], "acme.clip")
        self.assertEqual(manifest["version"], "1.1.0")
        self.assertEqual(manifest["embedding_dim"], 512)
        self.assertEqual(manifest["transfer"], "both")
        self.assertEqual(manifest["accepts"], ["image", "video"])
        self.assertTrue(manifest["requires"]["pixels"])
        self.assertFalse(manifest["requires"]["gpu"])

    def test_health_reports_loading_then_ok(self) -> None:
        gate = threading.Event()

        def load_after_gate() -> FakeEncoder:
            gate.wait(timeout=2.0)
            return FakeEncoder()

        server.runtime.reset_for_test(load_after_gate)
        loading = self.client.get("/health")
        self.assertEqual(loading.status_code, 200)
        self.assertEqual(loading.json()["status"], "loading")
        gate.set()
        for _ in range(100):
            ready = self.client.get("/health")
            if ready.json()["status"] == "ok":
                break
            time.sleep(0.01)
        self.assertEqual(ready.json()["status"], "ok")
        self.assertTrue(ready.json()["model_loaded"])

    def test_health_rejects_model_dimension_drift(self) -> None:
        """A model change cannot silently poison an existing vector index."""

        class WrongDimensionEncoder(FakeEncoder):
            embedding_dim = 128

        server.runtime.reset_for_test(WrongDimensionEncoder)
        first = self.client.get("/health")
        self.assertEqual(first.status_code, 200)
        for _ in range(50):
            response = self.client.get("/health")
            if response.status_code == 503:
                break
            time.sleep(0.01)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("declared dimension 512", response.json()["detail"])

    def test_valid_jpeg_returns_native_embedding(self) -> None:
        with self.subTest("path transfer"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as directory:
                path = Path(directory) / "fixture.jpg"
                path.write_bytes(jpeg_bytes())
                response = self.client.post("/analyze", json=work_item(path))
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["request_id"], "request-1")
        embedding = payload["annotations"][0]["embedding"]
        self.assertEqual(len(embedding), self.client.get("/manifest").json()["embedding_dim"])

    def test_inline_transfer(self) -> None:
        import base64

        encoded = base64.b64encode(jpeg_bytes()).decode("ascii")
        payload = work_item(Path(__file__))
        payload["derivatives"] = {"thumbnails": {"512": {"content_b64": encoded, "content_type": "image/jpeg"}}}
        response = self.client.post("/analyze", json=payload)
        self.assertEqual(response.status_code, 200, response.text)

    def test_video_keyframes_and_mean_pool_keep_declared_dimension(self) -> None:
        """Frame vectors and the asset-level mean must stay in one vector space."""

        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            first = Path(directory) / "frame_000.jpg"
            second = Path(directory) / "frame_100.jpg"
            first.write_bytes(jpeg_bytes())
            second.write_bytes(jpeg_bytes())
            payload = work_item(first, media_type="video", request_id="video-1")
            payload["derivatives"] = {
                "keyframes": [
                    {"time": 0.0, "path": str(first.resolve())},
                    {"time": 1.0, "path": str(second.resolve())},
                ]
            }
            response = self.client.post("/analyze", json=payload)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        embeddings = [
            annotation["embedding"]
            for annotation in result["annotations"]
            if annotation["namespace"] == "clip" and annotation["label"] == "embedding"
        ]
        declared = self.client.get("/manifest").json()["embedding_dim"]
        self.assertEqual(len(embeddings), 3)  # two frames plus one mean-pooled asset vector
        self.assertTrue(all(len(embedding) == declared == 512 for embedding in embeddings))
        self.assertNotIn("region", result["annotations"][2])

    def test_default_taxonomy_emits_ranked_visual_categories(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.jpg"
            path.write_bytes(jpeg_bytes())
            payload = work_item(path)
            payload["config"] = {"tag_threshold": 0.0, "top_k": 3}
            response = self.client.post("/analyze", json=payload)
        categories = [
            item for item in response.json()["annotations"]
            if item["namespace"] == "visual.category"
        ]
        self.assertEqual(len(categories), 3)
        self.assertTrue(all(item["value"]["group"] in {"format", "subject", "activity", "scene"} for item in categories))

    def test_corrupt_image_is_permanent_400(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "broken.jpg"
            path.write_bytes(b"not a jpeg")
            response = self.client.post("/analyze", json=work_item(path))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["kind"], "corrupt_media")
        self.assertFalse(response.json()["error"]["retryable"])

    def test_unknown_media_is_successful_empty_result(self) -> None:
        payload = work_item(Path(__file__), media_type="audio", request_id="unknown-1")
        response = self.client.post("/analyze", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request_id"], "unknown-1")
        self.assertEqual(response.json()["annotations"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
