-- One row per captured source item. Arrays and metadata are stored as JSON text.
CREATE TABLE IF NOT EXISTS items (
  row_id          TEXT PRIMARY KEY,
  date_created    TEXT NOT NULL,
  date_ingested   TEXT NOT NULL,
  source_type     TEXT NOT NULL,
  source_metadata TEXT NOT NULL DEFAULT '{}',
  authors         TEXT NOT NULL DEFAULT '[]',
  author_ids      TEXT NOT NULL DEFAULT '[]',
  drive_file_id   TEXT,
  extracted_text  TEXT NOT NULL,
  theme_tags      TEXT NOT NULL DEFAULT '[]',
  narrative_tags  TEXT NOT NULL DEFAULT '[]',
  summary         TEXT NOT NULL DEFAULT '',
  deliverables    TEXT NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'new',
  visibility      TEXT NOT NULL DEFAULT 'private',
  flags           TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS items_date_created ON items (date_created);
CREATE INDEX IF NOT EXISTS items_drive_file_id ON items (drive_file_id);

-- What happened: items saved and changed, settings changed, and problems.
CREATE TABLE IF NOT EXISTS events (
  id      TEXT PRIMARY KEY,
  time    TEXT NOT NULL,
  kind    TEXT NOT NULL,
  level   TEXT NOT NULL DEFAULT 'info',
  via     TEXT NOT NULL DEFAULT '',
  row_id  TEXT,
  actor   TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '{}',
  read    INTEGER NOT NULL DEFAULT 0,
  key     TEXT NOT NULL DEFAULT '',
  count   INTEGER NOT NULL DEFAULT 1,
  last_time TEXT
);
CREATE INDEX IF NOT EXISTS events_time ON events (time);
CREATE INDEX IF NOT EXISTS events_row_id ON events (row_id);
CREATE INDEX IF NOT EXISTS events_key ON events (key);

CREATE TABLE IF NOT EXISTS people (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT '',
  emails          TEXT NOT NULL DEFAULT '[]',
  slack_id        TEXT NOT NULL DEFAULT '',
  aliases         TEXT NOT NULL DEFAULT '[]',
  responsible_for TEXT NOT NULL DEFAULT '[]',
  active          INTEGER NOT NULL DEFAULT 1
);

-- Every saved version of the settings. The highest version is current.
CREATE TABLE IF NOT EXISTS settings (
  version  INTEGER PRIMARY KEY,
  saved_at TEXT NOT NULL,
  saved_by TEXT NOT NULL DEFAULT '',
  note     TEXT NOT NULL DEFAULT '',
  data     TEXT NOT NULL
);
