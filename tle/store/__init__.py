from __future__ import annotations

import json
import os
import threading
from functools import lru_cache

from ..config import Config, get_config
from .base import EventQuery, SearchQuery, Store, TagRow

__all__ = ["EventQuery", "SearchQuery", "Store", "TagRow", "make_store", "get_store", "store_for"]


def make_store(config: Config) -> Store:
    return _make(config.storage)


def store_for(storage: dict) -> Store:
    """One store per storage setting, shared by the whole process."""
    return _cached(json.dumps(storage, sort_keys=True))


@lru_cache
def _cached(storage_json: str) -> Store:
    return _make(json.loads(storage_json))


def _make(storage: dict) -> Store:
    """Postgres in production; SQLite for development and tests (see config.storage)."""
    backend = storage.get("backend", "sqlite")
    if backend == "postgres":
        from .postgres import PostgresStore

        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set.")
        return PostgresStore(dsn)
    if backend == "sqlite":
        from .sqlite import SQLiteStore

        return SQLiteStore(storage.get("sqlite", {}).get("path", "data/thought_leadership.db"))
    raise ValueError(f"Unknown storage backend '{backend}'. Use postgres, or sqlite for development.")


class _StoreGetter:
    """The store for this process. The first time each one is used, its tables and first settings
    are made if they're missing (settings.prepare), whatever uses it first: a page, the API, MCP
    or a job. If that fails (the database isn't reachable yet), the next use tries again."""

    def __init__(self):
        self.lock = threading.Lock()
        self.prepared: set[int] = set()

    def __call__(self) -> Store:
        store = store_for(get_config().storage)
        if id(store) not in self.prepared:
            with self.lock:
                if id(store) not in self.prepared:
                    from ..settings import prepare

                    prepare(store)
                    self.prepared.add(id(store))
        return store

    def cache_clear(self) -> None:
        _cached.cache_clear()
        self.prepared.clear()


get_store = _StoreGetter()
