# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Loads the configuration.

Everything a company sets lives in the database and is edited in the dashboard's Settings:
the company, people, themes, models and sources. A new installation starts from the
built-in defaults (defaults.py). Settings are re-read every SETTINGS_TTL seconds, so a change
in the dashboard reaches every running service.

What isn't a setting comes from the environment: the database (DATABASE_URL, a Secret Manager
secret on Cloud Run) and the keys (ANTHROPIC_API_KEY and the like, also secrets)."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import secrets  # noqa: F401  (trims the secrets in the environment when imported)
from .defaults import defaults
from .models import Person

log = logging.getLogger(__name__)

SOURCE_TYPES = [
    "slack", "clickup", "meeting", "document", "voicenote", "braindump",
    "interview", "linkedin", "marketing", "web", "email", "other",
]
TYPE_LABELS = {
    "slack": "Slack message", "clickup": "ClickUp message", "meeting": "Meeting", "document": "Document",
    "voicenote": "Voice note", "braindump": "Braindump", "interview": "Interview", "linkedin": "LinkedIn post",
    "marketing": "Marketing", "web": "Web page", "email": "Email", "other": "Other",
}
# The ways in, as items record them (Item.via).
SOURCES = {
    "slack": "Slack", "clickup": "ClickUp", "email": "Email", "drive": "Google Drive", "web": "Web form",
    "api": "API", "mcp": "Claude (MCP)", "cli": "Command line", "dashboard": "Dashboard",
}
VISIBILITIES = ["public", "private"]
STATUSES = ["new", "reviewed", "used", "archived"]
# Everything a company sets, saved in the database and edited in the dashboard.
DATABASE_KEYS = ("company", "llm", "fast", "transcription", "extraction", "themes", "narratives", "formats", "prompts",
                 "sources")
KEY_ENVS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
SETTINGS_TTL = 30


@dataclass
class Tag:
    name: str
    description: str = ""


@dataclass
class ModelConfig:
    provider: str
    model: str
    api_key_env: str

    @property
    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(f"{self.api_key_env} is not set. Add it as a secret (see the Status page).")
        return key


@dataclass
class Config:
    company: str
    llm: ModelConfig
    max_input_chars: int
    transcription: ModelConfig | None
    extraction: ModelConfig | None
    storage: dict[str, Any]
    people: list[Person]
    themes: list[Tag]
    narratives: list[Tag]
    formats: list[str]
    sources: dict[str, Any]
    prompts: dict[str, str] = field(default_factory=dict)
    settings_version: int | None = None  # the database version in use; None means the built-in defaults
    fast: ModelConfig | None = None  # a cheaper model for quick jobs, such as finding a chat session's topics

    def default_visibility(self, source_type: str) -> str:
        return self.sources.get("default_visibility", {}).get(source_type, "private")

    @property
    def quick(self) -> ModelConfig:
        """The model for quick jobs: `fast` if set, else `llm`."""
        return self.fast or self.llm

    @property
    def reader(self) -> ModelConfig:
        """The model that reads files (images, scanned PDFs): `extraction` if set, else `llm`."""
        return self.extraction or self.llm

    def files(self) -> dict[str, Any]:
        return self.sources.get("files", {})

    def find_person(self, value: str) -> Person | None:
        """By email, Slack ID, name or another spelling of the name."""
        return next((p for p in self.people if p.matches(value)), None)

    def person_by_email(self, email: str) -> Person | None:
        return self.find_person(email) if "@" in email else None

    def person_by_slack_id(self, slack_id: str) -> Person | None:
        return next((p for p in self.people if slack_id and p.slack_id == slack_id), None)

    def database_settings(self) -> dict[str, Any]:
        """The dashboard-edited settings, as they'd be saved."""
        model = lambda m, extra=None: {"provider": m.provider, "model": m.model, **(extra or {})} if m else None  # noqa: E731
        return {
            "company": self.company,
            "llm": model(self.llm, {"max_input_chars": self.max_input_chars}),
            "fast": model(self.fast),
            "transcription": model(self.transcription),
            "extraction": model(self.extraction),
            "themes": [{"name": t.name, "description": t.description} for t in self.themes],
            "narratives": [{"name": n.name, "description": n.description} for n in self.narratives],
            "formats": list(self.formats),
            "prompts": dict(self.prompts),
            "sources": self.sources,
        }


