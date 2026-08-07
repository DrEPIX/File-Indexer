-- MediaEngine initial schema.
--
-- Conventions used throughout:
--   * Timestamps are ISO-8601 UTC strings with a 'Z' suffix, stored as TEXT.
--     SQLite has no date type; TEXT sorts correctly and is human-readable in
--     a shell, which matters more here than the bytes saved by an integer.
--   * Every derived row carries a producer_id. Nothing derived is anonymous.
--   * ON DELETE CASCADE from assets is deliberate: deleting an asset must
--     remove every derived record for it in one statement (principle 6).

-- ── Identity ─────────────────────────────────────────────────────────────────

CREATE TABLE assets (
  id              INTEGER PRIMARY KEY,
  content_hash    TEXT    NOT NULL UNIQUE,   -- blake3 or sha256 of file bytes
  hash_algorithm  TEXT    NOT NULL DEFAULT 'blake3',
  perceptual_hash TEXT,                      -- 64-bit dhash as 16 hex chars
  media_type      TEXT    NOT NULL,          -- image|video|audio|document|other
  mime_type       TEXT,
  size_bytes      INTEGER NOT NULL,
  captured_at     TEXT,                      -- best-known creation time, ISO8601
  captured_at_tz  TEXT,                      -- IANA name or ±HH:MM offset
  captured_at_source TEXT,                   -- exif|container|filesystem|user
  imported_at     TEXT    NOT NULL,
  indexed_at      TEXT,                      -- last search-index rebuild
  CHECK (media_type IN ('image','video','audio','document','other')),
  CHECK (size_bytes >= 0)
);
CREATE INDEX idx_assets_captured   ON assets(captured_at);
CREATE INDEX idx_assets_media_type ON assets(media_type);
CREATE INDEX idx_assets_phash      ON assets(perceptual_hash) WHERE perceptual_hash IS NOT NULL;
CREATE INDEX idx_assets_size       ON assets(size_bytes);

-- N paths may point at one asset (duplicates, hardlinks, copies on two disks).
CREATE TABLE files (
  id            INTEGER PRIMARY KEY,
  asset_id      INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  path          TEXT    NOT NULL UNIQUE,
  filename      TEXT    NOT NULL,
  extension     TEXT,
  parent_dir    TEXT,                        -- promoted for folder filtering
  size_bytes    INTEGER,
  mtime_ns      INTEGER,
  inode         INTEGER,
  device        INTEGER,
  volume_id     TEXT,                        -- for removable/network media
  status        TEXT    NOT NULL DEFAULT 'present',
  first_seen_at TEXT,
  last_seen_at  TEXT,
  CHECK (status IN ('present','missing','error','excluded'))
);
CREATE INDEX idx_files_asset  ON files(asset_id);
CREATE INDEX idx_files_status ON files(status);
CREATE INDEX idx_files_dir    ON files(parent_dir);
-- The triage fast path probes this index. It must be covering.
CREATE INDEX idx_files_triage ON files(path, size_bytes, mtime_ns, inode);

CREATE TABLE asset_relations (               -- RAW+JPEG pairs, motion photos, sidecars
  parent_asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  child_asset_id  INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  relation        TEXT NOT NULL,
  created_at      TEXT,
  PRIMARY KEY (parent_asset_id, child_asset_id, relation),
  CHECK (relation IN ('raw_of','motion_of','sidecar_of','derivative_of')),
  CHECK (parent_asset_id <> child_asset_id)
);
CREATE INDEX idx_relations_child ON asset_relations(child_asset_id);

-- ── Technical metadata ───────────────────────────────────────────────────────
-- Hot columns are promoted for filtering; the untruncated extractor payload
-- lives in raw_json. Media-type-specific detail that would be NULL for most
-- rows (per-stream video/audio data) stays in the JSON.

