"""Filter packs: installable taxonomies, defined as data rather than code.

The core deliberately knows nothing about what tags exist. A *filter pack* is
the user-facing consequence of that: a TOML document naming a namespace and the
labels that live in it, which the engine turns into a real analyzer, real
annotations, and therefore a real search facet — with no Python written and no
core change.

A pack answers three questions:

``namespace``
    Where its claims land. ``content.sport``, ``origin.platform``,
    ``safety.nsfw``. This is what search and facets group by.

``method``
    How labels are decided. ``rules`` matches regular expressions against a
    file's own metadata and costs nothing. ``vision`` shows keyframes to a
    local vision model. ``text`` sends metadata and extracted text to a local
    language model.

``labels``
    The closed vocabulary. Packs are deliberately closed-vocabulary: a facet
    whose values drift every run is not a filter, it is a word cloud.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from ..util import stable_hash

__all__ = [
    "METHODS",
    "TEMPLATE",
    "FilterLabel",
    "FilterPack",
    "FilterRule",
    "load_pack",
    "parse_pack",
]

#: A complete, valid pack. Handed to the assistant when a document it wrote
#: fails validation — teaching the format at the moment of failure costs
#: nothing on the turns where it is not needed, and a worked example corrects
#: far more reliably than a prose description of the same rules.
TEMPLATE = '''\
[pack]
id = "filters.france-landmarks"     # must equal the pack_id you passed
name = "French landmarks"
display_name = "Landmark"           # the facet heading
version = "1.0.0"
namespace = "place.landmark"        # dotted lowercase
method = "vision"                   # rules | vision | text
accepts = ["image", "video"]
threshold = 0.45
description = "Well-known places in France."

[[label]]
name = "none"                       # always include; discarded automatically
display = "Not a French landmark"
hint = "no recognisable French landmark in shot"

[[label]]
name = "eiffel-tower"
display = "Eiffel Tower"
aliases = ["tour eiffel"]
hint = "the wrought-iron lattice tower on the Champ de Mars in Paris"

# A rules pack carries patterns instead of hints. Use
# (?<![0-9A-Za-z])word(?![0-9A-Za-z]) rather than \\bword\\b, because an
# underscore is a word character and filenames are full of them.
#
# [[label]]
# name = "drone"
# display = "Drone footage"
# [[label.rule]]
# field = "filename"                # filename|path|mime_type|text|container|encoder|title
# pattern = "(?<![0-9A-Za-z])(?:dji|mavic|drone)(?![0-9A-Za-z])"
# weight = 2.0
'''

#: How a pack decides which label applies.
METHODS = ("rules", "vision", "text")

_MEDIA_TYPES = ("image", "video", "audio", "document", "other")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9 ._+&'/-]*$")

#: Fields a rule may inspect. Everything here is cheap and already loaded;
#: nothing in this list requires decoding pixels or reading the original.
RULE_FIELDS = ("filename", "path", "mime_type", "text", "container", "encoder", "title")


@dataclass(frozen=True, slots=True)
class FilterRule:
    """One regular expression tested against one field of an asset."""

    field: str
    pattern: str
    weight: float = 1.0

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FilterLabel:
    """One value in a pack's vocabulary."""

    name: str
    display: str = ""
    #: Extra words that should find this label in the search box. Aliases are
    #: how "footy" reaches ``content.sport:football`` without the model ever
    #: having emitted the word.
    aliases: tuple[str, ...] = ()
    #: A short description of what the label looks like, handed to the model.
    hint: str = ""
    rules: tuple[FilterRule, ...] = ()

    @property
    def title(self) -> str:
        return self.display or self.name.replace("-", " ").replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class FilterPack:
    """A complete, installable taxonomy."""

    id: str
    name: str
    version: str
    namespace: str
    method: str
    labels: tuple[FilterLabel, ...]
    description: str = ""
    accepts: tuple[str, ...] = ("image", "video")
    #: Whether more than one label may apply to the same asset.
    multi_label: bool = False
    #: Minimum model confidence before a claim is recorded at all.
    threshold: float = 0.35
    #: Labels the pack may emit but which should not become facet values on
    #: their own — ``unknown`` exists so a model can decline, not so a user can
    #: filter for it.
    reject_labels: tuple[str, ...] = ("unknown", "none", "other")
    display_name: str = ""
    facetable: bool = True
    #: Where the pack was read from; blank for packs constructed in memory.
    source: str = ""
    builtin: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def facet_title(self) -> str:
        return self.display_name or self.name

    @property
    def label_names(self) -> tuple[str, ...]:
        return tuple(label.name for label in self.labels)

    def label(self, name: str) -> FilterLabel | None:
        for item in self.labels:
            if item.name == name:
                return item
        return None

    def alias_map(self) -> dict[str, tuple[str, str]]:
        """``alias -> (namespace, label)``, for search-box expansion."""
        out: dict[str, tuple[str, str]] = {}
        for label in self.labels:
            for alias in (label.name, label.title.lower(), *label.aliases):
                cleaned = alias.strip().lower()
                if cleaned:
                    out.setdefault(cleaned, (self.namespace, label.name))
        return out

    def fingerprint(self) -> str:
        """A hash of everything that changes what this pack would claim.

        Editing a prompt, a threshold, or the vocabulary must supersede the
        pack's previous claims. Folding the content into the plugin version is
        what makes that happen through the existing supersession path rather
        than through a special case.
        """
        return stable_hash(
            {
                "namespace": self.namespace,
                "method": self.method,
                "multi_label": self.multi_label,
                "threshold": self.threshold,
                "labels": [
                    {
                        "name": item.name,
                        "hint": item.hint,
                        "rules": [(r.field, r.pattern, r.weight) for r in item.rules],
                    }
                    for item in self.labels
                ],
            }
        )[:10]

    @property
    def plugin_version(self) -> str:
        return f"{self.version}+{self.fingerprint()}"


