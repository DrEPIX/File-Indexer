from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

from mediaengine.db.repositories import PendingAnnotation, PendingRegion, Repositories
from mediaengine.identity import FaceClusterer
from mediaengine.profiles import resolve_profile_link
from mediaengine.profiles.wikipedia import WikipediaClient


def add_face(repos: Repositories, digest_character: str, vector: list[float]) -> int:
    asset_id, _ = repos.assets.upsert_asset(
        content_hash=digest_character * 64,
        media_type="image",
        size_bytes=100,
    )
    producer_id = repos.annotations.get_or_create_producer(
        "acme.vision",
        "1.1.0",
        model_id="facenet-vggface2+objects",
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


def test_local_face_clusters_form_name_prompts(repos: Repositories) -> None:
    first = add_face(repos, "1", [1.0, 0.0, 0.0])
    second = add_face(repos, "2", [0.99, 0.01, 0.0])
    singleton = add_face(repos, "3", [0.0, 1.0, 0.0])

    result = FaceClusterer(repos).run(threshold=0.9, min_cluster_size=2)
    assert result.clusters == 1
    assert result.clustered_faces == 2
    assert result.singletons_held_back == 1
    suggestion = repos.identities.cluster_review_queue()[0]
    assert suggestion["size"] == 2

    named = repos.identities.name_cluster(int(suggestion["id"]), "Ada Example")
    assert named["regions_confirmed"] == 2
    links = repos.db.query(
        "SELECT region_id, confirmed, source FROM region_identity ORDER BY region_id"
    )
    assert {(row["region_id"], row["confirmed"], row["source"]) for row in links} == {
        (first, 1, "user"),
        (second, 1, "user"),
    }
    assert all(int(row["region_id"]) != singleton for row in links)


def test_user_links_known_and_arbitrary_profile_platforms(repos: Repositories) -> None:
    identity_id = repos.identities.create_identity("Profile Person")
    provider, handle, url = resolve_profile_link("github", handle="octocat")
    linked = repos.identity_profiles.add_profile(
        identity_id,
        provider=provider,
        handle=handle,
        profile_url=url,
    )
    assert linked["profile_url"] == "https://github.com/octocat"
    assert linked["user_confirmed"] == 1

    provider, handle, url = resolve_profile_link(
        "custom", profile_url="https://profiles.example.test/person"
    )
    repos.identity_profiles.add_profile(
        identity_id,
        provider=provider,
        handle=handle,
        profile_url=url,
    )
    assert len(repos.identity_profiles.profiles(identity_id)) == 2

    with pytest.raises(ValueError, match="recognized GitHub domain"):
        resolve_profile_link("github", profile_url="https://lookalike.example/octocat")
    with pytest.raises(ValueError, match="HTTPS"):
        resolve_profile_link("custom", profile_url="http://example.test/person")


def test_wikipedia_search_then_explicit_biography_selection(repos: Repositories) -> None:
    def transport(url: str) -> dict[str, Any]:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if params.get("list") == ["search"]:
            return {
                "query": {
                    "search": [
                        {
                            "pageid": 42,
                            "title": "Ada Example",
                            "description": "Example researcher",
                            "snippet": "An <span>example</span> biography",
                        }
                    ]
                }
            }
        return {
            "query": {
                "pages": [
                    {
                        "pageid": 42,
                        "title": "Ada Example",
                        "fullurl": "https://en.wikipedia.org/wiki/Ada_Example",
                        "extract": "Ada Example is an example researcher.",
                        "pageprops": {"wikibase_item": "Q42"},
                        "revisions": [{"revid": 123}],
                    }
                ]
            }
        }

    client = WikipediaClient(transport=transport)
    candidates = client.search("Ada Example")
    assert candidates[0]["page_title"] == "Ada Example"
    assert candidates[0]["snippet"] == "An example biography"

    biography = client.biography(candidates[0]["page_title"])
    identity_id = repos.identities.create_identity("Ada Example")
    stored = repos.identity_profiles.save_biography(identity_id, biography)
    assert stored["source_revision"] == 123
    assert stored["wikibase_item"] == "Q42"
    assert stored["summary"].startswith("Ada Example")


def test_biometric_purge_removes_profiles_and_biographies(repos: Repositories) -> None:
    identity_id = repos.identities.create_identity("Delete Me")
    repos.identity_profiles.add_profile(
        identity_id,
        provider="custom",
        handle=None,
        profile_url="https://example.test/delete-me",
    )
    repos.identity_profiles.save_biography(
        identity_id,
        {
            "page_title": "Delete Me",
            "source_url": "https://en.wikipedia.org/wiki/Delete_Me",
            "summary": "Test summary",
            "language": "en",
        },
    )
    counts = repos.identities.purge_biometrics()
    assert counts["identity_profiles"] == 1
    assert counts["identity_biographies"] == 1