CREATE TABLE technical_metadata (
  asset_id       INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
  width          INTEGER,
  height         INTEGER,
  duration_s     REAL,
  frame_rate     REAL,
  video_codec    TEXT,
  audio_codec    TEXT,
  audio_channels INTEGER,
  sample_rate    INTEGER,
  bit_rate       INTEGER,
  bit_depth      INTEGER,
  orientation    INTEGER,
  page_count     INTEGER,
  word_count     INTEGER,
  camera_make    TEXT,
  camera_model   TEXT,
  lens_model     TEXT,
  iso            INTEGER,
  f_number       REAL,
  exposure_time  REAL,
  focal_length   REAL,
  color_space    TEXT,
  container      TEXT,
  has_alpha      INTEGER,
  is_animated    INTEGER,
  extractor      TEXT,                       -- ffprobe|exiftool|pillow|pymupdf|...
  extracted_at   TEXT,
  raw_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_tech_dims     ON technical_metadata(width, height);
CREATE INDEX idx_tech_duration ON technical_metadata(duration_s);
CREATE INDEX idx_tech_camera   ON technical_metadata(camera_make, camera_model);

-- Extracted document/OCR text, kept out of technical_metadata so a full-text
-- rebuild does not have to read the wide row.
CREATE TABLE document_text (
  asset_id    INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
  text        TEXT NOT NULL,
  char_count  INTEGER NOT NULL DEFAULT 0,
  truncated   INTEGER NOT NULL DEFAULT 0,
  ocr_eligible INTEGER NOT NULL DEFAULT 0,   -- PDF with no text layer
  extractor   TEXT,
  extracted_at TEXT
);

-- ── Provenance ───────────────────────────────────────────────────────────────

CREATE TABLE producers (
  id           INTEGER PRIMARY KEY,
  plugin_id    TEXT NOT NULL,
  version      TEXT NOT NULL,
  model_id     TEXT NOT NULL DEFAULT '',     -- '' not NULL: NULLs break UNIQUE
  config_hash  TEXT NOT NULL DEFAULT '',
  transport    TEXT NOT NULL DEFAULT 'in_process',
  first_seen_at TEXT,
  UNIQUE(plugin_id, version, model_id, config_hash),
  CHECK (transport IN ('in_process','subprocess','http','core','user'))
);
CREATE INDEX idx_producers_plugin ON producers(plugin_id);

-- ── The generic annotation layer (the extension point) ───────────────────────

CREATE TABLE regions (
  id          INTEGER PRIMARY KEY,
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  frame_time  REAL,                          -- seconds; NULL for stills
  page_number INTEGER,                       -- documents
  x REAL, y REAL, w REAL, h REAL,            -- normalized 0..1
  kind        TEXT,                          -- free-form: face|object|text|...
  producer_id INTEGER REFERENCES producers(id),
  created_at  TEXT,
  CHECK (x IS NULL OR (x >= -0.5 AND x <= 1.5)),
  CHECK (w IS NULL OR w >= 0)
);
CREATE INDEX idx_regions_asset    ON regions(asset_id);
CREATE INDEX idx_regions_producer ON regions(producer_id);
CREATE INDEX idx_regions_kind     ON regions(kind);

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
  superseded_by INTEGER REFERENCES annotations(id) ON DELETE SET NULL,
  CHECK (source IN ('embedded','derived','user')),
  CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
  CHECK (namespace <> '' AND label <> '')
);
CREATE INDEX idx_ann_lookup   ON annotations(asset_id, namespace, label);
CREATE INDEX idx_ann_facet    ON annotations(namespace, label) WHERE superseded_by IS NULL;
CREATE INDEX idx_ann_producer ON annotations(producer_id);
CREATE INDEX idx_ann_region   ON annotations(region_id);
CREATE INDEX idx_ann_source   ON annotations(source) WHERE superseded_by IS NULL;
CREATE INDEX idx_ann_live     ON annotations(asset_id) WHERE superseded_by IS NULL;
-- Idempotency: one producer may assert a given (namespace,label) once per
-- region per asset. COALESCE because SQLite treats NULLs as distinct, which
-- would let a producer insert unlimited duplicate asset-level annotations.
CREATE UNIQUE INDEX idx_ann_unique
  ON annotations(asset_id, COALESCE(region_id, -1), namespace, label, producer_id);

CREATE TABLE namespaces (                    -- optional self-registration for UI
  namespace     TEXT PRIMARY KEY,
  display_name  TEXT,
  description   TEXT,
  value_type    TEXT NOT NULL DEFAULT 'categorical',
  facetable     INTEGER NOT NULL DEFAULT 1,
  searchable    INTEGER NOT NULL DEFAULT 1,
  registered_by TEXT,
  registered_at TEXT,
  CHECK (value_type IN ('categorical','numeric','text','geo','boolean'))
);

