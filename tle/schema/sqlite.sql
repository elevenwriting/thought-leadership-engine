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
  flags           TEXT NOT NULL DEFAULT '[]',
  via             TEXT NOT NULL DEFAULT '',
  reading         INTEGER NOT NULL DEFAULT 0
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

-- Parts of an item: a chat session's topics. Each points at the session's messages.
CREATE TABLE IF NOT EXISTS topics (
  id           TEXT PRIMARY KEY,
  row_id       TEXT NOT NULL,
  place        TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL,
  summary      TEXT NOT NULL DEFAULT '',
  messages     TEXT NOT NULL DEFAULT '[]',
  people       TEXT NOT NULL DEFAULT '[]',
  date_created TEXT NOT NULL,
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
  created TEXT NOT NULL
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
  messages    TEXT NOT NULL DEFAULT '[]',
  made_by     TEXT NOT NULL DEFAULT 'model',
  created     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS views_row_id ON views (row_id);
CREATE INDEX IF NOT EXISTS views_topic_id ON views (topic_id);

-- Where each scheduled sync got to (e.g. the last chat session saved in a channel), keyed by job and place.
CREATE TABLE IF NOT EXISTS sync_state (
  key     TEXT PRIMARY KEY,
  data    TEXT NOT NULL DEFAULT '{}',
  updated TEXT NOT NULL
);

-- Every saved version of the settings. The highest version is current.
CREATE TABLE IF NOT EXISTS settings (
  version  INTEGER PRIMARY KEY,
  saved_at TEXT NOT NULL,
  saved_by TEXT NOT NULL DEFAULT '',
  note     TEXT NOT NULL DEFAULT '',
  data     TEXT NOT NULL
);

-- Items saved before the way in was recorded take it from their "saved" event.
UPDATE items SET via = COALESCE((SELECT e.via FROM events e WHERE e.row_id = items.row_id
                                  AND e.kind = 'item_saved' AND e.via <> '' LIMIT 1), '')
 WHERE via = '';
