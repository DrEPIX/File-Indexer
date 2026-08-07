"""Glob matching for library include/exclude patterns.

Python's :func:`fnmatch.fnmatch` has no concept of a path separator, so
``*.jpg`` there also matches ``a/b.jpg`` and ``**`` means nothing at all.
:meth:`pathlib.PurePath.match` only gained full ``**`` support in 3.13, and the
engine targets 3.11. Both would silently mis-scope a user's exclude list, which
is the kind of bug that shows up as "why is my `node_modules` in the library".

So patterns are compiled to regexes here, with the semantics people expect from
`.gitignore` and `rsync`:

===================  ==========================================================
``*``                any run of characters except ``/``
``?``                exactly one character except ``/``
``[abc]``/``[!abc]`` a character class
``**/``              zero or more directory levels
``**``               anything, separators included
===================  ==========================================================

Paths are matched in POSIX form (forward slashes) relative to the library root,
so one config file works unchanged on Windows and Linux.
"""

from __future__ import annotations

import os.path
import re
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

__all__ = ["GlobMatcher", "compile_glob", "to_relative_posix"]

# normcase lowercases on Windows and is the identity elsewhere, which is
# precisely the case-sensitivity distinction wanted — and it needs no probe
# write to the filesystem to find out.
_FS_CASE_SENSITIVE: bool = os.path.normcase("A") == "A"


def _translate(pattern: str) -> str:
    """Convert one glob pattern to an anchored regular expression."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**/", i):
                # Zero or more whole directory levels. `+` inside, not `*`, so
                # the pattern can never match a doubled separator.
                out.append("(?:[^/]+/)*")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        if char == "[":
            close = i + 1
            if close < n and pattern[close] in "!^":
                close += 1
            if close < n and pattern[close] == "]":
                close += 1
            while close < n and pattern[close] != "]":
                close += 1
            if close >= n:  # unterminated class: treat as a literal bracket
                out.append(re.escape(char))
                i += 1
                continue
            body = pattern[i + 1 : close].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = close + 1
            continue
        out.append(re.escape(char))
        i += 1
    return "(?s:" + "".join(out) + r")\Z"


def compile_glob(pattern: str, *, case_sensitive: bool | None = None) -> re.Pattern[str]:
    """Compile a single glob to a regex.

    ``case_sensitive`` defaults to the host filesystem's convention: NTFS and
    APFS are case-insensitive in practice, so a Windows user writing
    ``**/*.JPG`` expects it to catch ``photo.jpg``.
    """
    if case_sensitive is None:
        case_sensitive = _FS_CASE_SENSITIVE
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(_translate(pattern.replace("\\", "/")), flags)


def to_relative_posix(path: Path, root: Path) -> str:
    """Render ``path`` relative to ``root`` with forward slashes.

    Falls back to the absolute POSIX form when ``path`` is outside ``root``,
    which happens when a symlink escapes the tree; matching an absolute path
    against a relative pattern simply fails, which is the safe outcome.
    """
    try:
        return PurePosixPath(path.relative_to(root).as_posix()).as_posix()
    except ValueError:
        return path.as_posix()


class GlobMatcher:
    """Compiled include/exclude sets for one library root.

    Two rules that are easy to get wrong and are handled here:

    * **deny wins.** A path matching both lists is excluded.
    * **a directory pattern prunes the directory.** ``**/node_modules/**``
      describes the *contents*, but the walker needs to know not to descend in
      the first place, so a copy of every pattern with a trailing ``/**``
      removed is kept for directory tests. Pruning at the directory saves
      walking a tree that would be discarded file by file.
    """

    __slots__ = ("_includes", "_excludes", "_dir_excludes", "include_all")

    def __init__(
        self,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        *,
        case_sensitive: bool | None = None,
    ) -> None:
        includes = list(include or ["**/*"])
        excludes = list(exclude or [])
        self.include_all = includes in ([], ["**/*"], ["**"])
        self._includes = [compile_glob(p, case_sensitive=case_sensitive) for p in includes]
        self._excludes = [compile_glob(p, case_sensitive=case_sensitive) for p in excludes]
        dir_patterns: list[str] = []
        for pattern in excludes:
            cleaned = pattern.replace("\\", "/")
            dir_patterns.append(cleaned)
            if cleaned.endswith("/**"):
                dir_patterns.append(cleaned[:-3])
            elif cleaned.endswith("/*"):
                dir_patterns.append(cleaned[:-2])
        self._dir_excludes = [
            compile_glob(p, case_sensitive=case_sensitive) for p in dict.fromkeys(dir_patterns)
        ]

    def matches_file(self, rel_posix: str) -> bool:
        """Whether a file at this root-relative path should be indexed."""
        if any(rx.match(rel_posix) for rx in self._excludes):
            return False
        if self.include_all:
            return True
        return any(rx.match(rel_posix) for rx in self._includes)

    def excludes_dir(self, rel_posix: str) -> bool:
        """Whether the walker should refuse to descend into this directory."""
        return any(rx.match(rel_posix) for rx in self._dir_excludes)

    def filter(self, rel_paths: Iterable[str]) -> list[str]:
        """Convenience for tests and the ``scan --dry-run`` path."""
        return [p for p in rel_paths if self.matches_file(p)]