def _model(raw: dict[str, Any] | None, providers: dict[str, str]) -> ModelConfig | None:
    if not raw or not raw.get("provider"):
        return None
    provider = raw["provider"]
    if provider not in providers:
        raise ValueError(f"Unknown provider '{provider}'. Use one of: {', '.join(providers)}")
    if not raw.get("model"):
        raise ValueError(f"Name the {provider} model to use.")
    return ModelConfig(provider=provider, model=raw["model"], api_key_env=raw.get("api_key_env") or providers[provider])


def on_cloud_run() -> bool:
    """A Cloud Run service sets K_SERVICE; a Cloud Run job sets CLOUD_RUN_JOB."""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"))


def storage() -> dict[str, Any]:
    """Postgres when DATABASE_URL is set. Otherwise SQLite, for development and tests only:
    on Cloud Run a missing DATABASE_URL is an error rather than a database that vanishes."""
    if os.environ.get("DATABASE_URL"):
        return {"backend": "postgres"}
    if on_cloud_run():
        raise RuntimeError("DATABASE_URL isn't set, so there's no database. The setup stores it as the "
                           "tle-database-url secret.")
    return {"backend": "sqlite", "sqlite": {"path": os.environ.get("TLE_SQLITE_PATH", "data/thought_leadership.db")}}


def build_config(settings: dict[str, Any] | None = None, people: list[Person] | None = None,
                 settings_version: int | None = None) -> Config:
    """The built-in defaults, with `settings` (from the database) on top, and the people."""
    raw = defaults()
    for key, value in (settings or {}).items():
        if key not in DATABASE_KEYS:
            continue
        if key == "sources":
            # Saved sources keep any source added to the defaults since they were saved.
            raw["sources"] = {**raw["sources"], **(value or {})}
        elif key == "llm":
            raw["llm"] = value or raw["llm"]
        else:
            raw[key] = value
    llm = _model(raw["llm"], KEY_ENVS)
    return Config(
        company=raw.get("company") or "",
        llm=llm,
        max_input_chars=int(raw["llm"].get("max_input_chars", 150_000)),
        fast=_model(raw.get("fast"), KEY_ENVS),
        transcription=_model(raw.get("transcription"), {k: v for k, v in KEY_ENVS.items() if k != "anthropic"}),
        extraction=_model(raw.get("extraction"), KEY_ENVS),
        storage=storage(),
        people=people or [],
        themes=[Tag(**t) for t in raw.get("themes") or []],
        narratives=[Tag(**n) for n in raw.get("narratives") or []],
        formats=raw.get("formats") or [],
        sources=raw.get("sources") or {},
        prompts=raw.get("prompts") or {},
        settings_version=settings_version,
    )


class _ConfigCache:
    """get_config(): the built-in defaults plus the database, cached for SETTINGS_TTL seconds."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache_clear()

    def cache_clear(self) -> None:
        self.value, self.loaded_at = None, 0.0

    def __call__(self) -> Config:
        with self.lock:
            if self.value is None or time.monotonic() - self.loaded_at > SETTINGS_TTL:
                self.value, self.loaded_at = self._load(), time.monotonic()
            return self.value

    @staticmethod
    def _load() -> Config:
        try:
            from .store import store_for

            store = store_for(storage())
            latest = store.latest_settings()
            people = store.list_people()
        except Exception as exc:  # noqa: BLE001
            # Before the tables exist (the very first start), run on the built-in defaults.
            log.info("Running on the built-in settings until the database is ready (%s).", exc)
            return build_config()
        return build_config(latest.data if latest else None, people, latest.version if latest else None)


get_config = _ConfigCache()
