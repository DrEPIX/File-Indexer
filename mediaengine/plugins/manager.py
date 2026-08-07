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
from .contract import PluginInfo
from .http import HttpAnalyzer, plugin_info_from_manifest


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

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.config = engine.config

    def catalog(self) -> list[dict[str, object]]:
        """Return discovered plugins plus operator-facing installation state."""

        rows = cast(list[dict[str, object]], self.engine.plugins.describe())
        for row in rows:
            plugin_id = str(row.get("plugin_id") or "")
            row["registered_remote"] = plugin_id in self.config.plugins.remote
            row["configurable"] = plugin_id in self.config.plugins.per_plugin or plugin_id == "local.lm-studio"
            row["kind"] = self._kind(row)
        return rows

    @staticmethod
    def _kind(row: dict[str, object]) -> str:
        plugin_id = str(row.get("plugin_id") or "")
        if plugin_id == "local.lm-studio":
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
