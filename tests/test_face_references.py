from __future__ import annotations

import pytest

from mediaengine.db.repositories import PendingAnnotation, PendingRegion, Repositories
from mediaengine.identity import FaceReferenceMatcher


def reference_pack() -> dict[str, object]:
    return {
        "schema": "mediaengine.face-reference-pack/1",
        "name": "test public figures",
        "version": "1",
        "model_id": "facenet-vggface2",
        "embedding_dim": 3,
        "source_url": "https://example.test/manifest",
        "license_name": "test license",
        "attribution": "test attribution",
        "rights_statement": "test fixture is authorized for processing",
        "retention_policy": "delete after the test",
        "people": [
            {
                "external_id": "person-a",
                "display_name": "Person A",
                "source_url": "https://example.test/a",
                "references": [
                    {
                        "embedding": [1.0, 0.0, 0.0],
                        "source_ref": "a.jpg",
                        "source_sha256": "a" * 64,
                    }
                ],
            },
            {
                "external_id": "person-b",
                "display_name": "Person B",
                "source_url": "https://example.test/b",
                "references": [
                    {
                        "embedding": [0.0, 1.0, 0.0],
                        "source_ref": "b.jpg",
                        "source_sha256": "b" * 64,
                    }
                ],
            },
        ],
    }


def add_face(repos: Repositories, suffix: str, vector: list[float]) -> int:
    asset_id, _ = repos.assets.upsert_asset(
        content_hash=(suffix * 64)[:64], media_type="image", size_bytes=123
    )
    producer_id = repos.annotations.get_or_create_producer(
        "acme.vision",
        "1.1.0",
        model_id="facenet-vggface2+fasterrcnn-resnet50-fpn-v2",
        transport="http",
    )
    repos.annotations.commit(
        asset_id,
        producer_id,
        "acme.vision",
        [
            PendingAnnotation(
                namespace="vision.face",
                label="face",
                confidence=0.99,
                region=PendingRegion(x=0.1, y=0.1, w=0.5, h=0.5, kind="face"),
                embedding=vector,
            )
        ],
    )
    row = repos.db.query_one("SELECT id FROM regions WHERE asset_id=?", (asset_id,))
    assert row is not None
    return int(row["id"])


def test_pack_match_requires_explicit_accept(repos: Repositories) -> None:
    region_id = add_face(repos, "c", [0.99, 0.01, 0.0])
    imported = repos.reference_faces.import_pack(reference_pack())
    pack_id = int(imported["id"])
    assert imported["people_count"] == 2
    assert imported["embedding_count"] == 2
    assert repos.reference_faces.import_pack(reference_pack())["imported"] is False

    matched = FaceReferenceMatcher(repos).match(pack_id, threshold=0.7, min_margin=0.1)
    assert matched.suggestions == 1
    assert repos.db.query_one("SELECT * FROM region_identity WHERE region_id=?", (region_id,)) is None

    suggestion = repos.reference_faces.list_suggestions()[0]
    assert suggestion["display_name"] == "Person A"
    reviewed = repos.reference_faces.review(int(suggestion["id"]), accept=True)
    assert reviewed["status"] == "accepted"
    link = repos.db.query_one("SELECT * FROM region_identity WHERE region_id=?", (region_id,))
    assert link is not None
    assert link["source"] == "user"
    assert link["confirmed"] == 1


def test_rejection_is_not_resuggested(repos: Repositories) -> None:
    add_face(repos, "d", [1.0, 0.0, 0.0])
    pack_id = int(repos.reference_faces.import_pack(reference_pack())["id"])
    matcher = FaceReferenceMatcher(repos)
    assert matcher.match(pack_id, threshold=0.7).suggestions == 1
    suggestion_id = int(repos.reference_faces.list_suggestions()[0]["id"])
    repos.reference_faces.review(suggestion_id, accept=False)
    rerun = matcher.match(pack_id, threshold=0.7)
    assert rerun.suggestions == 0
    assert rerun.reviewed_skipped == 1
    assert len(repos.reference_faces.list_suggestions(status="rejected")) == 1


def test_reference_pack_validation_and_biometric_purge(repos: Repositories) -> None:
    invalid = reference_pack()
    invalid["license_name"] = ""
    with pytest.raises(ValueError, match="license_name"):
        repos.reference_faces.import_pack(invalid)

    repos.reference_faces.import_pack(reference_pack())
    counts = repos.identities.purge_biometrics()
    assert counts["face_reference_packs"] == 1
    assert repos.reference_faces.list_packs() == []
