"""The AnalysisContext: everything a plugin may know about one asset.

Lazy and cached, per the contract: a metadata-only plugin that never calls
:meth:`AnalysisContext.image` never pays a decode, and when three plugins on
the same asset all ask for pixels, the decode happens once. This is what
keeps a fleet of cheap analyzers cheap.

The context is also the security boundary in miniature: it exposes read
accessors and nothing else. There is no repository handle, no connection, no
write path. What a plugin returns from ``analyze`` is the only way it can
affect the library.
"""

from __future__ import annotations

import logging
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL.Image import Image

    from ..config import Config
    from ..db.repositories import Repositories

__all__ = ["AnalysisContext"]

_LOG = logging.getLogger(__name__)


class AnalysisContext:
    """Read-only view of one asset, handed to ``analyze``."""

    def __init__(
        self,
        *,
        repos: "Repositories",
        config: "Config",
        asset: dict[str, Any],
        plugin_id: str,
        plugin_config: dict[str, Any],
    ) -> None:
        self._repos = repos
        self._config = config
        self._asset = dict(asset)
        self._plugin_id = plugin_id
        self._plugin_config = dict(plugin_config)
        self._images: dict[int | None, "Image"] = {}

    # ── identity ────────────────────────────────────────────────────────────

    @property
    def asset_id(self) -> int:
        return int(self._asset["id"])

    @property
    def content_hash(self) -> str:
        return str(self._asset["content_hash"])

    @property
    def media_type(self) -> str:
        return str(self._asset["media_type"])

    @property
    def mime_type(self) -> str | None:
        value = self._asset.get("mime_type")
        return None if value is None else str(value)

    @property
    def captured_at(self) -> str | None:
        value = self._asset.get("captured_at")
        return None if value is None else str(value)

    @property
    def asset(self) -> dict[str, Any]:
        """The raw asset row, for anything not promoted to a property."""
        return dict(self._asset)

    @property
    def config(self) -> dict[str, Any]:
        """This plugin's slice of ``plugins.per_plugin``. Opaque to the core."""
        return dict(self._plugin_config)

    # ── files ───────────────────────────────────────────────────────────────

    @cached_property
    def path(self) -> Path | None:
        """A readable path to the original bytes, or ``None`` if all copies
        are on an unplugged drive. Plugins must treat it as read-only."""
        value = self._repos.assets.primary_path(self.asset_id)
        return None if value is None else Path(value)

    @property
    def filename(self) -> str | None:
        return self.path.name if self.path is not None else None

    # ── technical metadata ──────────────────────────────────────────────────

    @cached_property
    def metadata(self) -> dict[str, Any]:
        """Promoted technical columns. Keys may be absent; never guessed."""
        record = self._repos.assets.get_technical_metadata(self.asset_id) or {}
        record.pop("raw", None)
        return record

    @cached_property
    def raw_metadata(self) -> dict[str, Any]:
        """The full extractor payload — EXIF groups, ffprobe streams.

        This is how ``core.exif-entities`` reads vendor tags without the core
        ever promoting them to columns.
        """
        record = self._repos.assets.get_technical_metadata(self.asset_id) or {}
        raw = record.get("raw")
        return raw if isinstance(raw, dict) else {}

    @cached_property
    def location(self) -> dict[str, Any] | None:
        """Best-known location (user beats EXIF beats derived), or ``None``."""
        return self._repos.places.best_location(self.asset_id)

    # ── content accessors (the lazy, cached, expensive ones) ────────────────

    def thumbnail(self, size: int = 512) -> Path | None:
        """Path to the nearest-sized thumbnail, generated at scan time.

        Returns the closest available size rather than failing on an exact
        miss: a classifier asking for 512 on a library thumbnailed at 256
        wants pixels, not an exception.
        """
        candidates = self._repos.derivatives.for_asset(self.asset_id, kind="thumb")
        if not candidates:
            return None
        best = min(candidates, key=lambda d: abs(int(d["variant"]) - size))
        path = Path(self._config.storage.derivatives_path) / str(best["rel_path"])
        return path if path.is_file() else None

    def keyframes(self, *, every: float | None = None) -> list[tuple[float, Path]]:
        """Video keyframes as ``(seconds, path)``, optionally thinned.

        ``every=10`` yields at most one frame per 10 seconds — a cheap way for
        a plugin to bound its own cost on long videos.
        """
        root = Path(self._config.storage.derivatives_path)
        frames: list[tuple[float, Path]] = []
        last_taken: float | None = None
        for row in self._repos.derivatives.keyframes(self.asset_id):
            moment = float(row["variant"])
            if every is not None and last_taken is not None and moment - last_taken < every:
                continue
            path = root / str(row["rel_path"])
            if path.is_file():
                frames.append((moment, path))
                last_taken = moment
        return frames

    def image(self, *, prefer_size: int | None = 512) -> "Image | None":
        """Decoded pixels, cached per context.

        Prefers the thumbnail: for classification, 512px of upright,
        orientation-corrected pixels beats a 45 MB original, and the cache is
        already content-addressed. Falls back to decoding the original for
        images with no thumbnail. Returns ``None`` rather than raising — "no
        pixels available" is a normal outcome for a corrupt or offline file.
        """
        if prefer_size in self._images:
            return self._images[prefer_size]
        from PIL import Image as PILImage
        from PIL import ImageFile

        ImageFile.LOAD_TRUNCATED_IMAGES = True
        source: Path | None = self.thumbnail(prefer_size or 512)
        if source is None and self.media_type == "image":
            source = self.path
        if source is None:
            return None
        try:
            with PILImage.open(source) as opened:
                loaded = opened.convert("RGB")
        except Exception as exc:  # noqa: BLE001 - undecodable = no pixels
            _LOG.debug("context.image failed for asset %d: %s", self.asset_id, exc)
            return None
        self._images[prefer_size] = loaded
        return loaded

    @cached_property
    def text(self) -> str | None:
        """Extracted document text, or ``None`` for non-documents."""
        return self._repos.assets.get_document_text(self.asset_id)

    def annotations(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Live annotations on this asset — the output of ``depends_on``.

        Prefix-matched on namespace, mirroring search: asking for ``face``
        also returns ``face.landmarks``.
        """
        return self._repos.annotations.for_asset(self.asset_id, namespace=namespace)

    def audio_path(self) -> Path | None:
        """Mono 16 kHz WAV of this asset's audio, extracting on first request.

        The expensive accessor: extraction runs ffmpeg. It exists on demand
        precisely so scans do not pay for it when no audio plugin is
        installed.
        """
        from ..core.derivatives import DerivativeBuilder

        existing = self._repos.derivatives.get(self.asset_id, "audio", "wav")
        root = Path(self._config.storage.derivatives_path)
        if existing is not None:
            path = root / str(existing["rel_path"])
            if path.is_file():
                return path
        if self.path is None:
            return None
        builder = DerivativeBuilder(self._config, self._repos.derivatives)
        produced = builder.extract_audio(
            asset_id=self.asset_id, content_hash=self.content_hash, source=self.path
        )
        return Path(produced["path"]) if produced else None
