-- User-linked identity profiles and explicitly fetched public biographies.

CREATE TABLE identity_profiles (
  id             INTEGER PRIMARY KEY,
  identity_id    INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
  provider       TEXT NOT NULL,
  handle         TEXT,
  profile_url    TEXT NOT NULL,
  display_label  TEXT,
  user_confirmed INTEGER NOT NULL DEFAULT 1,
  metadata_json  TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(identity_id, provider, profile_url),
  CHECK (user_confirmed IN (0,1))
);
CREATE INDEX idx_identity_profiles_identity ON identity_profiles(identity_id);
CREATE INDEX idx_identity_profiles_provider ON identity_profiles(provider);

CREATE TABLE identity_biographies (
  identity_id    INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
  provider       TEXT NOT NULL DEFAULT 'wikipedia',
  language       TEXT NOT NULL DEFAULT 'en',
  page_id        INTEGER,
  page_title     TEXT NOT NULL,
  source_url     TEXT NOT NULL,
  summary        TEXT NOT NULL,
  description    TEXT,
  wikibase_item  TEXT,
  source_revision INTEGER,
  retrieved_at   TEXT NOT NULL,
  metadata_json  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(identity_id, provider, language)
);
CREATE INDEX idx_identity_biographies_provider
  ON identity_biographies(provider, language);
