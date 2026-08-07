"""The query model, and the one-line syntax that produces it.

A query is free text plus structured filters. The structured side is
deliberately generic: a filter is a ``namespace[:label]`` pair matched against
the ``annotations`` table, so the moment any local analyzer starts emitting
``color.dominant=blue`` or ``animal.species=dog``, those become filterable with
no code change here. The core never learns what the namespaces mean.

The string syntax exists so the CLI, the web UI's search box and the API all
parse identically::

    beach sunset type:image color.dominant:blue camera:"Canon" after:2019
    conf:>=0.7 has:gps sort:captured

Reserved keys (``type:``, ``camera:``, ``after:``…) are engine concepts.
**Any other ``key:value`` token is an annotation filter** — that is the rule
that keeps plugin vocabularies first-class citizens of the search box.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..types import MEDIA_TYPE_VALUES
from ..util import parse_datetime, to_iso

__all__ = ["LabelFilter", "Query", "parse_query"]

_TOKEN = re.compile(
    r"""
    (?P<key>[A-Za-z0-9._-]+) : (?P<quoted>"[^"]*"|'[^']*') |   # key:"quoted value"
    (?P<key2>[A-Za-z0-9._-]+) : (?P<bare>[^\s"']+) |           # key:bare
    (?P<quoted_text>"[^"]*"|'[^']*') |                          # "quoted text"
    (?P<word>\S+)                                               # plain word
    """,
    re.VERBOSE,
)

_SORT_ALIASES = {
    "captured": "captured_at",
    "captured_at": "captured_at",
    "date": "captured_at",
    "imported": "imported_at",
    "imported_at": "imported_at",
    "size": "size_bytes",
    "relevance": "relevance",
}


@dataclass(slots=True)
class LabelFilter:
    """One structured condition against the annotations table.

    ``label=None`` means "any live annotation in this namespace", which is how
    ``has:gps``-style presence queries work for arbitrary plugin namespaces.
    """

    namespace: str
    label: str | None = None
    min_confidence: float | None = None

    def key(self) -> str:
        return f"{self.namespace}:{self.label}" if self.label else self.namespace


@dataclass(slots=True)
class Query:
    """Everything the planner needs to answer one search."""

    text: str = ""
    media_types: list[str] = field(default_factory=list)
    labels: list[LabelFilter] = field(default_factory=list)
    """ANDed. Clicking two facet values narrows; OR-groups are a UI concern."""

    excluded_labels: list[LabelFilter] = field(default_factory=list)
    """Annotations that must not exist on a matching asset."""

    sources: list[str] = field(default_factory=list)
    min_confidence: float | None = None
    """Applied to every label filter that does not carry its own."""

    camera: str | None = None
    """Substring-matched against make and model both; nobody remembers which
    half of "Canon EOS R5" lives in which column."""

    captured_after: str | None = None
    captured_before: str | None = None
    has_location: bool | None = None
    extension: str | None = None

    sort: str = "relevance"
    """``relevance`` degrades to newest-first when there is no text to rank."""

    descending: bool = True
    limit: int = 50
    offset: int = 0

    def is_empty(self) -> bool:
        """True when this would just page through the whole library."""
        return not (
            self.text
            or self.media_types
            or self.labels
            or self.excluded_labels
            or self.camera
            or self.captured_after
            or self.captured_before
            or self.has_location is not None
            or self.extension
        )


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_confidence(raw: str) -> float | None:
    cleaned = raw.lstrip(">=").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if 0.0 <= value <= 1.0 else None


def _parse_date_boundary(raw: str, *, end: bool) -> str | None:
    """``2019`` → the start (or end) of 2019, ``2019-06`` → that month, etc.

    A user typing ``before:2020`` means "before 2020 began". Expanding the
    shorthand here keeps the planner a dumb string comparison, which the
    ISO-8601-with-Z storage format makes correct.
    """
    text = raw.strip()
    if re.fullmatch(r"\d{4}", text):
        text = f"{text}-12-31 23:59:59" if end else f"{text}-01-01 00:00:00"
    elif re.fullmatch(r"\d{4}-\d{2}", text):
        text = f"{text}-28 23:59:59" if end else f"{text}-01 00:00:00"
    parsed, _ = parse_datetime(text)
    return to_iso(parsed)


def parse_query(raw: str, *, limit: int = 50, offset: int = 0) -> Query:
    """Turn the shared one-line syntax into a :class:`Query`."""
    query = Query(limit=limit, offset=offset)
    words: list[str] = []

    for match in _TOKEN.finditer(raw or ""):
        if match.group("quoted_text"):
            words.append(_unquote(match.group("quoted_text")))
            continue
        if match.group("word"):
            words.append(match.group("word"))
            continue

        key = (match.group("key") or match.group("key2") or "").lower()
        value = _unquote(match.group("quoted") or match.group("bare") or "")
        if not value:
            continue

        if key == "type":
            for part in value.lower().split(","):
                if part in MEDIA_TYPE_VALUES:
                    query.media_types.append(part)
        elif key == "camera":
            query.camera = value
        elif key == "ext":
            query.extension = value.lower().lstrip(".")
        elif key == "after":
            query.captured_after = _parse_date_boundary(value, end=False)
        elif key == "before":
            query.captured_before = _parse_date_boundary(value, end=True)
        elif key == "conf":
            query.min_confidence = _parse_confidence(value)
        elif key == "source":
            for part in value.lower().split(","):
                if part in ("embedded", "derived", "user"):
                    query.sources.append(part)
        elif key == "has":
            if value.lower() in ("gps", "location", "geo"):
                query.has_location = True
            else:
                # `has:color.dominant` — presence of any label in a namespace.
                query.labels.append(LabelFilter(namespace=value))
        elif key == "not":
            if value.lower() in ("gps", "location", "geo"):
                query.has_location = False
            else:
                # `not:safety.nsfw:flagged` and
                # `not:safety.nsfw=flagged` are equivalent. With no label,
                # the whole namespace is excluded.
                separator = "=" if "=" in value else ":" if ":" in value else None
                if separator:
                    namespace, label = value.rsplit(separator, 1)
                    query.excluded_labels.append(LabelFilter(namespace=namespace, label=label))
                else:
                    query.excluded_labels.append(LabelFilter(namespace=value))
        elif key == "nsfw":
            mode = value.lower()
            if mode in ("safe", "hide", "false", "no"):
                # Strict opt-in filtering: unrated assets are omitted too,
                # because claiming that unprocessed media is safe would be a lie.
                query.labels.append(LabelFilter(namespace="safety.nsfw", label="safe"))
            elif mode in ("only", "flagged", "true", "yes"):
                query.labels.append(LabelFilter(namespace="safety.nsfw", label="flagged"))
            elif mode == "review":
                query.labels.append(LabelFilter(namespace="safety.nsfw", label="review"))
        elif key == "sort":
            direction_down = True
            name = value.lower()
            if name.startswith(("+", "-")):
                direction_down = name.startswith("-")
                name = name[1:]
            query.sort = _SORT_ALIASES.get(name, "relevance")
            query.descending = direction_down
        else:
            # The default case IS the feature: an unreserved key is a plugin
            # namespace. `color.dominant:blue` filters on an annotation the
            # core has never heard of.
            query.labels.append(LabelFilter(namespace=key, label=value))

    query.text = " ".join(words).strip()
    return query