def _labels(raw: Any, pack_id: str) -> tuple[FilterLabel, ...]:
    if isinstance(raw, dict):
        # The mirror of the [[pack]] mistake: a single-bracketed [label].
        raise ConfigError(
            f"filter pack {pack_id}: you wrote [label] with single brackets, which makes one "
            "table. Each value needs its own [[label]] with double brackets"
        )
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"filter pack {pack_id}: needs at least one [[label]] table, each with a name"
        )
    labels: list[FilterLabel] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError(f"filter pack {pack_id}: every [[label]] must be a table")
        name = str(entry.get("name") or "").strip().lower()
        if not _LABEL_RE.match(name):
            raise ConfigError(f"filter pack {pack_id}: bad label name {name!r}")
        if name in seen:
            raise ConfigError(f"filter pack {pack_id}: duplicate label {name!r}")
        seen.add(name)
        aliases = tuple(
            str(value).strip().lower()
            for value in (entry.get("aliases") or [])
            if str(value).strip()
        )
        rules: list[FilterRule] = []
        for rule in entry.get("rule") or []:
            if not isinstance(rule, dict):
                raise ConfigError(f"filter pack {pack_id}: every [[label.rule]] must be a table")
            rule_field = str(rule.get("field") or "filename").strip().lower()
            if rule_field not in RULE_FIELDS:
                raise ConfigError(
                    f"filter pack {pack_id}: rule field {rule_field!r} must be one of "
                    f"{', '.join(RULE_FIELDS)}"
                )
            pattern = str(rule.get("pattern") or "")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"filter pack {pack_id}: label {name!r} has an invalid pattern: {exc}"
                ) from exc
            rules.append(
                FilterRule(field=rule_field, pattern=pattern, weight=float(rule.get("weight", 1.0)))
            )
        labels.append(
            FilterLabel(
                name=name,
                display=str(entry.get("display") or ""),
                aliases=aliases,
                hint=str(entry.get("hint") or ""),
                rules=tuple(rules),
            )
        )
    return tuple(labels)


def parse_pack(document: dict[str, Any], *, source: str = "", builtin: bool = False) -> FilterPack:
    """Validate a parsed TOML document into a :class:`FilterPack`.

    Validation is strict and the messages name the file, because a pack is
    something a user is expected to write by hand.
    """
    raw = document.get("pack")
    if isinstance(raw, list):
        # `[[pack]]` is valid TOML — an array of tables — so it parses cleanly
        # and then fails here. Saying "needs a [pack] table" to someone who
        # believes they wrote one is how a writer gets stuck in a loop.
        raise ConfigError(
            "you wrote [[pack]] with double brackets, which makes an array of tables. "
            "The header must be [pack] with single brackets. Only [[label]] is doubled "
            f"({source or 'filter pack'})"
        )
    if not isinstance(raw, dict):
        raise ConfigError(
            "the document needs a [pack] table with id, name, namespace, and method "
            f"({source or 'filter pack'})"
        )
    pack_id = str(raw.get("id") or "").strip().lower()
    if not _NAME_RE.match(pack_id):
        raise ConfigError(
            f"[pack].id is required and must match [a-z0-9._-], got {pack_id!r} "
            f"({source or 'filter pack'})"
        )
    namespace = str(raw.get("namespace") or "").strip().lower()
    if not _NAME_RE.match(namespace):
        raise ConfigError(
            f"[pack].namespace is required and must be dotted lowercase, got {namespace!r} "
            f"(filter pack {pack_id})"
        )
    method = str(raw.get("method") or "vision").strip().lower()
    if method not in METHODS:
        raise ConfigError(
            f"filter pack {pack_id}: method must be one of {', '.join(METHODS)}, got {method!r}"
        )
    accepts = tuple(
        str(value).strip().lower()
        for value in (raw.get("accepts") or ["image", "video"])
    )
    unknown = [value for value in accepts if value not in _MEDIA_TYPES]
    if unknown or not accepts:
        raise ConfigError(f"filter pack {pack_id}: accepts has unknown media types {unknown!r}")
    threshold = float(raw.get("threshold", 0.35))
    if not 0.0 <= threshold <= 1.0:
        raise ConfigError(f"filter pack {pack_id}: threshold must be between 0 and 1")
    return FilterPack(
        id=pack_id,
        name=str(raw.get("name") or pack_id),
        version=str(raw.get("version") or "1.0.0"),
        namespace=namespace,
        method=method,
        labels=_labels(document.get("label"), pack_id),
        description=str(raw.get("description") or ""),
        accepts=accepts,
        multi_label=bool(raw.get("multi_label", False)),
        threshold=threshold,
        reject_labels=tuple(
            str(value).strip().lower() for value in (raw.get("reject_labels") or ("unknown", "none", "other"))
        ),
        display_name=str(raw.get("display_name") or ""),
        facetable=bool(raw.get("facetable", True)),
        source=source,
        builtin=builtin,
        extra={key: value for key, value in raw.items() if key.startswith("x_")},
    )


def load_pack(path: Path, *, builtin: bool = False) -> FilterPack:
    """Read and validate one ``*.toml`` pack definition."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_pack(document, source=str(path), builtin=builtin)
