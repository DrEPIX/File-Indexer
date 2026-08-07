"""Glob semantics. These decide what does and does not enter the library."""

from __future__ import annotations

from pathlib import Path

from mediaengine.core.globs import GlobMatcher, compile_glob, to_relative_posix


class TestTranslation:
    def test_star_does_not_cross_separators(self) -> None:
        pattern = compile_glob("*.jpg", case_sensitive=True)
        assert pattern.match("a.jpg")
        assert not pattern.match("dir/a.jpg")

    def test_doubled_star_slash_spans_zero_directories(self) -> None:
        # The case everyone gets wrong: `**/` must match nothing at all, or
        # `**/*.jpg` fails to match a file sitting in the root.
        pattern = compile_glob("**/*.jpg", case_sensitive=True)
        assert pattern.match("a.jpg")
        assert pattern.match("one/a.jpg")
        assert pattern.match("one/two/three/a.jpg")

    def test_doubled_star_alone_crosses_separators(self) -> None:
        pattern = compile_glob("cache/**", case_sensitive=True)
        assert pattern.match("cache/a/b/c.txt")
        assert not pattern.match("other/a.txt")

    def test_question_mark_is_one_non_separator(self) -> None:
        pattern = compile_glob("IMG_???.jpg", case_sensitive=True)
        assert pattern.match("IMG_001.jpg")
        assert not pattern.match("IMG_0001.jpg")
        assert not pattern.match("IMG_a/b.jpg")

    def test_character_classes_including_negation(self) -> None:
        assert compile_glob("[abc].txt", case_sensitive=True).match("b.txt")
        assert not compile_glob("[!abc].txt", case_sensitive=True).match("b.txt")
        assert compile_glob("[!abc].txt", case_sensitive=True).match("d.txt")

    def test_unterminated_bracket_is_a_literal(self) -> None:
        # A user writing a Windows path by hand produces these. It must not
        # raise re.error and kill the scan.
        pattern = compile_glob("weird[name.txt", case_sensitive=True)
        assert pattern.match("weird[name.txt")

    def test_backslashes_are_normalised(self) -> None:
        pattern = compile_glob("photos\\**\\*.jpg", case_sensitive=True)
        assert pattern.match("photos/2019/a.jpg")

    def test_anchored_at_both_ends(self) -> None:
        pattern = compile_glob("*.jpg", case_sensitive=True)
        assert not pattern.match("a.jpg.bak")


class TestMatcher:
    def test_deny_beats_allow(self) -> None:
        matcher = GlobMatcher(["**/*.jpg"], ["**/private/**"])
        assert matcher.matches_file("holiday/a.jpg")
        assert not matcher.matches_file("private/a.jpg")

    def test_directory_pattern_prunes_the_directory_itself(self) -> None:
        # `**/node_modules/**` describes the contents, but the walker needs to
        # know not to descend. Without the stripped directory form it would
        # walk the whole tree and reject each file individually.
        matcher = GlobMatcher(["**/*"], ["**/node_modules/**"])
        assert matcher.excludes_dir("node_modules")
        assert matcher.excludes_dir("src/node_modules")
        assert not matcher.excludes_dir("src")

    def test_dotfile_exclusion_prunes_dot_directories(self) -> None:
        matcher = GlobMatcher(["**/*"], ["**/.*"])
        assert matcher.excludes_dir(".git")
        assert not matcher.matches_file(".env")
        assert matcher.matches_file("visible.txt")

    def test_include_all_shortcut_still_honours_excludes(self) -> None:
        matcher = GlobMatcher(["**/*"], ["**/*.tmp"])
        assert matcher.include_all
        assert matcher.matches_file("a/b/c.jpg")
        assert not matcher.matches_file("a/b/c.tmp")

    def test_empty_include_means_everything(self) -> None:
        assert GlobMatcher([], []).matches_file("anything/at/all.bin")

    def test_case_insensitivity_is_selectable(self) -> None:
        assert GlobMatcher(["**/*.jpg"], [], case_sensitive=False).matches_file("A.JPG")
        assert not GlobMatcher(["**/*.jpg"], [], case_sensitive=True).matches_file("A.JPG")

    def test_filter_helper(self) -> None:
        matcher = GlobMatcher(["**/*.jpg"], ["**/skip/**"])
        assert matcher.filter(["a.jpg", "skip/b.jpg", "c.png"]) == ["a.jpg"]


class TestRelativePosix:
    def test_uses_forward_slashes(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c.jpg"
        assert to_relative_posix(target, tmp_path) == "a/b/c.jpg"

    def test_path_outside_root_falls_back_to_absolute(self, tmp_path: Path) -> None:
        # Happens when a symlink escapes the tree. An absolute path simply
        # fails to match relative patterns, which is the safe outcome.
        outside = tmp_path.parent / "elsewhere.jpg"
        assert to_relative_posix(outside, tmp_path).endswith("elsewhere.jpg")