-- ── Vectors (semantic search) ────────────────────────────────────────────────

CREATE TABLE embeddings (
  id          INTEGER PRIMARY KEY,
  asset_id    INTEGER REFERENCES assets(id) ON DELETE CASCADE,
  region_id   INTEGER REFERENCES regions(id) ON DELETE CASCADE,
  producer_id INTEGER NOT NULL REFERENCES producers(id),
  kind        TEXT NOT NULL DEFAULT 'image', -- image|text|face|audio|...
  dim         INTEGER NOT NULL,
  norm        REAL,                          -- L2 norm, precomputed for cosine
  vector      BLOB    NOT NULL,              -- packed little-endian float32
  created_at  TEXT,
  CHECK (dim > 0),
  CHECK (asset_id IS NOT NULL OR region_id IS NOT NULL)
);
CREATE INDEX idx_emb_asset    ON embeddings(asset_id);
CREATE INDEX idx_emb_region   ON embeddings(region_id);
CREATE INDEX idx_emb_producer ON embeddings(producer_id);
CREATE INDEX idx_emb_kind     ON embeddings(kind, producer_id);

-- ── Identities (clusters are not people) ─────────────────────────────────────
-- A cluster is a machine grouping of similar embeddings. An identity is a
-- human-named person. Binding one to the other is a user action; that is why
-- region_identity.confirmed exists and why plugins may never set it.

CREATE TABLE identities (
  id           INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  updated_at   TEXT,
  cover_region_id INTEGER REFERENCES regions(id) ON DELETE SET NULL,
  notes        TEXT
);

CREATE TABLE clusters (
  id          INTEGER PRIMARY KEY,
  producer_id INTEGER NOT NULL REFERENCES producers(id),
  label       TEXT,
  centroid    BLOB,
  dim         INTEGER,
  size        INTEGER NOT NULL DEFAULT 0,
  identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
  created_at  TEXT
);
CREATE INDEX idx_clusters_producer ON clusters(producer_id);

CREATE TABLE region_identity (
  region_id   INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
  identity_id INTEGER REFERENCES identities(id) ON DELETE CASCADE,
  cluster_id  INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
  confidence  REAL,
  source      TEXT NOT NULL,                 -- derived|user
  confirmed   INTEGER NOT NULL DEFAULT 0,    -- user-confirmed -> immutable by plugins
  updated_at  TEXT,
  PRIMARY KEY (region_id),
  CHECK (source IN ('derived','user')),
  CHECK (confirmed IN (0,1))
);
CREATE INDEX idx_region_identity_identity ON region_identity(identity_id);
CREATE INDEX idx_region_identity_cluster  ON region_identity(cluster_id);

-- ── Places ───────────────────────────────────────────────────────────────────

CREATE TABLE places (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  kind      TEXT,                            -- country|region|city|poi|custom
  latitude  REAL,
  longitude REAL,
  country   TEXT,
  region    TEXT,
  city      TEXT,
  parent_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  UNIQUE(name, kind, parent_id)
);
CREATE INDEX idx_places_parent ON places(parent_id);
CREATE INDEX idx_places_name   ON places(name);

CREATE TABLE asset_locations (
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  latitude    REAL NOT NULL,
  longitude   REAL NOT NULL,
  altitude_m  REAL,
  accuracy_m  REAL,
  heading_deg REAL,
  place_id    INTEGER REFERENCES places(id) ON DELETE SET NULL,
  source      TEXT NOT NULL,                 -- exif|user|derived
  producer_id INTEGER REFERENCES producers(id),
  recorded_at TEXT,
  PRIMARY KEY (asset_id, source),
  CHECK (source IN ('exif','user','derived')),
  CHECK (latitude BETWEEN -90.0 AND 90.0),
  CHECK (longitude BETWEEN -180.0 AND 180.0)
);
CREATE INDEX idx_locations_place ON asset_locations(place_id);

-- SQLite has no native spatial index. The R*Tree module gives bounding-box
-- queries, which covers map-viewport reads directly and radius search as a
-- bbox pre-filter plus a haversine post-filter. SpatiaLite is not assumed.
CREATE VIRTUAL TABLE asset_geo USING rtree(id, min_lat, max_lat, min_lon, max_lon);

