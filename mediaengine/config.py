"""Configuration model.

A single ``config.yaml`` validated by Pydantic, with environment-variable
overrides using the ``MEDIAENGINE__`` prefix and ``__`` as the nesting
delimiter::

    MEDIAENGINE__API__PORT=9000
    MEDIAENGINE__WORKERS__METADATA_PROCESSES=4
    MEDIAENGINE__PLUGINS__ALLOW_NETWORK=true

Every path is resolved to an absolute path at load time relative to the
config file's directory, so a relative ``./data/library.db`` means "next to
the config", not "next to whatever the current working directory happens to
be when the GUI launched".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError

#: Bind addresses that need no bearer token. Anything else does.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1"})

__all__ = [
    "LOOPBACK_HOSTS",
    "LibraryConfig",
    "StorageConfig",
    "ScanConfig",
    "WorkersConfig",
    "PluginsConfig",
    "RemotePluginConfig",
    "SearchConfig",
    "ApiConfig",
    "LoggingConfig",
    "Config",
    "load_config",
    "save_config",
    "default_config",
]


class LibraryConfig(BaseModel):
    """Which files on disk are in scope."""

    model_config = {"extra": "forbid"}

    roots: list[Path] = Field(default_factory=list, description="Directories to scan.")
    include: list[str] = Field(default=["**/*"], description="Glob allowlist, relative to a root.")
    exclude: list[str] = Field(
        default=[
            "**/.*",
            "**/node_modules/**",
            "**/@eaDir/**",
            "**/.thumbnails/**",
            "**/lost+found/**",
            "**/#recycle/**",
            "**/$RECYCLE.BIN/**",
            "**/System Volume Information/**",
        ],
        description="Glob denylist. Evaluated after include; deny wins.",
    )
    follow_symlinks: bool = False
    include_hidden: bool = Field(
        default=False, description="Traverse dotfiles/dotdirs. Independent of the exclude globs."
    )
    max_depth: int | None = Field(default=None, ge=0, description="None = unlimited.")
    min_file_size: int = Field(default=1, ge=0, description="Bytes. 0 indexes zero-byte files too.")
    cross_filesystem: bool = Field(
        default=True, description="Descend into directories on a different device id."
    )


class StorageConfig(BaseModel):
    """Where the engine keeps its own data. Never inside a library root."""

    model_config = {"extra": "forbid"}

    db_path: Path = Path("./data/library.db")
    derivatives_path: Path = Path("./data/derivatives")
    thumbnail_sizes: list[int] = Field(default=[256, 512, 2048], min_length=1)
    thumbnail_format: Literal["webp", "jpeg", "png"] = "webp"
    thumbnail_quality: int = Field(default=82, ge=1, le=100)
    video_proxy: bool = Field(default=False, description="Transcode a scrubbing proxy per video.")
    video_proxy_height: int = Field(default=720, ge=144)
    max_derivative_bytes: int = Field(
        default=50 * 1024**3, ge=0, description="Soft cap on the derivative cache. 0 = unlimited."
    )

    @field_validator("thumbnail_sizes")
    @classmethod
    def _sorted_unique(cls, v: list[int]) -> list[int]:
        if any(s <= 0 for s in v):
            raise ValueError("thumbnail sizes must be positive")
        return sorted(set(v))


class ScanConfig(BaseModel):
    """Ingest behaviour."""

    model_config = {"extra": "forbid"}

    hash_algorithm: Literal["blake3", "sha256"] = "blake3"
    hash_chunk_size: int = Field(default=4 * 1024 * 1024, ge=4096)
    compute_perceptual_hash: bool = True
    extract_gps: bool = True
    extract_document_text: bool = True
    max_document_text_chars: int = Field(default=2_000_000, ge=0)
    video_keyframe_interval_s: float = Field(default=5.0, gt=0)
    video_keyframe_max: int = Field(default=64, ge=1)
    video_scene_detection: bool = True
    video_scene_threshold: float = Field(default=0.4, gt=0, lt=1)
    detect_sidecars: bool = True
    detect_motion_photos: bool = True
    batch_size: int = Field(default=256, ge=1, description="Paths per walker batch.")
    rescan_missing: bool = Field(
        default=True, description="Mark rows missing when a known path disappears."
    )


class WorkersConfig(BaseModel):
    """Pool sizing and subprocess safety limits."""

    model_config = {"extra": "forbid"}

    metadata_processes: int = Field(default=0, ge=0, description="0 = os.cpu_count().")
    derivative_workers: int = Field(default=0, ge=0, description="0 = os.cpu_count().")
    analysis_workers: int = Field(default=0, ge=0, description="0 = os.cpu_count().")
    io_threads: int = Field(default=8, ge=1)
    writer_queue_size: int = Field(default=10_000, ge=16)
    walk_queue_size: int = Field(default=64, ge=1, description="Batches, not paths. Backpressure.")
    subprocess_timeout_s: float = Field(default=60.0, gt=0)
    exiftool_daemon: bool = Field(
        default=True, description="Use -stay_open. ~10x faster than per-file spawns."
    )
    exiftool_batch_size: int = Field(default=64, ge=1)
    plugin_timeout_s: float = Field(default=300.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    retry_base_delay_s: float = Field(default=1.0, gt=0)
    retry_max_delay_s: float = Field(default=60.0, gt=0)

    def resolved_metadata_processes(self) -> int:
        return self.metadata_processes or (os.cpu_count() or 4)

    def resolved_derivative_workers(self) -> int:
        return self.derivative_workers or (os.cpu_count() or 4)

    def resolved_analysis_workers(self) -> int:
        return self.analysis_workers or (os.cpu_count() or 4)


class RemotePluginConfig(BaseModel):
    """An analyzer reached over HTTP rather than imported or forked.

    The engine calls out to ``base_url``; the plugin never calls in. This is a
    *host* network grant, distinct from the per-plugin ``network`` capability
    that governs whether plugin code may itself reach the internet.
    """

    model_config = {"extra": "forbid"}

    base_url: str
    auth_token: str | None = None
    timeout_s: float = Field(default=120.0, gt=0)
    connect_timeout_s: float = Field(default=5.0, gt=0)
    max_concurrency: int = Field(default=4, ge=1)
    verify_tls: bool = True
    health_path: str = "/health"
    manifest_path: str = "/manifest"
    analyze_path: str = "/analyze"
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")


class PluginsConfig(BaseModel):
    """Discovery, enablement and capability grants."""

    model_config = {"extra": "forbid"}

    enabled: list[str] = Field(
        default=["core.exif-entities", "core.exif-gps", "core.visual-signals"],
        description="Plugin ids permitted to run. Empty list means none.",
    )
    disabled: list[str] = Field(
        default_factory=list, description="Denylist. Wins over `enabled` and over `enable_all`."
    )
    enable_all: bool = Field(
        default=False, description="Run every discovered plugin except `disabled`."
    )
    directories: list[Path] = Field(
        default_factory=list, description="Extra directories scanned for plugin.toml manifests."
    )
    allow_network: bool = Field(
        default=False, description="Global gate for the `network` capability."
    )
    allow_gpu: bool = True
    allow_subprocess_plugins: bool = True
    allow_remote_plugins: bool = Field(
        default=True, description="Permit the host to call HTTP-transport analyzers."
    )
    remote: dict[str, RemotePluginConfig] = Field(
        default_factory=dict, description="plugin id -> HTTP endpoint definition."
    )
    per_plugin: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="Opaque config handed to each plugin as ctx.config."
    )
    auto_backfill_on_register: bool = Field(
        default=False, description="Enqueue a newly-discovered plugin across the library."
    )

    def is_enabled(self, plugin_id: str) -> bool:
        """Resolve the three-way enable/disable/enable_all precedence."""
        if plugin_id in self.disabled:
            return False
        if self.enable_all:
            return True
        return plugin_id in self.enabled


class SearchConfig(BaseModel):
    """Query planner defaults."""

    model_config = {"extra": "forbid"}

    default_page_size: int = Field(default=50, ge=1, le=1000)
    max_page_size: int = Field(default=500, ge=1, le=10_000)
    max_facet_values: int = Field(default=50, ge=1)
    facet_min_count: int = Field(default=1, ge=1)
    default_confidence_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    vector_backend: Literal["auto", "sqlite-vec", "numpy"] = "auto"
    vector_cache_size: int = Field(
        default=250_000, ge=0, description="Vectors held in the in-memory NumPy matrix."
    )
    fts_snippet_chars: int = Field(default=200, ge=0)


class ApiConfig(BaseModel):
    """HTTP service binding and auth.

    Loopback needs no token. Any other bind refuses to start without one —
    checked here at config-validation time so it fails before the socket opens.
    """

    model_config = {"extra": "forbid"}

    host: str = "127.0.0.1"
    port: int = Field(default=8420, ge=1, le=65535)
    auth_token: str | None = None
    cors_origins: list[str] = Field(default_factory=list)
    docs_enabled: bool = True
    max_upload_bytes: int = Field(default=100 * 1024**2, ge=0)
    request_timeout_s: float = Field(default=120.0, gt=0)

    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @model_validator(mode="after")
    def _require_token_off_loopback(self) -> "ApiConfig":
        if not self.is_loopback() and not self.auth_token:
            raise ValueError(
                f"api.host={self.host!r} is not loopback, so api.auth_token is required. "
                "Set it in config.yaml or via MEDIAENGINE__API__AUTH_TOKEN. "
                "Refusing to expose an unauthenticated media index on a network interface."
            )
        if self.auth_token is not None and len(self.auth_token) < 16:
            raise ValueError("api.auth_token must be at least 16 characters")
        return self


class LoggingConfig(BaseModel):
    """Log destination and shape."""

    model_config = {"extra": "forbid"}

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["json", "text"] = "json"
    file: Path | None = Path("./data/engine.log")
    max_bytes: int = Field(default=32 * 1024**2, ge=0)
    backup_count: int = Field(default=3, ge=0)
    console: bool = True


class Config(BaseSettings):
    """Top-level configuration object."""

    model_config = SettingsConfigDict(
        env_prefix="MEDIAENGINE__",
        env_nested_delimiter="__",
        extra="forbid",
        validate_assignment=True,
    )

    library: LibraryConfig = Field(default_factory=LibraryConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    source_path: Path | None = Field(
        default=None, exclude=True, description="Config file this was loaded from, if any."
    )

    def resolve_paths(self, base: Path) -> "Config":
        """Rewrite every relative path as absolute, anchored at ``base``.

        Called once at load time. Returns ``self`` for chaining.
        """
        base = base.resolve()

        def anchor(p: Path) -> Path:
            return p if p.is_absolute() else (base / p).resolve()

        self.library.roots = [anchor(r) for r in self.library.roots]
        self.library = self.library  # trigger validate_assignment
        self.storage.db_path = anchor(self.storage.db_path)
        self.storage.derivatives_path = anchor(self.storage.derivatives_path)
        self.plugins.directories = [anchor(d) for d in self.plugins.directories]
        if self.logging.file is not None:
            self.logging.file = anchor(self.logging.file)
        return self

    def ensure_directories(self) -> None:
        """Create the directories the engine writes into.

        Refuses if the derivative cache would live inside a library root —
        that would make the engine index its own output on the next scan.
        """
        self.storage.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage.derivatives_path.mkdir(parents=True, exist_ok=True)
        if self.logging.file is not None:
            self.logging.file.parent.mkdir(parents=True, exist_ok=True)

        deriv = self.storage.derivatives_path.resolve()
        for root in self.library.roots:
            try:
                deriv.relative_to(root.resolve())
            except ValueError:
                continue
            raise ConfigError(
                f"storage.derivatives_path ({deriv}) is inside library root ({root}). "
                "The engine would index its own thumbnails. Move it outside every root."
            )

    def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        """Opaque per-plugin settings, handed to the plugin as ``ctx.config``."""
        return dict(self.plugins.per_plugin.get(plugin_id, {}))


def default_config() -> Config:
    """A valid config with no library roots. Useful for tests and first run."""
    return Config()


def save_config(config: Config, path: str | os.PathLike[str] | None = None) -> Path:
    """Atomically persist a validated configuration.

    Runtime managers use this instead of hand-editing YAML, so plugin-shop and
    API changes have the same validation and crash-safety as desktop settings.
    An explicit destination is required when the config was not loaded from a
    file; silently inventing a config location would make deployments
    impossible to reason about.
    """

    selected = Path(path) if path is not None else config.source_path
    if selected is None:
        raise ConfigError("cannot persist config without a destination path")
    destination = selected.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json", exclude={"source_path"})
    rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, destination)
    except OSError as exc:
        raise ConfigError(f"could not save configuration to {destination}: {exc}") from exc
    config.source_path = destination
    return destination


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate configuration.

    Resolution order:

    1. ``path`` if given.
    2. ``$MEDIAENGINE_CONFIG``.
    3. ``./config.yaml``.
    4. Built-in defaults (no file).

    Environment variables always override file values.
    """
    candidate: Path | None = None
    if path is not None:
        candidate = Path(path)
        if not candidate.exists():
            raise ConfigError(f"config file not found: {candidate}")
    elif env := os.environ.get("MEDIAENGINE_CONFIG"):
        candidate = Path(env)
        if not candidate.exists():
            raise ConfigError(f"MEDIAENGINE_CONFIG points at a missing file: {candidate}")
    elif Path("config.yaml").exists():
        candidate = Path("config.yaml")

    data: dict[str, Any] = {}
    if candidate is not None:
        try:
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{candidate}: invalid YAML: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{candidate}: top level must be a mapping, got {type(raw).__name__}")
        data = raw

    try:
        cfg = Config(**data)
    except Exception as exc:  # pydantic ValidationError
        where = str(candidate) if candidate else "<defaults+env>"
        raise ConfigError(f"invalid configuration ({where}): {exc}") from exc

    base = candidate.parent if candidate is not None else Path.cwd()
    cfg.resolve_paths(base)
    cfg.source_path = candidate.resolve() if candidate is not None else None
    return cfg
