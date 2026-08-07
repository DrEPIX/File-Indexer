# Build Prompt — Modular Media Indexing Engine ("MediaEngine")

> Copy everything below the line into Fable 5.

---

You are a senior systems engineer. Build a **local-first, plugin-driven media indexing engine** in Python 3.11+ that powers a personal photo/video/document library application — the kind of thing Immich, PhotoPrism, or digiKam are, but with a first-class extension API so that *analyzers can be written independently and added later without touching the core*.

The single most important requirement: **the core knows nothing about what tags exist.** It stores, indexes, and searches arbitrary namespaced annotations produced by plugins. A plugin author (human or AI) must be able to ship a new analyzer that emits `garment.color=red`, `animal.species=dog`, `scene.setting=beach`, `ocr.text=...`, or any namespace they invent, and have it become searchable and facetable in the UI with zero core changes.

Do not implement any specific taxonomy. Implement the machinery that makes taxonomies pluggable.

---

## 0. Non-negotiable principles

Apply these everywhere; they drive most design decisions.

1. **Never modify originals.** Read-only access to source files, always. All derivatives go to a separate cache directory.
2. **Content identity, not path identity.** A file that moves or is renamed is the same asset. Two copies of the same bytes are one asset with two paths.
3. **Provenance on everything derived.** Every annotation records which producer (plugin id + version + model id + config hash) created it, when, and with what confidence. Nothing derived is anonymous.
4. **User data outranks machine data.** A human correction must survive re-indexing, model upgrades, and full re-analysis. Never let a plugin overwrite a user-confirmed value.
5. **Everything is resumable and idempotent.** Interrupt at any point; restart continues. Re-running a producer over an already-processed asset is a no-op unless the producer version changed.
6. **Everything derived is purgeable.** It must be possible to delete all output from one producer, or all derived data for one asset, in a single operation, and to re-derive it.
7. **Local by default.** No network egress from core or plugins unless a plugin explicitly declares the `network` capability and the operator enables it in config.

---

## 1. Corrections to a prior draft (do not repeat these mistakes)

An earlier version of this spec contained the following errors. Fix each one:

- **`tags(id, name, frequency, media_file_ids[])`** — arrays in a relational schema. Use a proper junction table. `frequency` is derived; make it a view or a materialized counter maintained by trigger, not a column that silently goes stale.
- **"Add geospatial indexing"** — SQLite has no native spatial index. Use the built-in **R\*Tree virtual table module** for bounding-box queries (sufficient for map viewport queries and radius search with a post-filter). Do not assume SpatiaLite is present; make it optional.
- **`asyncio` + `aiofiles` for extraction** — the bottleneck is `ffprobe`/`exiftool` process spawns and CPU-bound decode, not file reads. `aiofiles` buys nothing here. Use a bounded process pool for subprocess work and a thread pool for I/O, coordinated by a semaphore.
- **Per-file `exiftool` invocation** — spawning exiftool per file is roughly an order of magnitude slower than using its `-stay_open True -@ argfile` daemon mode. Implement a persistent exiftool worker with health checks and restart-on-crash.
- **No content hashing** — without it there is no dedup, no move detection, no stable asset identity, no cache key for derivatives. Required.
- **Single `media_files` table holding `duration`, `resolution`, `codec`, `frame_rate`** — these are NULL for images and documents. Split media-type-specific detail into separate tables or a JSON blob plus promoted hot columns.
- **No sidecar / RAW / motion-photo handling** — a real photo library must handle `.xmp` sidecars, RAW formats (CR2/CR3, NEF, ARW, DNG, RAF, ORF), and iOS/Android motion photos (paired HEIC+MOV, or embedded MP4 in JPEG).
- **Auth "if needed"** — decide it. Bind to loopback by default, require a bearer token from config for any non-loopback bind.

---

## 2. Ingest pipeline

Stages, each independently resumable:

