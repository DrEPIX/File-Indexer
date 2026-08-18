"""The searchable vocabulary contributed by installed filter packs.

Packs make search better in a way the facet panel alone cannot: they know that
"footy" and "soccer" mean the label ``football``, and that "vod" means
``stream-vod``. Feeding those aliases into the text query as alternatives means
typing what you actually call a thing finds it, without the user learning the
pack's canonical spelling.

Expansion is additive on purpose. An alias widens an FTS query rather than
converting it into a hard label filter, so a term that happens to match a pack
alias can never *reduce* a result set to nothing on a library where that pack
has not run yet.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .pack import FilterPack

__all__ = ["BASE_SYNONYMS", "Vocabulary", "build_vocabulary", "normalize_alias"]

#: Synonyms that hold regardless of which packs are installed. Kept short and
#: uncontroversial; anything domain-specific belongs in a pack.
BASE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "photo": ("photo", "image", "picture", "pic"),
    "image": ("photo", "image", "picture", "pic"),
    "picture": ("photo", "image", "picture", "pic"),
    "pic": ("photo", "image", "picture", "pic"),
    "video": ("video", "clip", "movie", "footage"),
    "clip": ("video", "clip", "footage"),
    "footage": ("video", "clip", "footage"),
    "doc": ("doc", "document", "pdf"),
    "document": ("doc", "document", "pdf"),
    "screenshot": ("screenshot", "screen", "capture", "screencap"),
    "screencap": ("screenshot", "screen", "capture", "screencap"),
    "selfie": ("selfie", "portrait", "self portrait"),
    "nsfw": ("nsfw", "flagged", "explicit", "adult"),
    "sfw": ("sfw", "safe"),
}


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """Term expansions and label lookups from the installed packs."""

    #: lowercase term -> every term that should also be searched for.
    expansions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: lowercase term -> ``(namespace, label)``, for the "did you mean a
    #: filter?" affordance and for resolving ``sport:footy``.
    labels: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    #: short namespace alias -> full namespace, so ``sport:football`` works as
    #: well as ``content.sport:football``.
    namespaces: Mapping[str, str] = field(default_factory=dict)

    def expand(self, term: str) -> tuple[str, ...]:
        """Every spelling worth searching for, given one typed term."""
        return self.expansions.get(term.strip().lower(), ())

    def resolve_label(self, term: str) -> tuple[str, str] | None:
        return self.labels.get(term.strip().lower())

    def resolve_namespace(self, term: str) -> str | None:
        return self.namespaces.get(term.strip().lower())

    def is_empty(self) -> bool:
        return not self.expansions and not self.labels


_PARENTHETICAL = re.compile(r"\([^)]*\)")
_PUNCTUATION = re.compile(r"[^0-9a-z]+")


def normalize_alias(value: str) -> str:
    """Reduce a human-facing spelling to something FTS can actually match.

    Display titles carry decoration — ``Football (soccer)`` — that becomes a
    dead phrase query if it reaches the index verbatim, because the index
    contains neither the brackets nor that word order.
    """
    stripped = _PARENTHETICAL.sub(" ", value.lower())
    return " ".join(part for part in _PUNCTUATION.split(stripped) if part)


def _merge(target: dict[str, set[str]], terms: Iterable[str]) -> None:
    """Make every term in a group find every other term in that group."""
    group = {normalize_alias(term) for term in terms if term and term.strip()}
    group.discard("")
    if len(group) < 2:
        return
    for term in group:
        target.setdefault(term, set()).update(group)


def build_vocabulary(packs: Iterable[FilterPack]) -> Vocabulary:
    """Compile the search vocabulary contributed by a set of packs."""
    expansions: dict[str, set[str]] = {}
    for term, group in BASE_SYNONYMS.items():
        _merge(expansions, [term, *group])

    labels: dict[str, tuple[str, str]] = {}
    namespaces: dict[str, str] = {}
    for pack in packs:
        namespaces.setdefault(pack.namespace, pack.namespace)
        # The last dotted segment is the natural short name: `content.sport`
        # answers to `sport`. First pack to claim it wins, so a later pack
        # cannot silently steal an established shorthand.
        short = pack.namespace.rsplit(".", 1)[-1]
        namespaces.setdefault(short, pack.namespace)
        # The facet's human title is also addressable, so the word shown above
        # a filter group is the word that works in the search box.
        title = normalize_alias(pack.facet_title)
        if title:
            namespaces.setdefault(title, pack.namespace)
        for label in pack.labels:
            raw = {label.name, label.title.lower(), *label.aliases}
            spellings = {normalize_alias(value) for value in raw if value.strip()}
            spellings.discard("")
            _merge(expansions, spellings)
            # The canonical name always resolves, even if normalisation
            # collapsed it (a hyphenated label like `stream-vod`).
            for spelling in {*spellings, label.name}:
                labels.setdefault(spelling, (pack.namespace, label.name))

    return Vocabulary(
        expansions={term: tuple(sorted(group)) for term, group in expansions.items()},
        labels=labels,
        namespaces=namespaces,
    )
