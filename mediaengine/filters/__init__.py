"""Filter packs — installable taxonomies that become search facets.

The core knows nothing about what tags exist; a filter pack is how a *user*
adds one without writing Python. Each pack is a TOML file naming a namespace
and a closed vocabulary of labels, plus how to decide between them: regular
expressions over metadata, or a constrained question put to a local model.

The engine turns a pack into an ordinary analyzer, so packs inherit everything
analyzers already have — provenance, versioning, supersession, purge, resumable
queues, and automatic facets.
"""

from __future__ import annotations

from .analyzer import FilterPackAnalyzer, plugin_info_for
from .catalog import (
    BUILTIN_DIR,
    PackDiscovery,
    discover_packs,
    install_pack,
    pack_path,
    remove_pack,
    user_pack_dir,
)
from .pack import (
    METHODS,
    RULE_FIELDS,
    TEMPLATE,
    FilterLabel,
    FilterPack,
    FilterRule,
    load_pack,
    parse_pack,
)
from .vocabulary import Vocabulary, build_vocabulary

__all__ = [
    "BUILTIN_DIR",
    "METHODS",
    "RULE_FIELDS",
    "TEMPLATE",
    "FilterLabel",
    "FilterPack",
    "FilterPackAnalyzer",
    "FilterRule",
    "PackDiscovery",
    "Vocabulary",
    "build_vocabulary",
    "discover_packs",
    "install_pack",
    "load_pack",
    "pack_path",
    "parse_pack",
    "remove_pack",
    "plugin_info_for",
    "user_pack_dir",
]