1. **Walk** — recursive traversal with include/exclude globs, follow-symlink policy, hidden-file policy, max depth. Emit candidate paths in batches. Record a `scan_session`.
2. **Triage** — `stat()` each path. If `(path, size, mtime_ns, inode)` matches an existing `files` row, mark seen and skip. This fast path must handle a 200k-file library in seconds.
3. **Identify** — hash content (BLAKE3, or SHA-256 if blake3 unavailable). Detect media type by magic bytes (`python-magic`/`filetype`), never by extension alone. Create or link the `assets` row.
4. **Extract technical metadata** — dispatch by media type:
   - Video/audio: `ffprobe -v quiet -print_format json -show_format -show_streams -show_chapters`. Capture container, per-stream codec/resolution/fps/bit depth/color primaries/rotation, duration, audio tracks, subtitle tracks, embedded chapters.
   - Images: exiftool daemon for the full tag set (EXIF, IPTC, XMP, MakerNotes), Pillow for dimensions and quick decode checks. Parse EXIF GPS into decimal degrees, handling N/S/E/W refs and altitude ref.
   - Documents: page/word count plus text extraction — PyMuPDF for PDF, `python-docx`, `openpyxl`, plain text, EPUB. If a PDF has no text layer, mark it OCR-eligible rather than OCR-ing inline.
5. **Derive** — generate and cache: thumbnails at several sizes (webp), a video proxy if configured, and keyframes for video analysis (fixed interval plus scene-change detection). Cache is content-addressed: `derivatives/<hash[0:2]>/<hash>/thumb_512.webp`. Never write next to originals.
6. **Enqueue analysis** — for each registered analyzer whose `accepts` matches this asset, insert an `analysis_tasks` row. This is what makes backfill work: register a new plugin, enqueue it across the whole library, walk the queue.
7. **Index** — rebuild the asset's FTS row and spatial row from current annotation state.

---

## 3. Database schema

SQLite with WAL mode, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`. All writes serialized through a single writer connection/thread; readers concurrent. Provide numbered forward migrations with a `schema_version` table.

```sql
-- ── Identity ────────────────────────────────────────────────
CREATE TABLE assets (
  id              INTEGER PRIMARY KEY,
  content_hash    TEXT    NOT NULL UNIQUE,
  perceptual_hash TEXT,                      -- dhash/phash for near-dupes
  media_type      TEXT    NOT NULL,          -- image|video|audio|document|other
  mime_type       TEXT,
  size_bytes      INTEGER NOT NULL,
  captured_at     TEXT,                      -- best-known creation time, ISO8601
  captured_at_tz  TEXT,
  imported_at     TEXT    NOT NULL
);

CREATE TABLE files (                         -- N paths → 1 asset
  id            INTEGER PRIMARY KEY,
  asset_id      INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  path          TEXT    NOT NULL UNIQUE,
  filename      TEXT    NOT NULL,
  extension     TEXT,
  mtime_ns      INTEGER,
  inode         INTEGER,
  device        INTEGER,
  volume_id     TEXT,                        -- for removable/network media
  status        TEXT    NOT NULL,            -- present|missing|error|excluded
  last_seen_at  TEXT
);
CREATE INDEX idx_files_asset ON files(asset_id);

CREATE TABLE asset_relations (               -- RAW+JPEG pairs, motion photos, sidecars
  parent_asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  child_asset_id  INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  relation        TEXT NOT NULL,             -- raw_of|motion_of|sidecar_of|derivative_of
  PRIMARY KEY (parent_asset_id, child_asset_id, relation)
);

-- ── Technical metadata ──────────────────────────────────────
CREATE TABLE technical_metadata (
  asset_id     INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
  width        INTEGER, height INTEGER,      -- promoted hot columns for filtering
  duration_s   REAL,
  frame_rate   REAL,
  video_codec  TEXT,  audio_codec TEXT,
  bit_depth    INTEGER,
  orientation  INTEGER,
  page_count   INTEGER,
  raw_json     TEXT NOT NULL                 -- full ffprobe/exiftool payload
);
CREATE INDEX idx_tech_dims ON technical_metadata(width, height);

-- ── Provenance ──────────────────────────────────────────────
CREATE TABLE producers (
  id           INTEGER PRIMARY KEY,
  plugin_id    TEXT NOT NULL,
  version      TEXT NOT NULL,
  model_id     TEXT,
  config_hash  TEXT,
  UNIQUE(plugin_id, version, model_id, config_hash)
);

-- ── The generic annotation layer (the extension point) ──────
CREATE TABLE regions (
  id          INTEGER PRIMARY KEY,
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  frame_time  REAL,                          -- seconds; NULL for stills
  page_number INTEGER,                       -- documents
  x REAL, y REAL, w REAL, h REAL,            -- normalized 0..1
  producer_id INTEGER REFERENCES producers(id)
);
CREATE INDEX idx_regions_asset ON regions(asset_id);

