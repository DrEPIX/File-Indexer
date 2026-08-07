"""Traversal, filtering and — the important one — resume semantics."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mediaengine.config import LibraryConfig
from mediaengine.core.control import CancelToken
from mediaengine.core.walker import BatchProducer, Walker
from mediaengine.errors import OperationCancelled


def build_tree(root: Path) -> None:
    """A tree with enough shape to exercise depth, order and pruning."""
    (root / "a" / "aa").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    (root / ".hidden").mkdir(parents=True)
    (root / "node_modules" / "deep").mkdir(parents=True)

    (root / "top.txt").write_text("top")
    (root / "a" / "one.txt").write_text("one")
    (root / "a" / "aa" / "two.txt").write_text("two")
    (root / "b" / "three.txt").write_text("three")
    (root / ".hidden" / "secret.txt").write_text("secret")
    (root / ".dotfile").write_text("dot")
    (root / "node_modules" / "deep" / "junk.txt").write_text("junk")


def names(walker: Walker, root: Path, **kwargs: object) -> list[str]:
    return [e.path.name for e in walker.iter_entries(root, **kwargs)]  # type: ignore[arg-type]


class TestFiltering:
    def test_default_excludes_prune_hidden_and_vendor_directories(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        walker = Walker(LibraryConfig(min_file_size=0))
        found = names(walker, tmp_path)
        assert set(found) == {"top.txt", "one.txt", "two.txt", "three.txt"}
        assert "secret.txt" not in found
        assert "junk.txt" not in found

    def test_pruning_a_directory_avoids_walking_it(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        walker = Walker(LibraryConfig(min_file_size=0))
        list(walker.iter_entries(tmp_path))
        # root, a, a/aa, b — but never node_modules or its child.
        assert walker.stats.directories == 4

    def test_include_hidden_opt_in(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        walker = Walker(
            LibraryConfig(include_hidden=True, min_file_size=0, exclude=["**/node_modules/**"])
        )
        assert "secret.txt" in names(walker, tmp_path)
        assert ".dotfile" in names(walker, tmp_path)

    def test_include_patterns_restrict(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        (tmp_path / "a" / "photo.jpg").write_bytes(b"\xff\xd8\xff")
        walker = Walker(LibraryConfig(include=["**/*.jpg"], min_file_size=0))
        assert names(walker, tmp_path) == ["photo.jpg"]

    def test_min_file_size_skips_zero_byte_files(self, tmp_path: Path) -> None:
        (tmp_path / "empty.bin").write_bytes(b"")
        (tmp_path / "full.bin").write_bytes(b"x")
        walker = Walker(LibraryConfig(min_file_size=1))
        assert names(walker, tmp_path) == ["full.bin"]
        assert walker.stats.skipped_small == 1

    def test_max_depth_zero_means_root_only(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        walker = Walker(LibraryConfig(max_depth=0, min_file_size=0))
        assert names(walker, tmp_path) == ["top.txt"]

    def test_max_depth_one_descends_a_single_level(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        walker = Walker(LibraryConfig(max_depth=1, min_file_size=0))
        found = set(names(walker, tmp_path))
        assert found == {"top.txt", "one.txt", "three.txt"}

    def test_a_missing_root_is_recorded_not_raised(self, tmp_path: Path) -> None:
        walker = Walker(LibraryConfig())
        assert list(walker.iter_entries(tmp_path / "nope")) == []
        assert walker.stats.errors == 1


class TestOrdering:
    def test_traversal_is_deterministic(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        config = LibraryConfig(min_file_size=0)
        first = [str(e.path) for e in Walker(config).iter_entries(tmp_path)]
        second = [str(e.path) for e in Walker(config).iter_entries(tmp_path)]
        # The whole resume mechanism rests on this.
        assert first == second

    def test_a_directory_is_visited_before_its_children(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        order = [str(e.path.parent) for e in Walker(LibraryConfig(min_file_size=0)).iter_entries(tmp_path)]
        assert order.index(str(tmp_path / "a")) < order.index(str(tmp_path / "a" / "aa"))


class TestBatching:
    def test_batches_are_capped_and_lossless(self, tmp_path: Path) -> None:
        for index in range(25):
            (tmp_path / f"f{index:03d}.txt").write_text("x")
        walker = Walker(LibraryConfig(min_file_size=0))
        batches = list(walker.iter_batches(tmp_path, batch_size=10))
        assert all(len(b) <= 10 for b in batches)
        assert sum(len(b) for b in batches) == 25

    def test_a_batch_carries_a_resume_cursor(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        batches = list(Walker(LibraryConfig(min_file_size=0)).iter_batches(tmp_path, batch_size=2))
        assert any(b.cursor for b in batches)

    def test_empty_tree_yields_nothing_harmful(self, tmp_path: Path) -> None:
        (tmp_path / "only_a_dir").mkdir()
        batches = list(Walker(LibraryConfig(min_file_size=0)).iter_batches(tmp_path))
        assert sum(len(b) for b in batches) == 0


class TestResume:
    def test_resuming_after_a_directory_skips_only_what_preceded_it(self, tmp_path: Path) -> None:
        build_tree(tmp_path)
        config = LibraryConfig(min_file_size=0)
        full = [str(e.path) for e in Walker(config).iter_entries(tmp_path)]

        # Resume after the root directory itself.
        resumed = [
            str(e.path)
            for e in Walker(config).iter_entries(tmp_path, resume_after=str(tmp_path))
        ]
        assert str(tmp_path / "top.txt") in full
        assert str(tmp_path / "top.txt") not in resumed
        # Everything below the root must still be walked.
        assert str(tmp_path / "a" / "one.txt") in resumed

    def test_the_cursor_directory_does_not_swallow_its_own_subtree(self, tmp_path: Path) -> None:
        # The bug a naive path-prefix implementation has: resuming after
        # `<root>/a` must not skip `<root>/a/aa`, because a depth-first walk
        # visits the subtree *after* the directory.
        build_tree(tmp_path)
        resumed = [
            e.path.name
            for e in Walker(LibraryConfig(min_file_size=0)).iter_entries(
                tmp_path, resume_after=str(tmp_path / "a")
            )
        ]
        assert "one.txt" not in resumed  # a's own file: already done
        assert "two.txt" in resumed  # a/aa's file: not yet done

    def test_resuming_and_finishing_covers_the_whole_tree_exactly_once(
        self, tmp_path: Path
    ) -> None:
        build_tree(tmp_path)
        config = LibraryConfig(min_file_size=0)
        full = [str(e.path) for e in Walker(config).iter_entries(tmp_path)]

        # Stop after the first directory's worth, then resume from its cursor.
        walker = Walker(config)
        batches = walker.iter_batches(tmp_path, batch_size=1)
        first = next(batches)
        cursor = first.cursor
        assert cursor is not None
        done = [str(e.path) for e in first.entries]
        batches.close()

        rest = [str(e.path) for e in Walker(config).iter_entries(tmp_path, resume_after=cursor)]
        assert sorted(done + rest) == sorted(full)
        assert len(set(done) & set(rest)) == 0

    def test_a_vanished_cursor_restarts_rather_than_indexing_nothing(
        self, tmp_path: Path
    ) -> None:
        # A scan that silently finds zero files looks like success and is not.
        build_tree(tmp_path)
        walker = Walker(LibraryConfig(min_file_size=0))
        found = names(walker, tmp_path, resume_after=str(tmp_path / "deleted-since"))
        assert set(found) == {"top.txt", "one.txt", "two.txt", "three.txt"}


class TestSafety:
    def test_symlink_loops_do_not_hang_the_walk(self, tmp_path: Path) -> None:
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "file.txt").write_text("x")
        try:
            os.symlink(tmp_path / "real", tmp_path / "real" / "loop", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this machine")

        walker = Walker(LibraryConfig(follow_symlinks=True, min_file_size=0))
        found = names(walker, tmp_path)
        assert found.count("file.txt") >= 1
        assert walker.stats.directories < 10  # not unbounded

    def test_symlinks_are_skipped_when_not_following(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_text("x")
        try:
            os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this machine")
        walker = Walker(LibraryConfig(follow_symlinks=False, min_file_size=0))
        assert names(walker, tmp_path) == ["real.txt"]
        assert walker.stats.skipped_symlink == 1

    def test_cancellation_unwinds_promptly(self, tmp_path: Path) -> None:
        for index in range(50):
            (tmp_path / f"f{index}.txt").write_text("x")
        token = CancelToken()
        walker = Walker(LibraryConfig(min_file_size=0), cancel=token)
        token.cancel("stop")
        with pytest.raises(OperationCancelled):
            list(walker.iter_entries(tmp_path))

    def test_long_and_unicode_filenames_survive(self, tmp_path: Path) -> None:
        (tmp_path / ("x" * 180 + ".txt")).write_text("long")
        (tmp_path / "проверка ünïcødé 文件.txt").write_text("unicode")
        found = names(Walker(LibraryConfig(min_file_size=0)), tmp_path)
        assert len(found) == 2
        assert any("文件" in name for name in found)


class TestBatchProducer:
    def test_queue_is_bounded_so_memory_does_not_track_library_size(
        self, tmp_path: Path
    ) -> None:
        for index in range(200):
            (tmp_path / f"f{index:04d}.txt").write_text("x")
        producer = BatchProducer(
            Walker(LibraryConfig(min_file_size=0)), [tmp_path], batch_size=5, queue_size=2
        )
        producer.start()
        total = 0
        for batch in producer:
            total += len(batch)
            # The walker blocks on a full queue rather than racing ahead, so
            # the queue never exceeds the bound however slow the consumer is.
            assert producer._queue.qsize() <= 2  # noqa: SLF001 - the property under test
        assert total == 200

    def test_walker_failure_surfaces_in_the_consumer(self, tmp_path: Path) -> None:
        class Exploding(Walker):
            def iter_batches(self, *args: object, **kwargs: object):  # type: ignore[override]
                raise RuntimeError("boom")
                yield  # pragma: no cover

        producer = BatchProducer(Exploding(LibraryConfig()), [tmp_path])
        with pytest.raises(RuntimeError, match="boom"):
            list(producer)

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        producer = BatchProducer(Walker(LibraryConfig()), [tmp_path])
        producer.start()
        producer.stop()
        producer.stop()
