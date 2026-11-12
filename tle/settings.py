# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Saving the dashboard-edited settings, and preparing a fresh database."""

from __future__ import annotations

from typing import Any

from .config import DATABASE_KEYS, Config, build_config, get_config
from .models import Person, SettingsVersion


def validate(data: dict[str, Any]) -> Config:
    """Raises ValueError if these settings wouldn't load."""
    try:
        return build_config(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Those settings wouldn't load: {exc}") from exc


def save(store, data: dict[str, Any], *, actor: str, note: str = "") -> SettingsVersion:
    data = {k: data[k] for k in DATABASE_KEYS if k in data}
    validate(data)
    version = store.save_settings(data, saved_by=actor, note=note)
    get_config.cache_clear()
    return version


def current(store) -> dict[str, Any]:
    """The settings now in force: the database's, or the built-in defaults before the first save."""
    latest = store.latest_settings()
    return build_config(latest.data).database_settings() if latest else build_config().database_settings()


def prepare(store) -> bool:
    """Creates or upgrades the tables, and saves the built-in defaults as the first settings.
    Safe to run any number of times, including from two services starting at once.
    Returns whether this was a fresh database."""
    store.init()
    if store.latest_settings() is not None:
        return False
    store.save_settings(build_config().database_settings(), saved_by="setup", note="The built-in defaults")
    get_config.cache_clear()
    return True


def save_person(store, person: Person, *, actor: str, before: Person | None = None) -> None:
    store.save_person(person)
    get_config.cache_clear()
