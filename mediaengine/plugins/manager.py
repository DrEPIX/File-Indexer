"""Persistent plugin-shop and remote registration operations.

Discovery remains read-only in :mod:`registry`. This manager is the small,
auditable mutation surface used by both the desktop GUI and HTTP API.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

from ..config import RemotePluginConfig, save_config
from ..errors import ConfigError, PluginLoadError
from .builtin.lm_studio import DEFAULT_FRAME_COUNT
from .contract import PluginInfo
from .http import HttpAnalyzer, plugin_info_from_manifest
from .models import ModelLibrary

#: The one analyzer whose behaviour depends on a separately-installed model.
LM_STUDIO_ID = "local.lm-studio"

#: The local ONNX tagger. Also model-dependent, but the model is a file on
#: disk rather than a service, which is what makes it work with nothing running.
LOCAL_TAGGER_ID = "local.vision-tagger"


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    plugin_id: str
    version: str
    description: str
    base_url: str
    enabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "description": self.description,
            "base_url": self.base_url,
            "enabled": self.enabled,
        }


def endpoint_is_local(base_url: str) -> bool:
    """Whether a URL targets loopback or the local machine by name."""

    parsed = urlparse(base_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname in {"localhost", "host.docker.internal"}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class PluginManager:
    """Safely mutate plugin configuration and refresh one running engine."""

    def __init__(self, engine: Any, *, models: ModelLibrary | None = None) -> None:
        self.engine = engine
        self.config = engine.config
        self.models = models or ModelLibrary(
            base_url=str(self.config.plugin_config(LM_STUDIO_ID).get("base_url") or "")
            or "http://127.0.0.1:1234"
        )

    def catalog(self) -> list[dict[str, object]]:
        """Return discovered plugins plus operator-facing installation state."""

        rows = cast(list[dict[str, object]], self.engine.plugins.describe())
        for row in rows:
            plugin_id = str(row.get("plugin_id") or "")
            row["registered_remote"] = plugin_id in self.config.plugins.remote
            row["configurable"] = plugin_id in self.config.plugins.per_plugin or plugin_id == LM_STUDIO_ID
            row["kind"] = self._kind(row)
            if plugin_id == LM_STUDIO_ID:
                settings = self.config.plugin_config(LM_STUDIO_ID)
                row["model"] = str(settings.get("model") or "")
                row["sees_pixels"] = bool(settings.get("send_image", True))
        return rows

    def filter_packs(self) -> dict[str, Any]:
        """Installed taxonomies, their enablement, and their queue state.

        A pack is an analyzer, so everything the analyzer view already knows
        about it — enabled, versions, done/failed/pending — is merged in rather
        than recomputed, and the two screens can never disagree.
        """
        rows = {str(row.get("plugin_id") or ""): row for row in self.engine.plugins.describe()}
        packs: list[dict[str, Any]] = []
        for pack in sorted(
            self.engine.plugins.packs.values(), key=lambda item: (not item.builtin, item.name)
        ):
            row = rows.get(pack.id, {})
            packs.append(
                {
                    "pack_id": pack.id,
                    "plugin_id": pack.id,
                    "name": pack.name,
                    "facet": pack.facet_title,
                    "namespace": pack.namespace,
                    "method": pack.method,
                    "description": pack.description,
                    "version": pack.version,
                    "accepts": list(pack.accepts),
                    "builtin": pack.builtin,
                    "source": pack.source,
                    "needs_model": pack.method in {"vision", "text"},
                    "multi_label": pack.multi_label,
                    "labels": [
                        {"name": label.name, "display": label.title, "hint": label.hint}
                        for label in pack.labels
                    ],
                    "enabled": bool(row.get("enabled")),
                    "load_error": row.get("load_error"),
                    "tasks": row.get("tasks") or {},
                }
            )
        return {
            "packs": packs,
            "errors": dict(self.engine.plugins.pack_errors),
            "directory": str(self._pack_dir()),
        }

    def _pack_dir(self) -> Any:
        from ..filters import user_pack_dir

        return user_pack_dir(self.config)

    def install_filter_pack(
        self, source: str | Any, *, overwrite: bool = False, enable: bool = False
    ) -> dict[str, Any]:
        """Add a taxonomy from a ``*.toml`` file and make it visible at once.

        Installing is not enabling. A pack that started analyzing the library
        the moment it was added would spend a user's GPU on a decision they
        have not made yet, so ``enable`` is opt-in and defaults off.
        """
        from ..filters import install_pack

        pack = install_pack(self.config, source, overwrite=overwrite)
        self.engine.reload_plugins()
        if enable:
            self.set_enabled(pack.id, True)
        return {
            "pack_id": pack.id,
            "name": pack.name,
            "namespace": pack.namespace,
            "method": pack.method,
            "labels": len(pack.labels),
            "enabled": self.config.plugins.is_enabled(pack.id),
            "source": str(pack.source),
        }

    def remove_filter_pack(self, pack_id: str) -> dict[str, Any]:
        """Delete a user-installed taxonomy and stop referring to it.

        The annotations it produced are left alone. Removing a pack is not a
        purge: the claims stay in the database with their provenance intact,
        and reinstalling the same pack picks them back up rather than
        re-analyzing a library from scratch.
        """
        from ..filters import remove_pack

        pack = self.engine.plugins.packs.get(pack_id)
        if pack is not None and pack.builtin:
            raise ConfigError(
                f"{pack_id} ships with the application and cannot be deleted; turn it off instead"
            )
        removed = remove_pack(self.config, pack_id)
        if removed is None:
            raise ConfigError(f"no installed filter pack with id {pack_id}")
        self.config.plugins.enabled = [
            value for value in self.config.plugins.enabled if value != pack_id
        ]
        self.config.plugins.disabled = [
            value for value in self.config.plugins.disabled if value != pack_id
        ]
        self.config.plugins.per_plugin.pop(pack_id, None)
        self._persist()
        self.engine.reload_plugins()
        return {"pack_id": pack_id, "removed": str(removed)}

    def facet_groups(self) -> list[dict[str, Any]]:
        """Facet values for every enabled pack, for the browse sidebar.

        Only enabled packs appear: offering a filter that cannot match
        anything because its analyzer has never run is worse than offering no
        filter at all.
        """
        groups: list[dict[str, Any]] = []
        for pack in self.engine.plugins.packs.values():
            if not self.config.plugins.is_enabled(pack.id) or not pack.facetable:
                continue
            counts = {
                str(row["label"]): int(row["count"])
                for row in self.engine.repos.annotations.facet(pack.namespace, limit=64)
            }
            values = [
                {
                    "label": label.name,
                    "display": label.title,
                    "count": counts.get(label.name, 0),
                    "query": f"{pack.namespace}:{label.name}",
                }
                for label in pack.labels
                if counts.get(label.name, 0)
            ]
            if values:
                groups.append(
                    {
                        "pack_id": pack.id,
                        "title": pack.facet_title,
                        "namespace": pack.namespace,
                        "values": sorted(values, key=lambda item: -int(item["count"])),
                    }
                )
        return groups

    def vision_models(self) -> dict[str, Any]:
        """The open-source tagging models, and which one is in use.

        Tagging models are catalogued rather than served: the engine does not
        host them, download them, or pick for you. What it can do is say what
        each one would emit, whether it runs in-process, and — the question
        nobody can answer from a model card — whether the runtime that executes
        it is actually installed here.
        """
        from ..models import CATALOG, execution_providers, installed_runtimes, tasks

        settings = self.config.plugin_config(LOCAL_TAGGER_ID)
        return {
            "models": [item.as_dict() for item in CATALOG],
            "tasks": tasks(),
            "runtimes": installed_runtimes(),
            "execution": execution_providers(),
            "model_path": str(settings.get("model_path") or ""),
            "labels_path": str(settings.get("labels_path") or ""),
            "enabled": self.config.plugins.is_enabled(LOCAL_TAGGER_ID),
            "plugin_id": LOCAL_TAGGER_ID,
        }

    def use_vision_model(
        self, model_path: str, *, labels_path: str = "", enable: bool = True
    ) -> dict[str, Any]:
        """Point the local tagger at a downloaded model and switch it on.

        Choosing a model is the same act as adopting it — the alternative is a
        store that congratulates you on a choice that then does nothing until
        you find the analyzer and enable it separately.
        """
        from ..models import preprocess_for

        settings = dict(self.config.plugin_config(LOCAL_TAGGER_ID))
        settings["model_path"] = str(model_path)
        if labels_path:
            settings["labels_path"] = str(labels_path)
        else:
            settings.pop("labels_path", None)
        # A recognised model brings its own preprocessing. Feeding a tagger
        # RGB when it wants BGR does not fail, it returns confident nonsense,
        # and no user could be expected to know the difference.
        settings.update(preprocess_for(str(model_path)))
        settings.setdefault("threshold", 0.35)
        settings.setdefault("max_frames", 12)
        self.configure(LOCAL_TAGGER_ID, settings)
        if enable:
            self.set_enabled(LOCAL_TAGGER_ID, True)
        return {
            "plugin_id": LOCAL_TAGGER_ID,
            "model_path": str(model_path),
            "enabled": self.config.plugins.is_enabled(LOCAL_TAGGER_ID),
        }

    def model_store(self, *, vision_only: bool = False) -> dict[str, Any]:
        """Everything the Model Store tab renders, in one background call."""

        return {
            "cli_available": self.models.cli_available,
            "served": self.models.served_ids(),
            "configured": str(self.config.plugin_config(LM_STUDIO_ID).get("model") or ""),
            "models": self.models.store(vision_only=vision_only),
        }

    def use_model_for_tagging(self, key: str, *, vision: bool) -> dict[str, Any]:
        """Point every model-backed analyzer at ``key``, in one step.

        Choosing a model in a store and then separately configuring each
        analyzer to use it is several steps too many; the store's promise is
        that picking a model is the same act as adopting it. Filter packs are
        included because a pack left pointing at "whatever loaded first" will
        quietly get a text model and fail on every video.
        """
        settings = dict(self.config.plugin_config(LM_STUDIO_ID))
        settings.update(
            {
                "model": key,
                "send_image": vision,
                "structured_output": True,
                "frame_count": int(settings.get("frame_count", DEFAULT_FRAME_COUNT)),
            }
        )
        settings.setdefault("base_url", "http://127.0.0.1:1234")
        self.configure(LM_STUDIO_ID, settings)
        self.set_enabled(LM_STUDIO_ID, True, grant_network=True)

        updated = [LM_STUDIO_ID]
        for pack in self.engine.plugins.packs.values():
            if pack.method not in {"vision", "text"}:
                continue
            pack_settings = dict(self.config.plugin_config(pack.id))
            pack_settings["model"] = key
            pack_settings.setdefault("base_url", settings["base_url"])
            self.configure(pack.id, pack_settings)
            updated.append(pack.id)
        return {"plugin_id": LM_STUDIO_ID, "model": key, "vision": vision, "updated": updated}

    @staticmethod
    def _kind(row: dict[str, object]) -> str:
        plugin_id = str(row.get("plugin_id") or "")
        if plugin_id == LM_STUDIO_ID:
            return "Local LLM"
        if row.get("transport") == "http":
            return "Remote API"
        if plugin_id.startswith("core."):
            return "Built in"
        return "Add-on"

    def set_enabled(
        self, plugin_id: str, enabled: bool, *, grant_network: bool = False
    ) -> dict[str, object]:
        plugin = self.engine.plugins.get(plugin_id)
        if plugin is None:
            raise PluginLoadError(f"plugin not found: {plugin_id}", plugin_id=plugin_id)
        enabled_ids = list(self.config.plugins.enabled)
        disabled_ids = list(self.config.plugins.disabled)
        if enabled:
            if plugin_id not in enabled_ids:
                enabled_ids.append(plugin_id)
            disabled_ids = [value for value in disabled_ids if value != plugin_id]
            if plugin.info.network and not self.config.plugins.allow_network:
                if not grant_network:
                    raise ConfigError(
                        f"{plugin_id} requires network access; repeat with grant_network=true"
                    )
                self.config.plugins.allow_network = True
        else:
            enabled_ids = [value for value in enabled_ids if value != plugin_id]
            if plugin_id not in disabled_ids:
                disabled_ids.append(plugin_id)
        self.config.plugins.enabled = enabled_ids
        self.config.plugins.disabled = disabled_ids
        self._persist()
        refreshed = self.engine.reload_plugins().get(plugin_id)
        return {
            "plugin_id": plugin_id,
            "enabled": bool(refreshed and refreshed.enabled),
            "notes": list(refreshed.notes) if refreshed else [],
        }

    def configure(self, plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
        if self.engine.plugins.get(plugin_id) is None:
            raise PluginLoadError(f"plugin not found: {plugin_id}", plugin_id=plugin_id)
        cleaned = {str(key): value for key, value in values.items()}
        self.config.plugins.per_plugin[plugin_id] = cleaned
        self._persist()
        self.engine.reload_plugins()
        return dict(cleaned)

    def register_remote(
        self,
        base_url: str,
        *,
        auth_token: str | None = None,
        enabled: bool = True,
        allow_external: bool = False,
        verify_tls: bool = True,
    ) -> RegistrationResult:
        remote = RemotePluginConfig(
            base_url=base_url,
            auth_token=auth_token or None,
            verify_tls=verify_tls,
            enabled=True,
        )
        if not endpoint_is_local(remote.base_url) and not allow_external:
            raise ConfigError(
                "automatic plugin registration only permits loopback URLs; "
                "set allow_external=true after reviewing the endpoint"
            )
        placeholder = PluginInfo(
            id="remote.probe", version="0", accepts=("other",), transport="http"
        )
        analyzer = HttpAnalyzer(
            placeholder,
            base_url=remote.base_url,
            auth_token=remote.auth_token,
            timeout_s=remote.timeout_s,
            connect_timeout_s=remote.connect_timeout_s,
            manifest_path=remote.manifest_path,
            health_path=remote.health_path,
            analyze_path=remote.analyze_path,
            verify_tls=remote.verify_tls,
        )
        info = plugin_info_from_manifest(analyzer.remote_manifest())
        self.config.plugins.remote[info.id] = remote
        if enabled:
            ids = list(self.config.plugins.enabled)
            if info.id not in ids:
                ids.append(info.id)
            self.config.plugins.enabled = ids
            self.config.plugins.disabled = [
                value for value in self.config.plugins.disabled if value != info.id
            ]
        self._persist()
        self.engine.reload_plugins()
        return RegistrationResult(
            plugin_id=info.id,
            version=info.version,
            description=info.description,
            base_url=remote.base_url,
            enabled=enabled,
        )

    def unregister_remote(self, plugin_id: str) -> bool:
        if plugin_id not in self.config.plugins.remote:
            return False
        del self.config.plugins.remote[plugin_id]
        self.config.plugins.enabled = [
            value for value in self.config.plugins.enabled if value != plugin_id
        ]
        if plugin_id not in self.config.plugins.disabled:
            self.config.plugins.disabled.append(plugin_id)
        self._persist()
        self.engine.reload_plugins()
        return True

    def _persist(self) -> None:
        save_config(self.config)