CREATE TABLE annotations (
  id            INTEGER PRIMARY KEY,
  asset_id      INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  region_id     INTEGER REFERENCES regions(id) ON DELETE CASCADE,
  namespace     TEXT    NOT NULL,            -- 'object', 'garment.color', 'ocr'
  label         TEXT    NOT NULL,
  value_json    TEXT,                        -- structured payload, optional
  confidence    REAL,
  source        TEXT    NOT NULL,            -- embedded|derived|user
  producer_id   INTEGER NOT NULL REFERENCES producers(id),
  created_at    TEXT    NOT NULL,
  superseded_by INTEGER REFERENCES annotations(id)
);
CREATE INDEX idx_ann_lookup ON annotations(asset_id, namespace, label);
CREATE INDEX idx_ann_facet  ON annotations(namespace, label) WHERE superseded_by IS NULL;
CREATE INDEX idx_ann_producer ON annotations(producer_id);

CREATE TABLE namespaces (                    -- optional self-registration for UI
  namespace     TEXT PRIMARY KEY,
  display_name  TEXT,
  value_type    TEXT,                        -- categorical|numeric|text|geo
  facetable     INTEGER NOT NULL DEFAULT 1,
  registered_by TEXT
);

-- ── Vectors (semantic search) ───────────────────────────────
CREATE TABLE embeddings (
  id          INTEGER PRIMARY KEY,
  asset_id    INTEGER REFERENCES assets(id) ON DELETE CASCADE,
  region_id   INTEGER REFERENCES regions(id) ON DELETE CASCADE,
  producer_id INTEGER NOT NULL REFERENCES producers(id),
  dim         INTEGER NOT NULL,
  vector      BLOB    NOT NULL               -- packed float32
);

-- ── Identities (clusters ≠ people) ──────────────────────────
CREATE TABLE clusters (
  id INTEGER PRIMARY KEY,
  producer_id INTEGER NOT NULL REFERENCES producers(id),
  centroid BLOB, size INTEGER, created_at TEXT
);
CREATE TABLE identities (
  id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  notes TEXT
);
CREATE TABLE region_identity (
  region_id   INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
  identity_id INTEGER REFERENCES identities(id) ON DELETE CASCADE,
  cluster_id  INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
  confidence  REAL,
  source      TEXT NOT NULL,                 -- derived|user
  confirmed   INTEGER NOT NULL DEFAULT 0,    -- user-confirmed → immutable by plugins
  PRIMARY KEY (region_id)
);

-- ── Places ──────────────────────────────────────────────────
CREATE TABLE places (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL, kind TEXT,
  latitude REAL, longitude REAL,
  country TEXT, region TEXT, city TEXT,
  parent_id INTEGER REFERENCES places(id)
);
CREATE TABLE asset_locations (
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  latitude    REAL NOT NULL, longitude REAL NOT NULL,
  altitude_m  REAL, accuracy_m REAL,
  place_id    INTEGER REFERENCES places(id),
  source      TEXT NOT NULL,                 -- exif|user|derived
  producer_id INTEGER REFERENCES producers(id),
  PRIMARY KEY (asset_id, source)
);
CREATE VIRTUAL TABLE asset_geo USING rtree(id, min_lat, max_lat, min_lon, max_lon);

-- ── Tags (normalized; note the junction table) ──────────────
CREATE TABLE tags (
  id INTEGER PRIMARY KEY,
  namespace TEXT NOT NULL DEFAULT 'user',
  name TEXT NOT NULL,
  UNIQUE(namespace, name)
);
CREATE TABLE asset_tags (
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  source      TEXT NOT NULL,
  producer_id INTEGER REFERENCES producers(id),
  confidence  REAL,
  PRIMARY KEY (asset_id, tag_id, source)
);
CREATE VIEW tag_frequency AS
  SELECT tag_id, COUNT(DISTINCT asset_id) AS n FROM asset_tags GROUP BY tag_id;

