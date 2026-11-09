from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..models import Event, Item, Link, Person, SettingsVersion, Topic, View
from .base import EventQuery, SearchQuery, Store, TagRow, read_schema

JSON_COLUMNS = {"source_metadata", "authors", "author_ids", "theme_tags", "narrative_tags", "deliverables", "flags"}
ARRAY_FILTERS = {"themes": "theme_tags", "narratives": "narrative_tags", "authors": "authors",
                 "author_ids": "author_ids", "flags": "flags"}
PERSON_JSON = ("emails", "aliases", "responsible_for")
# Columns added after the first release, so older databases get them on `tle init-db`.
ADDED_COLUMNS = {
    "items": {"author_ids": "TEXT NOT NULL DEFAULT '[]'", "flags": "TEXT NOT NULL DEFAULT '[]'",
              "via": "TEXT NOT NULL DEFAULT ''", "reading": "INTEGER NOT NULL DEFAULT 0"},
    "events": {"key": "TEXT NOT NULL DEFAULT ''", "count": "INTEGER NOT NULL DEFAULT 1", "last_time": "TEXT"},
}


class SQLiteStore(Store):
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()

    def _exec(self, sql: str, params=()):
        with self.lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def init(self) -> None:
        with self.lock:
            for table, columns in ADDED_COLUMNS.items():
                existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if existing:
                    for column, definition in columns.items():
                        if column not in existing:
                            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self.conn.executescript(read_schema("sqlite"))
            self.conn.commit()

    # --- Items ---------------------------------------------------------------

    def _row(self, item: Item) -> dict:
        data = item.model_dump()
        for col in JSON_COLUMNS:
            data[col] = json.dumps(data[col])
        for col in ("date_created", "date_ingested"):
            data[col] = data[col].isoformat()
        data["reading"] = int(data["reading"])
        return data

    def _item(self, row: sqlite3.Row) -> Item:
        data = dict(row)
        for col in JSON_COLUMNS:
            data[col] = json.loads(data.get(col) or ("{}" if col == "source_metadata" else "[]"))
        return Item(**data)

    def _insert(self, item: Item) -> None:
        data = self._row(item)
        cols = ", ".join(data)
        marks = ", ".join(f":{c}" for c in data)
        self._exec(f"INSERT INTO items ({cols}) VALUES ({marks})", data)

    def get(self, row_id: str) -> Item | None:
        rows = self._all("SELECT * FROM items WHERE row_id = ?", (row_id,))
        return self._item(rows[0]) if rows else None

    def _update(self, row_id: str, fields: dict) -> Item | None:
        values = {}
        for k, v in fields.items():
            if k in JSON_COLUMNS:
                v = json.dumps(v)
            elif isinstance(v, datetime):
                v = v.isoformat()
            elif isinstance(v, bool):
                v = int(v)
            values[k] = v
        sets = ", ".join(f"{k} = :{k}" for k in values)
        self._exec(f"UPDATE items SET {sets} WHERE row_id = :row_id", {**values, "row_id": row_id})
        return self.get(row_id)

    def delete(self, row_id: str) -> None:
        topics = [f"topic:{t.id}" for t in self.topics_for([row_id])]
        self.delete_links([f"item:{row_id}", *topics])
        self._exec("DELETE FROM topics WHERE row_id = ?", (row_id,))
        self._exec("DELETE FROM views WHERE row_id = ?", (row_id,))
        self._exec("DELETE FROM items WHERE row_id = ?", (row_id,))

    def search(self, q: SearchQuery) -> list[Item]:
        where, params = [], []
        if q.text:
            where.append("(instr(lower(extracted_text), ?) > 0 OR instr(lower(summary), ?) > 0 OR row_id IN "
                         "(SELECT row_id FROM topics WHERE instr(lower(title), ?) > 0 OR instr(lower(summary), ?) > 0))")
            params += [q.text.lower()] * 4
        for attr, col in ARRAY_FILTERS.items():
            values = getattr(q, attr)
            if values:
                marks = ", ".join("?" * len(values))
                where.append(f"EXISTS (SELECT 1 FROM json_each({col}) WHERE value IN ({marks}))")
                params += values
        if q.flagged:
            where.append("json_array_length(flags) > 0")
        if q.reading is not None:
            where.append("reading = ?")
            params.append(int(q.reading))
        for col, values in (("source_type", q.source_types), ("via", q.vias), ("status", q.statuses)):
            if values:
                where.append(f"{col} IN ({', '.join('?' * len(values))})")
                params += values
        for col, value in (("visibility", q.visibility), ("status", q.status)):
            if value:
                where.append(f"{col} = ?")
                params.append(value)
        if q.date_from:
            where.append("date_created >= ?")
            params.append(q.date_from.isoformat())
        if q.date_to:
            where.append("date_created <= ?")
            params.append(q.date_to.isoformat())
        if q.ingested_after:
            where.append("date_ingested > ?")
            params.append(q.ingested_after.isoformat())
        sql = "SELECT * FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {'date_ingested' if q.sort == 'added' else 'date_created'} DESC LIMIT ? OFFSET ?"
        return [self._item(r) for r in self._all(sql, [*params, q.limit, q.offset])]

    def find_by_drive_file_id(self, drive_file_id: str) -> Item | None:
        rows = self._all("SELECT * FROM items WHERE drive_file_id = ?", (drive_file_id,))
        return self._item(rows[0]) if rows else None

    def tag_rows(self, since: datetime | None = None) -> list[TagRow]:
        sql = ("SELECT date_created, source_type, via, visibility, authors, theme_tags, narrative_tags, deliverables "
               "FROM items WHERE status != 'archived' AND reading = 0")
        params = []
        if since:
            sql += " AND date_created >= ?"
            params.append(since.isoformat())
        return [
            TagRow(
                date_created=datetime.fromisoformat(r["date_created"]),
                source_type=r["source_type"],
                via=r["via"],
                visibility=r["visibility"],
                authors=json.loads(r["authors"]),
                theme_tags=json.loads(r["theme_tags"]),
                narrative_tags=json.loads(r["narrative_tags"]),
                deliverables=json.loads(r["deliverables"]),
            )
            for r in self._all(sql, params)
        ]

    def replace_in_lists(self, column: str, old: str, new: str | None) -> int:
        self.check_list_column(column)
        rows = self._all(f"SELECT row_id, {column} FROM items WHERE EXISTS "
                         f"(SELECT 1 FROM json_each({column}) WHERE value = ?)", (old,))
        for row in rows:
            values = json.loads(row[column])
            values = [new if v == old else v for v in values if not (v == old and new is None)]
            self._exec(f"UPDATE items SET {column} = ? WHERE row_id = ?",
                       (json.dumps(list(dict.fromkeys(values))), row["row_id"]))
        return len(rows)

    # --- Events --------------------------------------------------------------

    def add_event(self, event: Event) -> None:
        data = event.model_dump()
        data["time"] = data["time"].isoformat()
        data["last_time"] = data["last_time"] and data["last_time"].isoformat()
        data["details"] = json.dumps(data["details"], default=str)
        data["read"] = int(data["read"])
        cols = ", ".join(data)
        self._exec(f"INSERT INTO events ({cols}) VALUES ({', '.join(':' + c for c in data)})", data)

    def repeat_event(self, key: str, time) -> bool:
        cursor = self._exec("UPDATE events SET count = count + 1, last_time = ? WHERE key = ?", (time.isoformat(), key))
        return cursor.rowcount > 0

    def list_events(self, q: EventQuery) -> list[Event]:
        where, params = [], []
        if q.kinds:
            where.append(f"kind IN ({', '.join('?' * len(q.kinds))})")
            params += q.kinds
        for col in ("level", "via", "row_id"):
            if getattr(q, col):
                where.append(f"{col} = ?")
                params.append(getattr(q, col))
        if q.unread:
            where.append("read = 0")
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY time DESC LIMIT ? OFFSET ?"
        return [Event(**{**dict(r), "details": json.loads(r["details"]), "read": bool(r["read"])})
                for r in self._all(sql, [*params, q.limit, q.offset])]

    def count_unread(self, level: str | None = None) -> int:
        if level:
            return self._all("SELECT COUNT(*) AS n FROM events WHERE read = 0 AND level = ?", (level,))[0]["n"]
        return self._all("SELECT COUNT(*) AS n FROM events WHERE read = 0")[0]["n"]

    def mark_read(self, ids: list[str] | None = None) -> None:
        if ids is None:
            self._exec("UPDATE events SET read = 1 WHERE read = 0")
        elif ids:
            self._exec(f"UPDATE events SET read = 1 WHERE id IN ({', '.join('?' * len(ids))})", ids)

    # --- People --------------------------------------------------------------

    def list_people(self) -> list[Person]:
        return [Person(**{**dict(r), **{c: json.loads(r[c]) for c in PERSON_JSON}, "active": bool(r["active"])})
                for r in self._all("SELECT * FROM people ORDER BY name")]

    def save_person(self, person: Person) -> None:
        data = person.model_dump()
        for c in PERSON_JSON:
            data[c] = json.dumps(data[c])
        data["active"] = int(data["active"])
        cols = ", ".join(data)
        self._exec(f"INSERT OR REPLACE INTO people ({cols}) VALUES ({', '.join(':' + c for c in data)})", data)

    def delete_person(self, person_id: str) -> None:
        self._exec("DELETE FROM people WHERE id = ?", (person_id,))

    # --- Topics and links ----------------------------------------------------

    @staticmethod
    def _topic(row: sqlite3.Row) -> Topic:
        return Topic(**{**dict(row), "messages": json.loads(row["messages"]), "people": json.loads(row["people"])})

    def save_topics(self, row_id: str, topics: list[Topic]) -> None:
        self.delete_links([f"topic:{t.id}" for t in self.topics_for([row_id])])
        self._exec("DELETE FROM topics WHERE row_id = ?", (row_id,))
        self._exec("DELETE FROM views WHERE row_id = ?", (row_id,))
        for topic in topics:
            data = topic.model_dump()
            data.update(messages=json.dumps(data["messages"]), people=json.dumps(data["people"]),
                        date_created=data["date_created"].isoformat())
            self._exec(f"INSERT INTO topics ({', '.join(data)}) VALUES ({', '.join(':' + c for c in data)})", data)

    def save_topic(self, topic: Topic) -> None:
        data = topic.model_dump()
        data.update(messages=json.dumps(data["messages"]), people=json.dumps(data["people"]),
                    date_created=data["date_created"].isoformat())
        self._exec(f"INSERT OR REPLACE INTO topics ({', '.join(data)}) VALUES ({', '.join(':' + c for c in data)})", data)

    def delete_topic(self, topic_id: str) -> None:
        self.delete_links([f"topic:{topic_id}"])
        self._exec("DELETE FROM topics WHERE id = ?", (topic_id,))
        self._exec("DELETE FROM views WHERE topic_id = ?", (topic_id,))

    def topics_for(self, row_ids: list[str]) -> list[Topic]:
        if not row_ids:
            return []
        return [self._topic(r) for r in self._all(
            f"SELECT * FROM topics WHERE row_id IN ({', '.join('?' * len(row_ids))}) ORDER BY date_created, position", row_ids)]

    def get_topics(self, ids: list[str]) -> list[Topic]:
        if not ids:
            return []
        return [self._topic(r) for r in self._all(
            f"SELECT * FROM topics WHERE id IN ({', '.join('?' * len(ids))}) ORDER BY date_created, position", ids)]

    def recent_topics(self, place: str, before, limit: int = 20) -> list[Topic]:
        return [self._topic(r) for r in self._all(
            "SELECT * FROM topics WHERE place = ? AND date_created < ? ORDER BY date_created DESC, position DESC LIMIT ?",
            (place, before.isoformat(), limit))]

    def add_link(self, link: Link) -> None:
        data = link.model_dump()
        data["created"] = data["created"].isoformat()
        self._exec(f"INSERT INTO links ({', '.join(data)}) VALUES ({', '.join(':' + c for c in data)})", data)

    def links_for(self, ends: list[str]) -> list[Link]:
        if not ends:
            return []
        marks = ", ".join("?" * len(ends))
        return [Link(**dict(r)) for r in self._all(
            f"SELECT * FROM links WHERE from_id IN ({marks}) OR to_id IN ({marks}) ORDER BY created", [*ends, *ends])]

    def delete_link(self, link_id: str) -> None:
        self._exec("DELETE FROM links WHERE id = ?", (link_id,))

    def save_views(self, topic_id: str, views: list[View]) -> None:
        self._exec("DELETE FROM views WHERE topic_id = ?", (topic_id,))
        for view in views:
            data = view.model_dump()
            data.update(messages=json.dumps(data["messages"]), created=data["created"].isoformat())
            self._exec(f"INSERT INTO views ({', '.join(data)}) VALUES ({', '.join(':' + c for c in data)})", data)

    def views_for(self, row_ids: list[str]) -> list[View]:
        if not row_ids:
            return []
        return [View(**{**dict(r), "messages": json.loads(r["messages"])}) for r in self._all(
            f"SELECT * FROM views WHERE row_id IN ({', '.join('?' * len(row_ids))}) ORDER BY created, rowid", row_ids)]

    def delete_links(self, ends: list[str]) -> None:
        if ends:
            marks = ", ".join("?" * len(ends))
            self._exec(f"DELETE FROM links WHERE from_id IN ({marks}) OR to_id IN ({marks})", [*ends, *ends])

    # --- Sync state ----------------------------------------------------------

    def get_state(self, key: str) -> dict | None:
        rows = self._all("SELECT data FROM sync_state WHERE key = ?", (key,))
        return json.loads(rows[0]["data"]) if rows else None

    def set_state(self, key: str, data: dict) -> None:
        self._exec("INSERT OR REPLACE INTO sync_state (key, data, updated) VALUES (?, ?, ?)",
                   (key, json.dumps(data), datetime.now(timezone.utc).isoformat()))

    # --- Settings ------------------------------------------------------------

    def _version(self, row: sqlite3.Row) -> SettingsVersion:
        return SettingsVersion(**{**dict(row), "data": json.loads(row["data"])})

    def latest_settings(self) -> SettingsVersion | None:
        rows = self._all("SELECT * FROM settings ORDER BY version DESC LIMIT 1")
        return self._version(rows[0]) if rows else None

    def settings_history(self, limit: int = 50) -> list[SettingsVersion]:
        return [self._version(r) for r in self._all("SELECT * FROM settings ORDER BY version DESC LIMIT ?", (limit,))]

    def _add_settings(self, version: SettingsVersion) -> None:
        self._exec("INSERT INTO settings (version, saved_at, saved_by, note, data) VALUES (?, ?, ?, ?, ?)",
                   (version.version, version.saved_at.isoformat(), version.saved_by, version.note,
                    json.dumps(version.data)))
