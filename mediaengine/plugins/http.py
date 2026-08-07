"""HTTP analyzer transport and work-item serialization.

The model service is always the server and MediaEngine is always the client.
Only derivative paths or explicitly requested inline bytes cross this boundary;
the service never receives a database handle and can never mutate originals.
"""

from __future__ import annotations

import base64
import mimetypes
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import (
    PluginContractError,
    PluginExecutionError,
    PluginTimeout,
    PluginUnavailable,
)
from .contract import PROTOCOL, Annotation, PluginInfo, Region
from .context import AnalysisContext

__all__ = ["HttpAnalyzer", "plugin_info_from_manifest"]


def _string_tuple(value: object, field: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        if required:
            raise PluginContractError(f"manifest.{field} must be an array of strings")
        return ()
    result = tuple(str(item).strip() for item in value if str(item).strip())
    if required and not result:
        raise PluginContractError(f"manifest.{field} must not be empty")
    return result


def plugin_info_from_manifest(manifest: Mapping[str, Any]) -> PluginInfo:
    """Normalize an HTTP response or ``[plugin]`` TOML table."""

    requires_value = manifest.get("requires", {})
    requires = requires_value if isinstance(requires_value, Mapping) else {}
    namespaces_value = manifest.get("namespaces", {})
    namespaces: dict[str, dict[str, Any]] = {}
    if isinstance(namespaces_value, Mapping):
        for name, metadata in namespaces_value.items():
            if isinstance(metadata, Mapping):
                namespaces[str(name)] = dict(metadata)
    raw_dimension = manifest.get("embedding_dim")
    dimension = int(raw_dimension) if raw_dimension is not None else None
    return PluginInfo(
        id=str(manifest.get("id", "")),
        version=str(manifest.get("version", "")),
        accepts=_string_tuple(manifest.get("accepts"), "accepts", required=True),
        emits=_string_tuple(manifest.get("emits"), "emits"),
        depends_on=_string_tuple(manifest.get("depends_on"), "depends_on"),
        model_id=str(manifest.get("model_id") or ""),
        description=str(manifest.get("description") or ""),
        transport="http",
        transfer=str(manifest.get("transfer") or "paths"),
        embedding_dim=dimension,
        pixels=bool(requires.get("pixels", False)),
        frames=bool(requires.get("frames", False)),
        audio=bool(requires.get("audio", False)),
        text=bool(requires.get("text", False)),
        metadata_only=bool(requires.get("metadata_only", False)),
        gpu=bool(requires.get("gpu", False)),
        network=bool(requires.get("network", False)),
        max_concurrency=max(1, int(requires.get("max_concurrency", 1))),
        namespaces=namespaces,
    )


class HttpAnalyzer:
    """An :class:`Analyzer` backed by the frozen JSON-over-HTTP protocol."""

    def __init__(
        self,
        info: PluginInfo,
        *,
        base_url: str,
        auth_token: str | None = None,
        timeout_s: float = 120.0,
        connect_timeout_s: float = 5.0,
        health_path: str = "/health",
        manifest_path: str = "/manifest",
        analyze_path: str = "/analyze",
        verify_tls: bool = True,
        http_transport: Any | None = None,
    ) -> None:
        self.info = info
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.health_path = health_path
        self.manifest_path = manifest_path
        self.analyze_path = analyze_path
        self.verify_tls = verify_tls
        self._http_transport = http_transport

    @property
    def _headers(self) -> dict[str, str]:
        return (
            {"Authorization": f"Bearer {self.auth_token}"}
            if self.auth_token
            else {}
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            import httpx
        except ImportError as exc:
            raise PluginUnavailable(
                "HTTP plugins require the 'remote' extra: pip install mediaengine[remote]",
                plugin_id=self.info.id,
            ) from exc
        timeout = httpx.Timeout(self.timeout_s, connect=self.connect_timeout_s)
        try:
            with httpx.Client(
                timeout=timeout,
                verify=self.verify_tls,
                transport=self._http_transport,
            ) as client:
                return client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers,
                    **kwargs,
                )
        except httpx.TimeoutException as exc:
            raise PluginTimeout(
                f"{self.info.id} timed out: {exc}", plugin_id=self.info.id
            ) from exc
        except httpx.HTTPError as exc:
            raise PluginUnavailable(
                f"{self.info.id} is unavailable: {exc}", plugin_id=self.info.id
            ) from exc

    def remote_manifest(self) -> dict[str, Any]:
        """Fetch the service's authoritative runtime manifest."""

        response = self._request("GET", self.manifest_path)
        if response.status_code != 200:
            raise PluginUnavailable(
                f"{self.info.id} manifest returned HTTP {response.status_code}",
                plugin_id=self.info.id,
            )
        payload = self._json_object(response, "manifest")
        if payload.get("protocol") not in (None, PROTOCOL):
            raise PluginContractError(
                f"{self.info.id} advertises unsupported protocol {payload.get('protocol')!r}",
                plugin_id=self.info.id,
            )
        return payload

    def analyze(self, ctx: AnalysisContext) -> Sequence[Annotation]:
        """Serialize one context, invoke the service, and decode its claims."""

        health = self._request("GET", self.health_path)
        if health.status_code != 200:
            self._raise_remote(health, operation="health")
        health_payload = self._json_object(health, "health")
        if health_payload.get("status") != "ok":
            raise PluginUnavailable(
                f"{self.info.id} is {health_payload.get('status', 'not ready')}: "
                f"{health_payload.get('detail', '')}",
                plugin_id=self.info.id,
            )

        request_id = uuid.uuid4().hex
        response = self._request(
            "POST", self.analyze_path, json=self._work_item(ctx, request_id)
        )
        if response.status_code != 200:
            self._raise_remote(response, operation="analyze")
        payload = self._json_object(response, "analysis result")
        if payload.get("protocol") != PROTOCOL or payload.get("request_id") != request_id:
            raise PluginContractError(
                f"{self.info.id} returned a mismatched protocol or request_id",
                plugin_id=self.info.id,
            )
        raw = payload.get("annotations")
        if not isinstance(raw, list):
            raise PluginContractError(
                f"{self.info.id} response.annotations must be an array",
                plugin_id=self.info.id,
            )
        return [self._annotation(item, index) for index, item in enumerate(raw)]

    def _work_item(self, ctx: AnalysisContext, request_id: str) -> dict[str, Any]:
        root = Path(ctx._config.storage.derivatives_path)  # transport is engine-side
        derivatives: dict[str, Any] = {}
        if ctx.path is not None:
            derivatives["original"] = self._reference(ctx.path)

        thumbnails: dict[str, Any] = {}
        for row in ctx._repos.derivatives.for_asset(ctx.asset_id, kind="thumb"):
            path = root / str(row["rel_path"])
            if path.is_file():
                thumbnails[str(row["variant"])] = self._reference(path)
        if thumbnails:
            derivatives["thumbnails"] = thumbnails

        if self.info.frames:
            frames = [
                {"time": moment, **self._reference(path)}
                for moment, path in ctx.keyframes()
            ]
            if frames:
                derivatives["keyframes"] = frames
        if self.info.audio:
            audio_path = ctx.audio_path()
            if audio_path is not None:
                derivatives["audio"] = self._reference(audio_path)

        prior = []
        dependency_ids = set(self.info.depends_on)
        if dependency_ids:
            for item in ctx.annotations():
                if str(item.get("plugin_id")) in dependency_ids:
                    prior.append(self._prior_annotation(item))

        return {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "deadline_s": self.timeout_s,
            "asset": {
                "asset_id": ctx.asset_id,
                "content_hash": ctx.content_hash,
                "media_type": ctx.media_type,
                "mime_type": ctx.mime_type,
                "size_bytes": ctx.asset.get("size_bytes"),
                "captured_at": ctx.captured_at,
                "filename": ctx.filename,
            },
            "metadata": ctx.metadata,
            "derivatives": derivatives,
            "text": ctx.text if self.info.text else None,
            "prior_annotations": prior,
            "config": ctx.config,
        }

    def _reference(self, path: Path) -> dict[str, Any]:
        resolved = path.resolve()
        if self.info.transfer == "inline":
            content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            return {
                "content_b64": base64.b64encode(resolved.read_bytes()).decode("ascii"),
                "content_type": content_type,
            }
        return {"path": str(resolved)}

    @staticmethod
    def _prior_annotation(item: Mapping[str, Any]) -> dict[str, Any]:
        region = None
        if item.get("region_id") is not None:
            region = {
                "x": item.get("x"), "y": item.get("y"),
                "w": item.get("w"), "h": item.get("h"),
                "frame_time": item.get("frame_time"),
                "page_number": item.get("page_number"),
                "kind": item.get("region_kind"),
            }
        return {
            "namespace": item.get("namespace"),
            "label": item.get("label"),
            "value": item.get("value"),
            "confidence": item.get("confidence"),
            "region": region,
            "producer": f"{item.get('plugin_id')}@{item.get('producer_version')}",
        }

    def _annotation(self, value: object, index: int) -> Annotation:
        if not isinstance(value, Mapping):
            raise PluginContractError(
                f"{self.info.id} annotation[{index}] must be an object",
                plugin_id=self.info.id,
            )
        region_value = value.get("region")
        region = None
        if region_value is not None:
            if not isinstance(region_value, Mapping):
                raise PluginContractError(
                    f"{self.info.id} annotation[{index}].region must be an object",
                    plugin_id=self.info.id,
                )
            region = Region(
                x=self._optional_float(region_value.get("x")),
                y=self._optional_float(region_value.get("y")),
                w=self._optional_float(region_value.get("w")),
                h=self._optional_float(region_value.get("h")),
                frame_time=self._optional_float(region_value.get("frame_time")),
                page_number=(
                    int(region_value["page_number"])
                    if region_value.get("page_number") is not None
                    else None
                ),
                kind=(str(region_value["kind"]) if region_value.get("kind") is not None else None),
            )
        raw_embedding = value.get("embedding")
        embedding: list[float] | None = None
        if raw_embedding is not None:
            if not isinstance(raw_embedding, Sequence) or isinstance(raw_embedding, (str, bytes)):
                raise PluginContractError(
                    f"{self.info.id} annotation[{index}].embedding must be an array",
                    plugin_id=self.info.id,
                )
            embedding = [float(number) for number in raw_embedding]
        raw_value = value.get("value")
        if raw_value is not None and not isinstance(raw_value, Mapping):
            raise PluginContractError(
                f"{self.info.id} annotation[{index}].value must be an object",
                plugin_id=self.info.id,
            )
        return Annotation(
            namespace=str(value.get("namespace") or ""),
            label=str(value.get("label") or ""),
            value=dict(raw_value) if isinstance(raw_value, Mapping) else None,
            confidence=self._optional_float(value.get("confidence")),
            region=region,
            embedding=embedding,
        )

    def _optional_float(self, value: object) -> float | None:
        if value is None:
            return None
        if not isinstance(value, (int, float, str)):
            raise PluginContractError(
                f"{self.info.id} returned a non-numeric value where a number was required",
                plugin_id=self.info.id,
            )
        try:
            return float(value)
        except ValueError as exc:
            raise PluginContractError(
                f"{self.info.id} returned an invalid number {value!r}",
                plugin_id=self.info.id,
            ) from exc

    def _json_object(self, response: Any, operation: str) -> dict[str, Any]:
        try:
            value = response.json()
        except Exception as exc:  # noqa: BLE001 - third-party response parser
            raise PluginContractError(
                f"{self.info.id} {operation} returned invalid JSON",
                plugin_id=self.info.id,
            ) from exc
        if not isinstance(value, dict):
            raise PluginContractError(
                f"{self.info.id} {operation} response must be an object",
                plugin_id=self.info.id,
            )
        return value

    def _raise_remote(self, response: Any, *, operation: str) -> None:
        status = int(response.status_code)
        message = f"{self.info.id} {operation} returned HTTP {status}"
        retryable = status in (408, 429, 500, 502, 503, 504)
        try:
            payload = response.json()
            error = payload.get("error", payload) if isinstance(payload, dict) else {}
            if isinstance(error, dict):
                message = str(error.get("message") or message)
                if status == 500 and error.get("retryable") is False:
                    retryable = False
        except Exception:  # noqa: BLE001 - preserve the HTTP status fallback
            pass
        if retryable:
            raise PluginUnavailable(message, plugin_id=self.info.id)
        raise PluginExecutionError(message, plugin_id=self.info.id)
