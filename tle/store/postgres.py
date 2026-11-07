from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..models import Event, Item, Person, SettingsVersion
from .base import EventQuery, SearchQuery, Store, TagRow, read_schema

ARRAY_FILTERS = {"themes": "theme_tags", "narratives": "narrative_tags", "authors": "authors",
                 "author_ids": "author_ids", "flags": "flags"}


class PostgresStore(Store):
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _all(self, sql: str, params=None) -> list[dict]:
        with self._connect() as conn:
            return conn.execute(sql, params or {}).fetchall()

    def _exec(self, sql: str, params=None) -> int:
        with self._connect() as conn:
            return conn.execute(sql, params or {}).rowcount

    def init(self) -> None:
        with self._connect() as conn:
            # The API and the dashboard both do this when they start; one at a time.
            conn.execute("SELECT pg_advisory_xact_lock(7140212)")
            conn.execute(read_schema("postgres"))

    # --- Items ---------------------------------------------------------------

    def _insert(self, item: Item) -> None:
        data = item.model_dump()
        data["source_metadata"] = Jsonb(data["source_metadata"])
        cols = ", ".join(data)
        marks = ", ".join(f"%({c})s" for c in data)
        self._exec(f"INSERT INTO items ({cols}) VALUES ({marks})", data)

    def get(self, row_id: str) -> Item | None:
        rows = self._all("SELECT * FROM items WHERE row_id = %(id)s", {"id": row_id})
        return Item(**rows[0]) if rows else None

    def _update(self, row_id: str, fields: dict) -> Item | None:
        if "source_metadata" in fields:
            fields["source_metadata"] = Jsonb(fields["source_metadata"])
        sets = ", ".join(f"{k} = %({k})s" for k in fields)
        self._exec(f"UPDATE items SET {sets} WHERE row_id = %(row_id)s", {**fields, "row_id": row_id})
        return self.get(row_id)

    def delete(self, row_id: str) -> None:
        self._exec("DELETE FROM items WHERE row_id = %(id)s", {"id": row_id})

    def search(self, q: SearchQuery) -> list[Item]:
        where, params = [], {}
        if q.text:
            where.append("(strpos(lower(extracted_text), lower(%(text)s)) > 0 "
                         "OR strpos(lower(summary), lower(%(text)s)) > 0)")
            params["text"] = q.text
        for attr, col in ARRAY_FILTERS.items():
            if getattr(q, attr):
                where.append(f"{col} && %({attr})s")
                params[attr] = getattr(q, attr)
        if q.flagged:
            where.append("cardinality(flags) > 0")
        for col, attr in (("source_type", "source_types"), ("status", "statuses")):
            if getattr(q, attr):
                where.append(f"{col} = ANY(%({attr})s)")
                params[attr] = getattr(q, attr)
        for col in ("visibility", "status"):
            if getattr(q, col):
                where.append(f"{col} = %({col})s")
                params[col] = getattr(q, col)
        if q.date_from:
            where.append("date_created >= %(date_from)s")
            params["date_from"] = q.date_from
        if q.date_to:
            where.append("date_created <= %(date_to)s")
            params["date_to"] = q.date_to
        sql = "SELECT * FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY date_created DESC LIMIT %(limit)s OFFSET %(offset)s"
        return [Item(**r) for r in self._all(sql, {**params, "limit": q.limit, "offset": q.offset})]

    def find_by_drive_file_id(self, drive_file_id: str) -> Item | None:
        rows = self._all("SELECT * FROM items WHERE drive_file_id = %(id)s LIMIT 1", {"id": drive_file_id})
        return Item(**rows[0]) if rows else None

    def tag_rows(self, since: datetime | None = None) -> list[TagRow]:
        sql = ("SELECT date_created, source_type, visibility, authors, theme_tags, narrative_tags, deliverables "
               "FROM items WHERE status != 'archived'")
        params = {}
        if since:
            sql += " AND date_created >= %(since)s"
            params["since"] = since
        return [TagRow(**r) for r in self._all(sql, params)]

    def replace_in_lists(self, column: str, old: str, new: str | None) -> int:
        self.check_list_column(column)
        value = f"array_remove({column}, %(old)s)" if new is None else (
            # Replace, then drop a duplicate if the item already had the new value.
            f"ARRAY(SELECT DISTINCT v FROM unnest(array_replace({column}, %(old)s, %(new)s)) v)")
        return self._exec(f"UPDATE items SET {column} = {value} WHERE %(old)s = ANY({column})",
                          {"old": old, "new": new})

    # --- Events --------------------------------------------------------------

    def add_event(self, event: Event) -> None:
        data = event.model_dump()
        data["details"] = Jsonb(data["details"])
        cols = ", ".join(data)
        self._exec(f"INSERT INTO events ({cols}) VALUES ({', '.join(f'%({c})s' for c in data)})", data)

    def repeat_event(self, key: str, time) -> bool:
        return self._exec("UPDATE events SET count = count + 1, last_time = %(time)s WHERE key = %(key)s",
                          {"key": key, "time": time}) > 0

    def list_events(self, q: EventQuery) -> list[Event]:
        where, params = [], {"limit": q.limit, "offset": q.offset}
        if q.kinds:
            where.append("kind = ANY(%(kinds)s)")
            params["kinds"] = q.kinds
        for col in ("level", "via", "row_id"):
            if getattr(q, col):
                where.append(f"{col} = %({col})s")
                params[col] = getattr(q, col)
        if q.unread:
            where.append("NOT read")
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY time DESC LIMIT %(limit)s OFFSET %(offset)s"
        return [Event(**r) for r in self._all(sql, params)]

    def count_unread(self, level: str | None = None) -> int:
        if level:
            return self._all("SELECT COUNT(*) AS n FROM events WHERE NOT read AND level = %(level)s",
                             {"level": level})[0]["n"]
        return self._all("SELECT COUNT(*) AS n FROM events WHERE NOT read")[0]["n"]

    def mark_read(self, ids: list[str] | None = None) -> None:
        if ids is None:
            self._exec("UPDATE events SET read = TRUE WHERE NOT read")
        elif ids:
            self._exec("UPDATE events SET read = TRUE WHERE id = ANY(%(ids)s)", {"ids": ids})

    # --- People --------------------------------------------------------------

    def list_people(self) -> list[Person]:
        return [Person(**r) for r in self._all("SELECT * FROM people ORDER BY name")]

    def save_person(self, person: Person) -> None:
        data = person.model_dump()
        cols = ", ".join(data)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in data if c != "id")
        self._exec(f"INSERT INTO people ({cols}) VALUES ({', '.join(f'%({c})s' for c in data)}) "
                   f"ON CONFLICT (id) DO UPDATE SET {updates}", data)

    def delete_person(self, person_id: str) -> None:
        self._exec("DELETE FROM people WHERE id = %(id)s", {"id": person_id})

    # --- Settings ------------------------------------------------------------

    def latest_settings(self) -> SettingsVersion | None:
        rows = self._all("SELECT * FROM settings ORDER BY version DESC LIMIT 1")
        return SettingsVersion(**rows[0]) if rows else None

    def settings_history(self, limit: int = 50) -> list[SettingsVersion]:
        return [SettingsVersion(**r) for r in
                self._all("SELECT * FROM settings ORDER BY version DESC LIMIT %(limit)s", {"limit": limit})]

    def _add_settings(self, version: SettingsVersion) -> None:
        data = version.model_dump()
        data["data"] = Jsonb(data["data"])
        self._exec("INSERT INTO settings (version, saved_at, saved_by, note, data) "
                   "VALUES (%(version)s, %(saved_at)s, %(saved_by)s, %(note)s, %(data)s)", data)