-- ── Tags (normalized; junction table, no arrays) ─────────────────────────────

CREATE TABLE tags (
  id        INTEGER PRIMARY KEY,
  namespace TEXT NOT NULL DEFAULT 'user',
  name      TEXT NOT NULL,
  color     TEXT,
  parent_id INTEGER REFERENCES tags(id) ON DELETE SET NULL,
  created_at TEXT,
  UNIQUE(namespace, name)
);
CREATE INDEX idx_tags_parent ON tags(parent_id);

CREATE TABLE asset_tags (
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  source      TEXT NOT NULL,
  producer_id INTEGER REFERENCES producers(id),
  confidence  REAL,
  created_at  TEXT,
  PRIMARY KEY (asset_id, tag_id, source),
  CHECK (source IN ('embedded','derived','user'))
);
CREATE INDEX idx_asset_tags_tag ON asset_tags(tag_id);

-- frequency is derived, so it is a view. A column here would go stale the
-- moment anything wrote to asset_tags without remembering to update it.
CREATE VIEW tag_frequency AS
  SELECT tag_id, COUNT(DISTINCT asset_id) AS n
  FROM asset_tags
  GROUP BY tag_id;

-- ── Derivative cache bookkeeping ─────────────────────────────────────────────
-- Tracked so the cache can be purged per-asset or swept for orphans without
-- walking the whole derivatives tree.

CREATE TABLE derivatives (
  id           INTEGER PRIMARY KEY,
  asset_id     INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,                -- thumb|proxy|keyframe|audio|preview
  variant      TEXT NOT NULL,                -- '512', '00012.34', 'wav'
  rel_path     TEXT NOT NULL,                -- relative to storage.derivatives_path
  size_bytes   INTEGER,
  width        INTEGER,
  height       INTEGER,
  created_at   TEXT,
  UNIQUE(asset_id, kind, variant)
);
CREATE INDEX idx_derivatives_asset ON derivatives(asset_id);

-- ── Jobs ─────────────────────────────────────────────────────────────────────

CREATE TABLE scan_sessions (
  id          INTEGER PRIMARY KEY,
  root_path   TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  files_seen  INTEGER NOT NULL DEFAULT 0,
  files_new   INTEGER NOT NULL DEFAULT 0,
  files_updated INTEGER NOT NULL DEFAULT 0,
  files_skipped INTEGER NOT NULL DEFAULT 0,
  bytes_seen  INTEGER NOT NULL DEFAULT 0,
  errors      INTEGER NOT NULL DEFAULT 0,
  state       TEXT NOT NULL DEFAULT 'running',
  cursor      TEXT,                          -- last completed directory, for resume
  options_json TEXT,
  CHECK (state IN ('running','paused','finished','cancelled','failed'))
);
CREATE INDEX idx_scan_state ON scan_sessions(state);

CREATE TABLE analysis_tasks (
  id             INTEGER PRIMARY KEY,
  asset_id       INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  plugin_id      TEXT NOT NULL,
  plugin_version TEXT NOT NULL,
  state          TEXT NOT NULL DEFAULT 'pending',
  priority       INTEGER NOT NULL DEFAULT 100,   -- lower runs first
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  scheduled_at   TEXT,
  started_at     TEXT,
  finished_at    TEXT,
  heartbeat_at   TEXT,                           -- stale 'running' -> requeue
  UNIQUE(asset_id, plugin_id, plugin_version),
  CHECK (state IN ('pending','running','done','failed','skipped'))
);
CREATE INDEX idx_tasks_queue   ON analysis_tasks(state, plugin_id, priority, id);
CREATE INDEX idx_tasks_asset   ON analysis_tasks(asset_id);
CREATE INDEX idx_tasks_running ON analysis_tasks(heartbeat_at) WHERE state = 'running';

CREATE TABLE errors (
  id          INTEGER PRIMARY KEY,
  scope       TEXT,                          -- file|asset|plugin|scan|system
  ref_id      INTEGER,
  ref_text    TEXT,                          -- path or plugin id
  kind        TEXT NOT NULL,
  message     TEXT,
  traceback   TEXT,
  occurred_at TEXT NOT NULL
);
CREATE INDEX idx_errors_scope ON errors(scope, ref_id);
CREATE INDEX idx_errors_time  ON errors(occurred_at);

