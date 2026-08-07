-- Opt-in, locally managed face-reference packs.
--
-- Reference images are intentionally not stored.  Only embeddings and the
-- provenance needed to audit/delete them live here.  A match remains a
-- suggestion until a human explicitly accepts it.

CREATE TABLE face_reference_packs (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  version           TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  embedding_dim     INTEGER NOT NULL,
  content_hash      TEXT NOT NULL,
  source_url        TEXT NOT NULL,
  license_name      TEXT NOT NULL,
  attribution       TEXT,
  rights_statement  TEXT NOT NULL,
  retention_policy  TEXT NOT NULL,
  metadata_json     TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL,
  UNIQUE(name, version, model_id),
  CHECK (embedding_dim > 0)
);

CREATE TABLE face_reference_people (
  id            INTEGER PRIMARY KEY,
  pack_id       INTEGER NOT NULL REFERENCES face_reference_packs(id) ON DELETE CASCADE,
  external_id   TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  identity_id   INTEGER REFERENCES identities(id) ON DELETE SET NULL,
  source_url    TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  UNIQUE(pack_id, external_id)
);
CREATE INDEX idx_face_reference_people_pack ON face_reference_people(pack_id);

CREATE TABLE face_reference_embeddings (
  id            INTEGER PRIMARY KEY,
  person_id     INTEGER NOT NULL REFERENCES face_reference_people(id) ON DELETE CASCADE,
  dim           INTEGER NOT NULL,
  norm          REAL NOT NULL,
  vector        BLOB NOT NULL,
  source_ref    TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  UNIQUE(person_id, source_sha256),
  CHECK (dim > 0)
);
CREATE INDEX idx_face_reference_embeddings_person ON face_reference_embeddings(person_id);

CREATE TABLE face_match_suggestions (
  id                INTEGER PRIMARY KEY,
  region_id         INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
  pack_id           INTEGER NOT NULL REFERENCES face_reference_packs(id) ON DELETE CASCADE,
  person_id         INTEGER NOT NULL REFERENCES face_reference_people(id) ON DELETE CASCADE,
  confidence        REAL NOT NULL,
  second_confidence REAL,
  margin            REAL NOT NULL,
  model_id          TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'pending',
  created_at        TEXT NOT NULL,
  reviewed_at       TEXT,
  UNIQUE(region_id, pack_id),
  CHECK (confidence BETWEEN 0.0 AND 1.0),
  CHECK (second_confidence IS NULL OR second_confidence BETWEEN 0.0 AND 1.0),
  CHECK (margin >= 0.0),
  CHECK (status IN ('pending','accepted','rejected'))
);
CREATE INDEX idx_face_match_suggestions_status
  ON face_match_suggestions(status, confidence DESC);
CREATE INDEX idx_face_match_suggestions_person ON face_match_suggestions(person_id);