-- ── Jobs ────────────────────────────────────────────────────
CREATE TABLE scan_sessions (
  id INTEGER PRIMARY KEY, root_path TEXT NOT NULL,
  started_at TEXT, finished_at TEXT,
  files_seen INTEGER DEFAULT 0, files_new INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0, state TEXT NOT NULL,
  cursor TEXT                                -- for resume
);
CREATE TABLE analysis_tasks (
  id INTEGER PRIMARY KEY,
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  plugin_id TEXT NOT NULL, plugin_version TEXT NOT NULL,
  state TEXT NOT NULL,                       -- pending|running|done|failed|skipped
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  scheduled_at TEXT, started_at TEXT, finished_at TEXT,
  UNIQUE(asset_id, plugin_id, plugin_version)
);
CREATE INDEX idx_tasks_queue ON analysis_tasks(state, plugin_id);

CREATE TABLE errors (
  id INTEGER PRIMARY KEY, scope TEXT, ref_id INTEGER,
  kind TEXT NOT NULL, message TEXT, traceback TEXT, occurred_at TEXT
);

-- ── Full-text ───────────────────────────────────────────────
CREATE VIRTUAL TABLE search_index USING fts5(
  filename, tags, labels, doc_text, place_names, people,
  content='', tokenize='unicode61 remove_diacritics 2'
);
-- contentless: rowid = assets.id; on update, DELETE then INSERT.
```

---

## 4. The plugin contract — build this first, everything else serves it

### 4.1 In-process analyzers

```python
from typing import Protocol, Iterable, Literal

class Annotation(BaseModel):
    namespace: str                  # dotted, e.g. "garment.color"
    label: str
    value: dict | None = None
    confidence: float | None = None
    region: Region | None = None    # bbox + optional frame_time/page
    embedding: list[float] | None = None

class Capabilities(BaseModel):
    pixels: bool = False            # needs decoded image
    frames: bool = False            # needs video keyframes
    audio: bool = False
    text: bool = False
    metadata_only: bool = False
    gpu: bool = False
    network: bool = False           # denied unless operator opts in
    max_concurrency: int | None = None

class Analyzer(Protocol):
    id: str                         # stable, e.g. "acme.object-detector"
    version: str                    # bump → automatic re-analysis
    accepts: set[MediaType]
    requires: Capabilities
    emits: list[str]                # namespaces, for UI registration

    def analyze(self, ctx: "AnalysisContext") -> Iterable[Annotation]: ...
