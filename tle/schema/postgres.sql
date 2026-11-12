-- Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
-- (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

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
  flags           TEXT[] NOT NULL DEFAULT '{}',
  via             TEXT NOT NULL DEFAULT '',
  reading         BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE items ADD COLUMN IF NOT EXISTS author_ids TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE items ADD COLUMN IF NOT EXISTS flags TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE items ADD COLUMN IF NOT EXISTS via TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN IF NOT EXISTS reading BOOLEAN NOT NULL DEFAULT FALSE;
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

-- Parts of an item: a chat session's topics. Each points at the session's messages.
CREATE TABLE IF NOT EXISTS topics (
  id           TEXT PRIMARY KEY,
  row_id       TEXT NOT NULL,
  place        TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL,
  summary      TEXT NOT NULL DEFAULT '',
  messages     TEXT[] NOT NULL DEFAULT '{}',
  people       TEXT[] NOT NULL DEFAULT '{}',
  date_created TIMESTAMPTZ NOT NULL,
  position     INTEGER NOT NULL DEFAULT 0,
  made_by      TEXT NOT NULL DEFAULT 'model'
);
CREATE INDEX IF NOT EXISTS topics_row_id ON topics (row_id);
CREATE INDEX IF NOT EXISTS topics_place ON topics (place, date_created DESC);

-- "This is related to that", between topics and items: "topic:<id>" or "item:<row_id>".
CREATE TABLE IF NOT EXISTS links (
  id      TEXT PRIMARY KEY,
  from_id TEXT NOT NULL,
  to_id   TEXT NOT NULL,
  kind    TEXT NOT NULL DEFAULT 'continues',
  reason  TEXT NOT NULL DEFAULT '',
  made_by TEXT NOT NULL DEFAULT 'model',
  created TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS links_from ON links (from_id);
CREATE INDEX IF NOT EXISTS links_to ON links (to_id);

-- Positions people took in a chat session's topics (pulled out when a channel asks for views).
CREATE TABLE IF NOT EXISTS views (
  id          TEXT PRIMARY KEY,
  topic_id    TEXT NOT NULL,
  row_id      TEXT NOT NULL,
  person      TEXT NOT NULL,
  view        TEXT NOT NULL,
  stance      TEXT NOT NULL DEFAULT 'new',
  responds_to TEXT NOT NULL DEFAULT '',
  messages    TEXT[] NOT NULL DEFAULT '{}',
  made_by     TEXT NOT NULL DEFAULT 'model',
  created     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS views_row_id ON views (row_id);
CREATE INDEX IF NOT EXISTS views_topic_id ON views (topic_id);

-- Where each scheduled sync got to (e.g. the last chat session saved in a channel), keyed by job and place.
CREATE TABLE IF NOT EXISTS sync_state (
  key     TEXT PRIMARY KEY,
  data    JSONB NOT NULL DEFAULT '{}',
  updated TIMESTAMPTZ NOT NULL
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

-- Items saved before the way in was recorded take it from their "saved" event.
UPDATE items SET via = e.via FROM events e
 WHERE items.via = '' AND e.row_id = items.row_id AND e.kind = 'item_saved' AND e.via <> '';
