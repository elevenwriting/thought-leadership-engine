from __future__ import annotations

import json
import os
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
    def __call__(self) -> Store:
        return store_for(get_config().storage)

    def cache_clear(self) -> None:
        _cached.cache_clear()


get_store = _StoreGetter()