```

`AnalysisContext` exposes **lazy, cached** accessors so cheap plugins never pay decode cost and expensive decodes are shared across all plugins running on the same asset:

```python
ctx.asset_id, ctx.media_type, ctx.content_hash
ctx.metadata               -> dict          # already-extracted technical metadata
ctx.thumbnail(size=512)    -> PIL.Image
ctx.image()                -> PIL.Image     # full-res, orientation-corrected
ctx.frames(every=1.0)      -> Iterator[tuple[float, PIL.Image]]
ctx.audio_path()           -> Path          # extracted wav, cached
ctx.text()                 -> str           # documents / OCR output
ctx.annotations(namespace=None) -> list[Annotation]   # what earlier plugins found
ctx.logger, ctx.config, ctx.cancel_token
```

Plugins **return** annotations; they never write to the database directly. The core assigns producer ids, resolves conflicts against user data, and commits transactionally.

**Dependency ordering:** a plugin may declare `depends_on = ["core.face-detector"]`. The scheduler topologically sorts and guarantees dependencies have completed for that asset. This is how a face-*recognition* plugin consumes regions from a face-*detection* plugin, or an attribute classifier consumes detection boxes from an object detector.

### 4.2 Out-of-process analyzers

Heavyweight model plugins must be able to run in their own process, their own virtualenv, and their own language. Support a subprocess contract: JSON Lines over stdin/stdout, with a `plugin.toml` manifest declaring the same fields as the Protocol above. The host sends a work item (asset id, paths to cached derivatives, prior annotations); the plugin replies with annotations or an error. Crashes are isolated and the task is retried with backoff, then marked failed.

Provide a reference implementation of *both* forms and document the contract in `/docs/PLUGINS.md` well enough that someone can write a conforming plugin from the doc alone.

### 4.3 Discovery and lifecycle

- Discover via `importlib.metadata` entry points, group `mediaengine.analyzers`, **plus** a scan of a `plugins/` directory for manifests.
- `GET /api/plugins` lists installed plugins, versions, enabled state, and per-plugin task counts.
- `POST /api/plugins/{id}/backfill` enqueues that plugin across the whole library (or a filtered subset).
- Bumping `version` invalidates prior tasks for that plugin and re-enqueues; the old annotations remain until the new ones commit, then are marked `superseded_by`.
- `DELETE /api/producers/{id}/annotations` purges everything one producer ever wrote.

### 4.4 Ship these reference plugins

Three thin ones only — they exist to prove the contract, not to be good models:

- `core.exif-entities` — pulls creator/artist/copyright/description/keywords from embedded metadata into annotations. Metadata-only, no pixels.
- `core.exif-gps` — EXIF GPS → `asset_locations` + R-tree row.
- `example.stub-classifier` — emits two hardcoded labels in namespace `example.demo`, so plugin authors have a working template to copy.

Do **not** build face recognition, object detection, or any attribute classifier. Those are downstream plugins written against this contract. The core's job is to make them trivially droppable.

---

## 5. Search

One query planner that composes four index types and returns unified, paginated, facetable results:

1. **Structured filters** — media type, date range, dimensions, duration, camera make/model, file size, folder, has-location, has-faces.
2. **Full-text** — FTS5 over filename, tags, annotation labels, document text, place names, identity names.
3. **Spatial** — R-tree bbox for map viewport; radius search as bbox pre-filter + haversine post-filter.
4. **Vector** — cosine similarity over `embeddings` for semantic/"find similar" queries. Use `sqlite-vec` if available; otherwise brute-force NumPy over a memory-mapped matrix (fine to ~1M vectors) behind an interface so the backend is swappable.

**Facets must be computed dynamically from the `annotations` table**, grouped by namespace. The UI renders filter controls for namespaces it has never seen. This is the payoff of the whole design — a new plugin's labels become browsable filters automatically.

Support boolean composition (`AND`/`OR`/`NOT`) across all four, a confidence threshold per namespace, and a `source` filter (show only user-confirmed, only machine-derived, or both).

---

## 6. API surface

Expose the engine **twice**: as an embeddable Python class and as an HTTP service over it. A PyQt app should be able to use it in-process without an HTTP hop.

```python
engine = MediaEngine(config)
engine.events.subscribe(handler)            # thread-safe pub/sub
job = engine.scan(root, recursive=True)     # returns handle, non-blocking
engine.search(Query(...)) -> SearchResult
engine.asset(asset_id) -> AssetDetail
```

Events flow through a `queue.Queue` the GUI polls, or a callback dispatched on the caller's thread — never a raw callback from a worker thread into Qt widgets. Event types: `scan.started`, `scan.progress`, `asset.indexed`, `analysis.progress`, `plugin.error`, `scan.finished`.

HTTP layer (FastAPI + uvicorn), all responses Pydantic-validated, OpenAPI auto-generated:

```
POST   /api/scan                        → {job_id}
GET    /api/jobs/{id}                   → progress, counts, errors
WS     /api/jobs/{id}/events            → streaming progress
GET    /api/assets/{id}                 → metadata + annotations + regions + location
GET    /api/assets/{id}/thumb?size=512  → binary
GET    /api/search?q=&filters=&cursor=  → results + facets (cursor pagination)
GET    /api/facets?namespace=           → distinct labels + counts
GET    /api/map?bbox=&zoom=             → clustered geo points
GET    /api/identities                  → named people
POST   /api/identities/{id}/confirm     → user-confirms a region→identity link
POST   /api/annotations                 → manual annotation (source='user')
GET    /api/plugins                     → installed analyzers
POST   /api/plugins/{id}/backfill       → enqueue across library
DELETE /api/producers/{id}/annotations  → purge one producer's output
```

Bind `127.0.0.1` by default. Any non-loopback bind requires a bearer token from config and must refuse to start without one.

---

## 7. Concurrency

- **One writer.** All DB writes go through a single writer thread consuming a queue, in WAL mode. This eliminates almost all lock contention. Readers use their own connections freely.
- **Bounded pools.** Separate pools for: filesystem walk (1), metadata subprocesses (`ProcessPoolExecutor`, default = cpu_count), derivative generation (cpu_count), analyzer execution (per-plugin `max_concurrency`, GPU plugins default to 1).
- **Backpressure.** Bounded queues throughout; the walker blocks rather than materializing 200k paths in memory.
- **Timeouts.** Every subprocess gets a hard timeout and is killed with its process group on expiry. Sanitize all args; never `shell=True`.
- **Graceful shutdown.** SIGINT/SIGTERM → stop accepting work, cancel in-flight analyzers via cancel token, drain the writer queue, checkpoint WAL, close cleanly. A second signal force-exits.
- **Retry.** Transient failures (lock, timeout) retry with exponential backoff; deterministic failures (corrupt file, unsupported codec) fail fast and record to `errors`.

---

## 8. Configuration

Single `config.yaml`, validated by a Pydantic settings model, env-var overridable:

```yaml
library:
  roots: []
  include: ["**/*"]
  exclude: ["**/.*", "**/node_modules/**", "**/@eaDir/**"]
  follow_symlinks: false