-- ── Plugin registry state ────────────────────────────────────────────────────
-- Persisted so a version bump is detectable across restarts and so the UI can
-- show plugins that were installed once and later removed.

CREATE TABLE plugin_registry (
  plugin_id     TEXT PRIMARY KEY,
  version       TEXT NOT NULL,
  transport     TEXT NOT NULL DEFAULT 'in_process',
  enabled       INTEGER NOT NULL DEFAULT 0,
  accepts_json  TEXT NOT NULL DEFAULT '[]',
  emits_json    TEXT NOT NULL DEFAULT '[]',
  depends_json  TEXT NOT NULL DEFAULT '[]',
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  config_hash   TEXT,
  first_seen_at TEXT,
  last_seen_at  TEXT,
  present       INTEGER NOT NULL DEFAULT 1
);

-- Grants of the `network` capability are logged. Auditability is a stated
-- requirement, not a nicety.
CREATE TABLE capability_grants (
  id         INTEGER PRIMARY KEY,
  plugin_id  TEXT NOT NULL,
  capability TEXT NOT NULL,
  granted    INTEGER NOT NULL,
  reason     TEXT,
  occurred_at TEXT NOT NULL
);
CREATE INDEX idx_grants_plugin ON capability_grants(plugin_id);

-- Generic key/value for engine state that does not deserve a table.
CREATE TABLE kv (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT
);

-- ── Full-text ────────────────────────────────────────────────────────────────
-- FTS5 in external-content mode over search_docs, keyed by assets.id.
--
-- The spec sketched a contentless table (content=''). Contentless tables
-- cannot DELETE by rowid without either SQLite >= 3.43 (contentless_delete=1)
-- or replaying the exact original column values. Re-indexing a single asset
-- after a plugin adds labels is the common operation here, so external content
-- is the simpler correct choice: search_docs holds the current text once, FTS5
-- holds only the term index, and delete/update are ordinary SQL. Storage cost
-- is the same order as contentless. Tradeoff recorded in ARCHITECTURE.md.

CREATE TABLE search_docs (
  asset_id    INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
  filename    TEXT NOT NULL DEFAULT '',
  tags        TEXT NOT NULL DEFAULT '',
  labels      TEXT NOT NULL DEFAULT '',
  doc_text    TEXT NOT NULL DEFAULT '',
  place_names TEXT NOT NULL DEFAULT '',
  people      TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE search_index USING fts5(
  filename, tags, labels, doc_text, place_names, people,
  content='search_docs',
  content_rowid='asset_id',
  tokenize='unicode61 remove_diacritics 2'
);

-- Keep the index in lockstep with the content table. Writing search_docs is
-- then the only thing the indexer has to remember to do.
CREATE TRIGGER search_docs_ai AFTER INSERT ON search_docs BEGIN
  INSERT INTO search_index(rowid, filename, tags, labels, doc_text, place_names, people)
  VALUES (new.asset_id, new.filename, new.tags, new.labels, new.doc_text, new.place_names, new.people);
END;

CREATE TRIGGER search_docs_ad AFTER DELETE ON search_docs BEGIN
  INSERT INTO search_index(search_index, rowid, filename, tags, labels, doc_text, place_names, people)
  VALUES ('delete', old.asset_id, old.filename, old.tags, old.labels, old.doc_text, old.place_names, old.people);
END;

CREATE TRIGGER search_docs_au AFTER UPDATE ON search_docs BEGIN
  INSERT INTO search_index(search_index, rowid, filename, tags, labels, doc_text, place_names, people)
  VALUES ('delete', old.asset_id, old.filename, old.tags, old.labels, old.doc_text, old.place_names, old.people);
  INSERT INTO search_index(rowid, filename, tags, labels, doc_text, place_names, people)
  VALUES (new.asset_id, new.filename, new.tags, new.labels, new.doc_text, new.place_names, new.people);
END;

-- ── Convenience views ────────────────────────────────────────────────────────

CREATE VIEW live_annotations AS
  SELECT * FROM annotations WHERE superseded_by IS NULL;

CREATE VIEW asset_primary_file AS
  SELECT asset_id, MIN(id) AS file_id, MIN(path) AS path
  FROM files
  WHERE status = 'present'
  GROUP BY asset_id;
