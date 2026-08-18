"""Plugin discovery and enablement.

In-process analyzers arrive through the ``mediaengine.analyzers`` entry-point
group — the same group for built-ins and third parties, because there is no
privileged path into the registry. Discovery is separate from enablement:
everything found is recorded in the database (so the UI can show what exists),
but only plugins the config enables ever run.

Manifest-directory scanning (``plugin.toml`` for subprocess/HTTP transports)
lands with the transport runners in the remainder of milestone 3; the
enablement and version-bump logic here is already transport-agnostic.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..db.repositories import Repositories
from ..errors import PluginLoadError, PluginUnavailable
from ..util import stable_hash
from .contract import Analyzer, PluginInfo
from .http import HttpAnalyzer, plugin_info_from_manifest

if TYPE_CHECKING:
    from ..filters import FilterPack

__all__ = ["LoadedPlugin", "PluginRegistry"]

_LOG = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "mediaengine.analyzers"


@dataclass(slots=True)
class LoadedPlugin:
    """One discovered analyzer plus its runtime state."""

    info: PluginInfo
    analyzer: Analyzer | None
    enabled: bool
    load_error: str | None = None
    config_hash: str = ""
    previous_version: str | None = None
    """Set when the persisted registry shows a different version — the signal
    that this plugin's tasks must be invalidated and re-enqueued."""

    notes: list[str] = field(default_factory=list)


