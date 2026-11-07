-- One row per captured source item.
CREATE TABLE IF NOT EXISTS items (
  row_id          TEXT PRIMARY KEY,
  date_created    TIMESTAMPTZ NOT NULL,
  date_ingested   TIMESTAMPTZ NOT NULL,
  source_type     TEXT NOT NULL,
  source_metadata JSONB NOT NULL DEFAULT '{}',
  authors         TEXT[] NOT NULL DEFAULT '{}',
  author_ids      TEXT[] NOT NULL DEFAULT '{}',
  drive_file_id   TEXT,
  extracted_text  TEXT NOT NULL,
  theme_tags      TEXT[] NOT NULL DEFAULT '{}',
  narrative_tags  TEXT[] NOT NULL DEFAULT '{}',
  summary         TEXT NOT NULL DEFAULT '',
  deliverables    TEXT[] NOT NULL DEFAULT '{}',
  status          TEXT NOT NULL DEFAULT 'new',
  visibility      TEXT NOT NULL DEFAULT 'private',
  flags           TEXT[] NOT NULL DEFAULT '{}'
);
ALTER TABLE items ADD COLUMN IF NOT EXISTS author_ids TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE items ADD COLUMN IF NOT EXISTS flags TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS items_date_created ON items (date_created DESC);
CREATE INDEX IF NOT EXISTS items_theme_tags ON items USING GIN (theme_tags);
CREATE INDEX IF NOT EXISTS items_narrative_tags ON items USING GIN (narrative_tags);
CREATE INDEX IF NOT EXISTS items_drive_file_id ON items (drive_file_id);

-- What happened: items saved and changed, settings changed, and problems.
CREATE TABLE IF NOT EXISTS events (
  id      TEXT PRIMARY KEY,
  time    TIMESTAMPTZ NOT NULL,
  kind    TEXT NOT NULL,
  level   TEXT NOT NULL DEFAULT 'info',
  via     TEXT NOT NULL DEFAULT '',
  row_id  TEXT,
  actor   TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}',
  read    BOOLEAN NOT NULL DEFAULT FALSE,
  key     TEXT NOT NULL DEFAULT '',
  count   INTEGER NOT NULL DEFAULT 1,
  last_time TIMESTAMPTZ
);
ALTER TABLE events ADD COLUMN IF NOT EXISTS key TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN IF NOT EXISTS count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE events ADD COLUMN IF NOT EXISTS last_time TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS events_time ON events (time DESC);
CREATE INDEX IF NOT EXISTS events_row_id ON events (row_id);
CREATE INDEX IF NOT EXISTS events_key ON events (key) WHERE key <> '';

CREATE TABLE IF NOT EXISTS people (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT '',
  emails          TEXT[] NOT NULL DEFAULT '{}',
  slack_id        TEXT NOT NULL DEFAULT '',
  aliases         TEXT[] NOT NULL DEFAULT '{}',
  responsible_for TEXT[] NOT NULL DEFAULT '{}',
  active          BOOLEAN NOT NULL DEFAULT TRUE
);

-- Every saved version of the settings. The highest version is current.
CREATE TABLE IF NOT EXISTS settings (
  version  INTEGER PRIMARY KEY,
  saved_at TIMESTAMPTZ NOT NULL,
  saved_by TEXT NOT NULL DEFAULT '',
  note     TEXT NOT NULL DEFAULT '',
  data     JSONB NOT NULL
);

-- Optional, for later: semantic search with pgvector.
-- CREATE EXTENSION IF NOT EXISTS vector;
-- ALTER TABLE items ADD COLUMN embedding vector(1536);
