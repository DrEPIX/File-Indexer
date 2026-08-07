"""Search syntax for generic machine tags and opt-in safety filtering."""

from mediaengine.search import parse_query
from mediaengine.engine import MediaEngine


def test_nsfw_safe_requires_an_explicit_safe_rating() -> None:
    query = parse_query("nsfw:safe")
    assert [(item.namespace, item.label) for item in query.labels] == [
        ("safety.nsfw", "safe")
    ]


def test_nsfw_only_finds_flagged_assets() -> None:
    query = parse_query("nsfw:only")
    assert [(item.namespace, item.label) for item in query.labels] == [
        ("safety.nsfw", "flagged")
    ]


def test_generic_negative_annotation_filter() -> None:
    query = parse_query("not:safety.nsfw:flagged")
    assert [(item.namespace, item.label) for item in query.excluded_labels] == [
        ("safety.nsfw", "flagged")
    ]


def test_safety_filters_execute_against_annotations(engine: MediaEngine) -> None:
    safe_id, _ = engine.repos.assets.upsert_asset(
        content_hash="sha256:safe",
        media_type="video",
        size_bytes=10,
        hash_algorithm="sha256",
    )
    flagged_id, _ = engine.repos.assets.upsert_asset(
        content_hash="sha256:flagged",
        media_type="video",
        size_bytes=20,
        hash_algorithm="sha256",
    )
    unrated_id, _ = engine.repos.assets.upsert_asset(
        content_hash="sha256:unrated",
        media_type="video",
        size_bytes=30,
        hash_algorithm="sha256",
    )
    engine.repos.annotations.add_user_annotation(safe_id, "safety.nsfw", "safe")
    engine.repos.annotations.add_user_annotation(flagged_id, "safety.nsfw", "flagged")

    strict_safe = engine.search("nsfw:safe", with_facets=False)
    assert [item["id"] for item in strict_safe.hits] == [safe_id]

    excluded = engine.search("not:safety.nsfw:flagged", with_facets=False)
    assert {item["id"] for item in excluded.hits} == {safe_id, unrated_id}
