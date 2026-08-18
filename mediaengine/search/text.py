"""Turning what a person typed into something FTS5 will answer well.

Two problems live here.

**Filenames are not sentences.** ``IMG_20190407_beachDay.jpg`` and
``S02E11.1080p.WEB-DL.mkv`` carry real search terms welded together by
underscores, camel case, and digit boundaries. The indexer expands them so
"beach", "2019", "s02", and "e11" are all findable, because a library search
box that cannot find a file by a word visibly present in its name reads as
broken no matter how good the ranking is.

**One typed word is not one search term.** A query needs phrases, exclusion,
prefix matching for type-ahead, and synonyms — and it must never raise a
syntax error, because the text came from a human mid-sentence.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

__all__ = [
    "MatchPlan",
    "compile_match",
    "expand_filename",
    "tokenize",
]

#: Characters FTS5 treats as syntax. Anything containing them gets quoted.
_BARE = re.compile(r"[^\s\"'*:^()\-]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DIGIT_EDGE = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_SEPARATORS = re.compile(r"[^0-9A-Za-z]+")
_STOPWORDS = frozenset({"a", "an", "the", "of", "and", "or", "to", "in", "on", "for", "with"})


def expand_filename(name: str, *, max_terms: int = 48) -> str:
    """Return ``name`` plus the terms hidden inside it, space separated.

    The original is kept first so exact-name matches still rank highest; the
    derived terms are appended so a word that was glued to another one is
    still reachable. Duplicate terms are dropped, and the result is capped so
    a pathological filename cannot bloat the index.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return ""
    seen: dict[str, None] = {cleaned: None}
    stem, _, extension = cleaned.rpartition(".")
    if stem:
        seen.setdefault(stem, None)
        seen.setdefault(extension.lower(), None)
    for chunk in _SEPARATORS.split(stem or cleaned):
        if not chunk:
            continue
        seen.setdefault(chunk, None)
        for piece in _CAMEL.split(chunk):
            # Both halves matter: "S02E11" should be findable as "s02" and as
            # "02", because people search for episode codes either way.
            if len(piece) >= 2:
                seen.setdefault(piece, None)
            for part in _DIGIT_EDGE.split(piece):
                if len(part) >= 2:
                    seen.setdefault(part, None)
    terms = [term for term in seen if term]
    return " ".join(terms[:max_terms])


def tokenize(text: str) -> list[str]:
    """Split free text into searchable words, dropping punctuation noise."""
    return [token for token in _SEPARATORS.split(text or "") if token]


@dataclass(slots=True)
class MatchPlan:
    """The FTS expressions to try, in order, for one piece of free text.

    ``strict`` requires every term; ``loose`` requires any of them. Running the
    strict query first and falling back keeps precise queries precise while
    stopping a four-word query from returning nothing at all.
    """

    strict: str = ""
    loose: str = ""
    #: Terms the user explicitly excluded with a leading ``-``.
    excluded: list[str] = field(default_factory=list)
    #: True when the last term was left open for type-ahead.
    prefixed: bool = False

    def __bool__(self) -> bool:
        return bool(self.strict)

    def expressions(self) -> list[str]:
        """Distinct expressions to attempt, most precise first."""
        out = [self.strict]
        if self.loose and self.loose != self.strict:
            out.append(self.loose)
        return out


def _quote(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


def _group(terms: Iterable[str], *, prefix_last: bool = False) -> str:
    """One OR group of alternative spellings, as an FTS5 sub-expression."""
    quoted = [_quote(term) for term in terms if term]
    if not quoted:
        return ""
    if prefix_last:
        quoted = [f"{value}*" for value in quoted]
    if len(quoted) == 1:
        return quoted[0]
    return "(" + " OR ".join(quoted) + ")"


def compile_match(
    text: str,
    *,
    synonyms: Callable[[str], tuple[str, ...]] | None = None,
    prefix_last: bool = True,
) -> MatchPlan:
    """Compile free text into a :class:`MatchPlan` that cannot raise.

    Supported by design, because people already type them:

    ``"exact phrase"``
        Matched as a phrase rather than as separate words.
    ``-word``
        Excluded. Applied as an FTS ``NOT`` so it costs nothing extra.
    ``word OR word``
        An explicit alternative; also produced automatically by ``synonyms``.

    Everything else is a term. Terms are ANDed in :attr:`MatchPlan.strict` and
    ORed in :attr:`MatchPlan.loose`.
    """
    raw = (text or "").strip()
    if not raw:
        return MatchPlan()

    groups: list[str] = []
    excluded: list[str] = []
    pending_or = False
    #: Alternatives behind the most recent plain-word group, and where it sits
    #: in ``groups``. Tracked during the walk rather than re-derived from the
    #: raw string afterwards: the last *token* a person typed is frequently a
    #: quoted phrase or an exclusion, neither of which may be turned into a
    #: type-ahead prefix.
    last_plain: tuple[int, list[str]] | None = None
    quoted_pattern = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')

    for match in quoted_pattern.finditer(raw):
        phrase = match.group(1) if match.group(1) is not None else match.group(2)
        if phrase is not None:
            terms = tokenize(phrase)
            if terms:
                # FTS5 spells a phrase as one quoted string; separate quoted
                # tokens would be ANDed anywhere in the document instead.
                groups.append(_quote(" ".join(terms)))
                last_plain = None
            continue
        word = (match.group(3) or "").strip()
        if not word:
            continue
        if word.upper() == "OR" and groups:
            pending_or = True
            continue
        negated = word.startswith("-") and len(word) > 1
        terms = tokenize(word.lstrip("-"))
        if not terms:
            continue
        if negated:
            excluded.extend(terms)
            last_plain = None
            continue
        alternatives: list[str] = []
        for term in terms:
            alternatives.append(term)
            if synonyms is not None:
                alternatives.extend(synonyms(term))
        # Dedupe while keeping the typed spelling first, so bm25 still favours
        # the word the user actually used.
        ordered = list(dict.fromkeys(alternatives))
        rendered = _group(ordered)
        if not rendered:
            continue
        if pending_or and groups:
            groups[-1] = f"({groups[-1]} OR {rendered})"
            pending_or = False
            # An explicit alternation means the word is finished; prefixing it
            # would silently widen a choice the user already made.
            last_plain = None
        else:
            groups.append(rendered)
            last_plain = (len(groups) - 1, ordered) if len(terms[-1]) >= 2 else None

    if not groups:
        return MatchPlan(excluded=excluded)

    # Type-ahead applies only when the query genuinely ends in a bare word.
    prefixed = False
    if prefix_last and last_plain is not None and last_plain[0] == len(groups) - 1:
        rendered = _group(last_plain[1], prefix_last=True)
        if rendered:
            groups[-1] = rendered
            prefixed = True

    strict = " AND ".join(groups)
    loose = " OR ".join(groups) if len(groups) > 1 else strict
    if excluded:
        # FTS5 spells set difference as the binary `A NOT B`; there is no
        # `AND NOT`, and writing one is a syntax error rather than a no-op.
        tail = " ".join(f"NOT {_quote(term)}" for term in dict.fromkeys(excluded))
        strict = f"({strict}) {tail}"
        loose = f"({loose}) {tail}"
    return MatchPlan(strict=strict, loose=loose, excluded=excluded, prefixed=prefixed)
