"""Scoped and recoverable analyzer sweeps.

An analyzer that accepts every media type used to be an all-or-nothing
commitment: one click queued a slow model against the entire library with no
way to narrow it and no way to revive work that failed while the model server
was down. Both are what made a working integration look like a hang.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from mediaengine.config import Config
from mediaengine.db.repositories import Repositories
from mediaengine.engine import MediaEngine
from mediaengine.plugins.contract import Annotation, PluginInfo
from mediaengine.plugins.registry import LoadedPlugin
from mediaengine.plugins.runner import PluginRunner
from mediaengine.types import TaskState

from .conftest import make_config, write_jpeg


class _Everything:
    """An analyzer that accepts every media type, like the LM Studio bridge."""

    info = PluginInfo(
        id="test.everything",
        version="1.0.0",
        accepts=("image", "video", "audio", "document", "other"),
        emits=("test.label",),
    )

    def analyze(self, ctx: Any) -> Sequence[Annotation]:
        return [Annotation(namespace="test.label", label="seen")]


def _loaded() -> LoadedPlugin:
    analyzer = _Everything()
    return LoadedPlugin(info=analyzer.info, analyzer=analyzer, enabled=True)


@pytest.fixture()
def seeded(tmp_path: Path) -> tuple[Config, Repositories, MediaEngine]:
    root = tmp_path / "media"
    root.mkdir()
    write_jpeg(root / "photo.jpg")
    # A real ISO-BMFF header, so media-type detection sniffs "video" from the
    # bytes rather than trusting the extension.
    (root / "clip.mp4").write_bytes(
        b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 2048
    )
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    config = make_config(tmp_path, root)
    engine = MediaEngine(config).start()
    engine.scan([root], generate_derivatives=False)
    yield config, engine.repos, engine
    engine.close()


def _runner(seeded: tuple[Config, Repositories, MediaEngine]) -> PluginRunner:
    config, repos, engine = seeded
    return PluginRunner(config, repos, engine.plugins)


def _by_media_type(repos: Repositories, plugin_id: str) -> dict[str, int]:
    rows = repos.db.query(
        "SELECT a.media_type, COUNT(*) n FROM analysis_tasks t "
        "JOIN assets a ON a.id = t.asset_id WHERE t.plugin_id = ? GROUP BY a.media_type",
        [plugin_id],
    )
    return {str(row["media_type"]): int(row["n"]) for row in rows}


def test_unscoped_enqueue_covers_everything_the_plugin_accepts(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    _, repos, _ = seeded
    _runner(seeded).enqueue_backfill([_loaded()])
    counts = _by_media_type(repos, "test.everything")
    assert set(counts) >= {"image", "video"}


def test_scoping_to_video_queues_only_video(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    _, repos, _ = seeded
    _runner(seeded).enqueue_backfill([_loaded()], media_types=["video"])
    assert set(_by_media_type(repos, "test.everything")) == {"video"}


def test_scoping_is_still_idempotent(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    """Re-running a scoped sweep must stay free, like the unscoped one."""
    runner = _runner(seeded)
    first = runner.enqueue_backfill([_loaded()], media_types=["video"])
    second = runner.enqueue_backfill([_loaded()], media_types=["video"])
    assert first >= 1
    assert second == 0


def test_widening_the_scope_later_adds_the_rest(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    _, repos, _ = seeded
    runner = _runner(seeded)
    runner.enqueue_backfill([_loaded()], media_types=["video"])
    runner.enqueue_backfill([_loaded()])
    assert set(_by_media_type(repos, "test.everything")) >= {"image", "video"}


def test_a_scope_the_plugin_rejects_queues_nothing(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    class OnlyImages(_Everything):
        info = PluginInfo(id="test.images", version="1", accepts=("image",), emits=("test.label",))

    analyzer = OnlyImages()
    plugin = LoadedPlugin(info=analyzer.info, analyzer=analyzer, enabled=True)
    assert _runner(seeded).enqueue_backfill([plugin], media_types=["video"]) == 0


def test_failed_tasks_can_be_revived_after_the_cause_is_fixed(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    """A model server that was down leaves work that is only *temporarily* dead."""
    _, repos, _ = seeded
    runner = _runner(seeded)
    runner.enqueue_backfill([_loaded()], media_types=["video"])
    claimed = repos.tasks.claim(plugin_ids=["test.everything"], limit=5)
    assert claimed
    for task in claimed:
        repos.tasks.complete(int(task["id"]), TaskState.FAILED, error="LM Studio was not running")
    assert repos.tasks.pending_count("test.everything") == 0

    revived = repos.tasks.reset_failed("test.everything")
    assert revived == len(claimed)
    assert repos.tasks.pending_count("test.everything") == len(claimed)


def test_backfill_result_reports_what_it_revived(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    from mediaengine.plugins.runner import BackfillResult

    payload = BackfillResult(enqueued=3, retried=2).as_dict()
    assert payload["retried"] == 2
    assert payload["enqueued"] == 3


def test_bulk_label_reads_group_by_asset(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    """The grid needs every tile's labels in one query, not one query per tile."""
    _, repos, _ = seeded
    ids = repos.assets.iter_asset_ids(limit=10)
    assert len(ids) >= 2
    repos.annotations.add_user_annotation(ids[0], "user.label", "favorite")
    repos.annotations.add_user_annotation(ids[1], "user.label", "keep")

    labels = repos.annotations.live_labels(ids, namespaces=["user.label"])
    assert [row["label"] for row in labels[ids[0]]] == ["favorite"]
    assert [row["label"] for row in labels[ids[1]]] == ["keep"]


def test_bulk_label_reads_respect_the_per_asset_cap(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    _, repos, _ = seeded
    asset_id = repos.assets.iter_asset_ids(limit=1)[0]
    for index in range(6):
        repos.annotations.add_user_annotation(asset_id, "user.label", f"tag{index}")
    assert len(repos.annotations.live_labels([asset_id], per_asset=3)[asset_id]) == 3


def test_bulk_label_reads_tolerate_an_empty_request(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    _, repos, _ = seeded
    assert repos.annotations.live_labels([]) == {}


def test_bulk_label_reads_filter_by_namespace(
    seeded: tuple[Config, Repositories, MediaEngine],
) -> None:
    _, repos, _ = seeded
    asset_id = repos.assets.iter_asset_ids(limit=1)[0]
    repos.annotations.add_user_annotation(asset_id, "user.label", "favorite")
    assert repos.annotations.live_labels([asset_id], namespaces=["llm"]) == {}