class PluginRegistry:
    """Discovers, enables and records analyzers."""

    def __init__(self, config: Config, repos: Repositories) -> None:
        self.config = config
        self.repos = repos
        self._plugins: dict[str, LoadedPlugin] = {}
        self._discovered = False
        #: Filter packs found during the last discovery, by pack id.
        self.packs: dict[str, "FilterPack"] = {}
        #: Pack files that failed to parse, keyed by path — surfaced in the UI
        #: so a typo in a user's TOML is visible rather than silently ignored.
        self.pack_errors: dict[str, str] = {}

    # ── discovery ───────────────────────────────────────────────────────────

    def discover(self) -> dict[str, LoadedPlugin]:
        """Load every entry-point analyzer and persist the registry state.

        Idempotent; call at startup and after installs. A plugin that fails to
        import is *recorded as broken*, not skipped silently — "why did my
        plugin never run" must be answerable from the UI.
        """
        found: dict[str, LoadedPlugin] = {}
        for entry in entry_points(group=_ENTRY_POINT_GROUP):
            plugin = self._load_entry(entry.name, entry)
            if plugin is not None:
                found[plugin.info.id] = plugin

        for plugin in self._load_filter_packs():
            # An entry-point analyzer wins a name collision: a pack cannot
            # impersonate installed code.
            found.setdefault(plugin.info.id, plugin)

        if self.config.plugins.allow_remote_plugins:
            for path in self._manifest_paths():
                plugin = self._load_http_manifest(path)
                if plugin is None:
                    continue
                if plugin.info.id in found:
                    _LOG.warning(
                        "ignoring duplicate manifest %s for plugin %s",
                        path,
                        plugin.info.id,
                    )
                    continue
                found[plugin.info.id] = plugin
            for plugin_id, remote in self.config.plugins.remote.items():
                if plugin_id in found or not remote.enabled:
                    continue
                plugin = self._load_configured_remote(plugin_id, remote)
                if plugin is not None:
                    found[plugin.info.id] = plugin

        self._plugins = found
        self._persist()
        self._discovered = True
        return dict(self._plugins)

    def _load_filter_packs(self) -> list[LoadedPlugin]:
        """Turn every discovered filter pack into an analyzer.

        Packs are data, so a broken one is a user-authored TOML error rather
        than a code fault; it is recorded as a load error against its own id so
        the store can show *which* pack is wrong and why.
        """
        from ..filters import FilterPackAnalyzer, discover_packs

        discovery = discover_packs(self.config)
        self.pack_errors = dict(discovery.errors)
        self.packs = {pack.id: pack for pack in discovery.packs.values()}
        out: list[LoadedPlugin] = []
        for pack in discovery.sorted_packs():
            try:
                analyzer = FilterPackAnalyzer(pack)
            except Exception as exc:  # noqa: BLE001 - one bad pack must not hide the rest
                _LOG.warning("filter pack %s failed to build: %s", pack.id, exc)
                out.append(
                    LoadedPlugin(
                        info=PluginInfo(id=pack.id, version="0", accepts=("other",)),
                        analyzer=None,
                        enabled=False,
                        load_error=str(exc),
                    )
                )
                continue
            plugin_config = self.config.plugin_config(pack.id)
            loaded = LoadedPlugin(
                info=analyzer.info,
                analyzer=analyzer,
                enabled=self.config.plugins.is_enabled(pack.id),
                config_hash=stable_hash(plugin_config) if plugin_config else "",
                notes=[f"filter pack: {pack.source}" if pack.source else "filter pack"],
            )
            self._gate_capabilities(loaded)
            out.append(loaded)
        return out

    def _manifest_paths(self) -> list[Path]:
        """Find manifests at a configured root or one directory below it."""

        paths: set[Path] = set()
        for directory in self.config.plugins.directories:
            root = Path(directory)
            direct = root / "plugin.toml"
            if direct.is_file():
                paths.add(direct.resolve())
            if root.is_dir():
                paths.update(path.resolve() for path in root.glob("*/plugin.toml") if path.is_file())
        return sorted(paths)

    def _load_http_manifest(self, path: Path) -> LoadedPlugin | None:
        """Load one HTTP ``plugin.toml`` without importing model dependencies."""

        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
            raw_plugin = document.get("plugin")
            if not isinstance(raw_plugin, dict):
                raise PluginLoadError(f"{path}: missing [plugin] table")
            transport = str(raw_plugin.get("transport") or "in_process")
            if transport != "http":
                _LOG.debug("transport %s in %s is not implemented by the HTTP loader", transport, path)
                return None
            info = plugin_info_from_manifest(raw_plugin)
            http_value = raw_plugin.get("http")
            if not isinstance(http_value, dict):
                raise PluginLoadError(f"{path}: HTTP plugin is missing [plugin.http]")
            override = self.config.plugins.remote.get(info.id)
            settings: dict[str, Any] = dict(http_value)
            if override is not None:
                settings.update(override.model_dump(exclude_none=True))
            analyzer = self._http_analyzer(info, settings)
            notes: list[str] = [f"manifest: {path}"]
            if self.config.plugins.is_enabled(info.id):
                try:
                    remote_manifest = analyzer.remote_manifest()
                    remote_info = plugin_info_from_manifest(remote_manifest)
                    if remote_info.id != info.id:
                        raise PluginLoadError(
                            f"{path}: service id {remote_info.id!r} does not match {info.id!r}",
                            plugin_id=info.id,
                        )
                    info = remote_info
                    analyzer = self._http_analyzer(info, settings)
                except PluginUnavailable as exc:
                    # Service startup and model downloads are asynchronous.
                    # Keep it runnable so task execution can retry later.
                    notes.append(f"service not ready during discovery: {exc}")
            plugin_config = self.config.plugin_config(info.id)
            loaded = LoadedPlugin(
                info=info,
                analyzer=analyzer,
                enabled=self.config.plugins.is_enabled(info.id),
                config_hash=stable_hash(plugin_config) if plugin_config else "",
                notes=notes,
            )
            self._gate_capabilities(loaded)
            return loaded
        except Exception as exc:  # noqa: BLE001 - one bad manifest must not hide the others
            _LOG.warning("plugin manifest %s failed to load: %s", path, exc)
            return None

    def _load_configured_remote(self, plugin_id: str, remote: Any) -> LoadedPlugin | None:
        """Discover a config-only HTTP endpoint from its live manifest."""

        placeholder = PluginInfo(id=plugin_id, version="0", accepts=("other",), transport="http")
        settings = remote.model_dump(exclude_none=True)
        analyzer = self._http_analyzer(placeholder, settings)
        try:
            info = plugin_info_from_manifest(analyzer.remote_manifest())
            if info.id != plugin_id:
                raise PluginLoadError(
                    f"configured remote {plugin_id!r} advertises id {info.id!r}",
                    plugin_id=plugin_id,
                )
            analyzer = self._http_analyzer(info, settings)
        except Exception as exc:  # noqa: BLE001 - recorded as a broken discovery
            _LOG.warning("remote plugin %s failed discovery: %s", plugin_id, exc)
            return LoadedPlugin(
                info=placeholder,
                analyzer=None,
                enabled=False,
                load_error=str(exc),
            )
        plugin_config = self.config.plugin_config(info.id)
        loaded = LoadedPlugin(
            info=info,
            analyzer=analyzer,
            enabled=self.config.plugins.is_enabled(info.id),
            config_hash=stable_hash(plugin_config) if plugin_config else "",
            notes=[f"configured endpoint: {settings['base_url']}"],
        )
        self._gate_capabilities(loaded)
        return loaded

    @staticmethod
    def _http_analyzer(info: PluginInfo, settings: dict[str, Any]) -> HttpAnalyzer:
        base_url = str(settings.get("base_url") or "").rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise PluginLoadError(f"{info.id}: plugin.http.base_url must be an HTTP URL")
        return HttpAnalyzer(
            info,
            base_url=base_url,
            auth_token=str(settings["auth_token"]) if settings.get("auth_token") else None,
            timeout_s=float(settings.get("timeout_s", 120.0)),
            connect_timeout_s=float(settings.get("connect_timeout_s", 5.0)),
            health_path=str(settings.get("health_path", "/health")),
            manifest_path=str(settings.get("manifest_path", "/manifest")),
            analyze_path=str(settings.get("analyze_path", "/analyze")),
            verify_tls=bool(settings.get("verify_tls", True)),
        )

    def _load_entry(self, declared_id: str, entry: object) -> LoadedPlugin | None:
        try:
            cls = entry.load()  # type: ignore[attr-defined]
            analyzer: Analyzer = cls()
            info: PluginInfo = analyzer.info
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop discovery
            _LOG.warning("plugin %s failed to load: %s", declared_id, exc)
            try:
                broken = PluginInfo(id=declared_id, version="0", accepts=("other",))
            except Exception:  # noqa: BLE001 - even the id is bad
                return None
            return LoadedPlugin(
                info=broken, analyzer=None, enabled=False, load_error=str(exc)
            )

        if info.id != declared_id:
            # The entry-point name is the public identity; a mismatch means
            # producer rows would be attributed to a different id than the
            # config enables. Refuse rather than guess which one was meant.
            raise PluginLoadError(
                f"entry point {declared_id!r} declares info.id {info.id!r}; they must match",
                plugin_id=declared_id,
            )

        plugin_config = self.config.plugin_config(info.id)
        loaded = LoadedPlugin(
            info=info,
            analyzer=analyzer,
            enabled=self.config.plugins.is_enabled(info.id),
            config_hash=stable_hash(plugin_config) if plugin_config else "",
        )
        self._gate_capabilities(loaded)
        return loaded

    def _gate_capabilities(self, plugin: LoadedPlugin) -> None:
        """Deny-by-default capability checks, with the audit trail the spec
        requires. A denial disables the plugin rather than degrading it."""
        info = plugin.info
        if info.network:
            granted = self.config.plugins.allow_network
            self.repos.tasks.log_capability_grant(
                info.id,
                "network",
                granted,
                reason="plugins.allow_network" if granted else "denied by default",
            )
            if not granted:
                plugin.enabled = False
                plugin.notes.append("disabled: requires network, plugins.allow_network=false")
        if info.gpu and not self.config.plugins.allow_gpu:
            self.repos.tasks.log_capability_grant(
                info.id, "gpu", False, reason="plugins.allow_gpu=false"
            )
            plugin.enabled = False
            plugin.notes.append("disabled: requires gpu, plugins.allow_gpu=false")

    def _persist(self) -> None:
        """Mirror discovery into ``plugin_registry`` and detect version bumps."""
        for plugin in self._plugins.values():
            info = plugin.info
            previous = self.repos.tasks.record_plugin(
                info.id,
                info.version,
                transport=info.transport,
                enabled=plugin.enabled,
                accepts=list(info.accepts),
                emits=list(info.emits),
                depends_on=list(info.depends_on),
                capabilities={
                    "pixels": info.pixels, "frames": info.frames, "audio": info.audio,
                    "text": info.text, "metadata_only": info.metadata_only,
                    "gpu": info.gpu, "network": info.network,
                    "max_concurrency": info.max_concurrency,
                },
                config_hash=plugin.config_hash or None,
            )
            plugin.previous_version = previous
            if previous is not None:
                # Version bump: queued work for the old version is pointless.
                dropped = self.repos.tasks.invalidate_plugin_version(info.id, info.version)
                _LOG.info(
                    "%s changed %s -> %s; dropped %d stale task(s)",
                    info.id, previous, info.version, dropped,
                )
            for namespace, meta in info.namespaces.items():
                self.repos.annotations.register_namespace(
                    namespace,
                    display_name=str(meta.get("display_name") or namespace),
                    description=meta.get("description"),
                    value_type=str(meta.get("value_type") or "categorical"),
                    facetable=bool(meta.get("facetable", True)),
                    registered_by=info.id,
                )
        self.repos.tasks.mark_plugins_absent(list(self._plugins))

    # ── access ──────────────────────────────────────────────────────────────

    def ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()

    @property
    def plugins(self) -> dict[str, LoadedPlugin]:
        self.ensure_discovered()
        return dict(self._plugins)

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        self.ensure_discovered()
        return self._plugins.get(plugin_id)

    def enabled(self) -> list[LoadedPlugin]:
        """Runnable plugins in dependency order.

        Topologically sorted so that a plugin consuming another's output
        (``depends_on``) is scheduled after it. Cycles and missing
        dependencies disable the dependent plugin with a recorded reason
        rather than crashing the scheduler.
        """
        self.ensure_discovered()
        runnable = {
            p.info.id: p for p in self._plugins.values() if p.enabled and p.analyzer is not None
        }
        ordered: list[LoadedPlugin] = []
        state: dict[str, int] = {}  # 0=unvisited 1=visiting 2=done

        def visit(plugin_id: str, chain: tuple[str, ...]) -> bool:
            if state.get(plugin_id) == 2:
                return True
            if state.get(plugin_id) == 1:
                for member in chain:
                    if member in runnable:
                        runnable[member].notes.append(
                            f"disabled: dependency cycle {' -> '.join((*chain, plugin_id))}"
                        )
                return False
            state[plugin_id] = 1
            plugin = runnable.get(plugin_id)
            if plugin is None:
                return False
            for dependency in plugin.info.depends_on:
                if dependency not in runnable:
                    plugin.notes.append(f"disabled: dependency {dependency!r} not available")
                    plugin.enabled = False
                    state[plugin_id] = 2
                    return False
                if not visit(dependency, (*chain, plugin_id)):
                    plugin.enabled = False
                    state[plugin_id] = 2
                    return False
            state[plugin_id] = 2
            ordered.append(plugin)
            return True

        for plugin_id in sorted(runnable):
            visit(plugin_id, ())
        return [p for p in ordered if p.enabled]

    def describe(self) -> list[dict[str, object]]:
        """Registry rows merged with live discovery, for the CLI and the API."""
        self.ensure_discovered()
        persisted = {str(row["plugin_id"]): row for row in self.repos.tasks.list_plugins()}
        counts = self.repos.tasks.counts_by_plugin()
        out: list[dict[str, object]] = []
        seen: set[str] = set()
        for plugin_id, plugin in sorted(self._plugins.items()):
            seen.add(plugin_id)
            row = dict(persisted.get(plugin_id, {}))
            row.update(
                {
                    "plugin_id": plugin_id,
                    "version": plugin.info.version,
                    "transport": plugin.info.transport,
                    "enabled": plugin.enabled,
                    "present": True,
                    "accepts": list(plugin.info.accepts),
                    "emits": list(plugin.info.emits),
                    "depends_on": list(plugin.info.depends_on),
                    "description": plugin.info.description,
                    "load_error": plugin.load_error,
                    "notes": list(plugin.notes),
                    "tasks": counts.get(plugin_id, {}),
                }
            )
            out.append(row)
        for plugin_id, row in sorted(persisted.items()):
            if plugin_id not in seen:
                gone = dict(row)
                gone["present"] = False
                gone["tasks"] = counts.get(plugin_id, {})
                out.append(gone)
        return out