storage:
  db_path: ./data/library.db
  derivatives_path: ./data/derivatives
  thumbnail_sizes: [256, 512, 2048]
scan:
  hash_algorithm: blake3
  compute_perceptual_hash: true
  extract_gps: true
  video_keyframe_interval_s: 5.0
workers:
  metadata_processes: 0        # 0 = cpu_count
  derivative_workers: 0
  writer_queue_size: 10000
  subprocess_timeout_s: 60
plugins:
  enabled: ["core.exif-entities", "core.exif-gps"]
  allow_network: false
  per_plugin: {}
api:
  host: 127.0.0.1
  port: 8420
  auth_token: null
logging:
  level: INFO
  format: json
  file: ./data/engine.log
```

---

## 9. Data handling requirements

These are engineering requirements, not boilerplate — implement them:

- Derived data is **segregated and purgeable**. Face embeddings and identity links live in their own tables; provide `engine.purge_biometrics()` that removes all face regions, embeddings, clusters, and identity links in one transaction without touching the underlying media index.
- Every annotation is **attributable and reversible** — you can always answer "which model version claimed this, and when," and undo it.
- **Export and delete** must exist per-asset: full JSON dump of everything the system knows about one asset, and a hard delete of all derived records for it.
- Plugins are **network-denied by default**; the `network` capability must be explicitly granted per plugin in config, and grants are logged.
- Ship a short `docs/DATA.md` noting that face templates and similar biometric identifiers are separately regulated in some jurisdictions (Texas CUBI, Illinois BIPA, GDPR Art. 9), that any face-recognition plugin should therefore be **off by default**, and that the purge and export functions above exist to support those obligations.

---

## 10. Deliverables

```
mediaengine/
  core/        identity.py  walker.py  extractors/  derivatives.py  pipeline.py
  db/          schema.sql  migrations/  connection.py  writer.py  repositories/
  plugins/     contract.py  registry.py  runner.py  subprocess_host.py  builtin/
  search/      planner.py  fts.py  spatial.py  vectors.py  facets.py
  workers/     pool.py  scheduler.py  events.py
  api/         app.py  routes/  schemas.py  ws.py
  config.py    engine.py    cli.py    __main__.py
tests/         unit/  integration/  fixtures/   # incl. corrupt + zero-byte + no-EXIF files
docs/          README.md  ARCHITECTURE.md  PLUGINS.md  API.md  DATA.md
pyproject.toml
```

Full type annotations, `mypy --strict` clean. Docstrings on every public symbol. Tests must cover: resumable scan after kill -9, duplicate/moved file handling, a plugin that raises, a plugin that times out, unicode and very long filenames, zero-byte and truncated media, and concurrent read-during-write.

**Build in this order, each milestone runnable and demoable:**

1. Config, DB, migrations, single-writer connection layer.
2. Walker + identity + technical metadata extraction + derivatives. CLI: `scan`, `stat`.
3. Plugin contract + registry + scheduler + the three reference plugins. CLI: `plugins list`, `backfill`.
4. Search: FTS + structured filters + facets. CLI: `search`.
5. Spatial + vector search.
6. Identities/clusters layer.
7. FastAPI + WebSocket + OpenAPI.
8. Out-of-process plugin host.
9. Docs + a worked example: index a sample folder, write a toy plugin, watch its labels appear as search facets.

Do not stub or `TODO` anything in a milestone you declare complete. If a design decision is genuinely ambiguous, choose the simpler option, implement it fully, and note the tradeoff in `ARCHITECTURE.md`.
