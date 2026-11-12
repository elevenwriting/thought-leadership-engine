# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

from pathlib import Path

import pytest
import yaml

# Keys and credentials a developer's machine may hold. Tests never use them, so nothing calls
# Anthropic, Slack, ClickUp or Google for real.
SECRETS = ["ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "OPENAI_API_KEY", "GEMINI_API_KEY", "SLACK_BOT_TOKEN",
           "SLACK_SIGNING_SECRET", "CLICKUP_API_TOKEN", "DATABASE_URL", "GOOGLE_APPLICATION_CREDENTIALS", "K_SERVICE", "CLOUD_RUN_JOB",
           "TLE_DEV_USER", "TLE_IAP_AUDIENCE", "TLE_SQLITE_PATH"]
FIXTURE = Path(__file__).parent / "fixtures" / "settings.yaml"


def seed(store) -> None:
    """Saves the test company (tests/fixtures/settings.yaml) as the settings and people."""
    from tle.config import DATABASE_KEYS, get_config
    from tle.models import Person

    data = yaml.safe_load(FIXTURE.read_text())
    store.init()
    store.save_settings({k: v for k, v in data.items() if k in DATABASE_KEYS}, saved_by="tests")
    for person in data.get("people", []):
        store.save_person(Person.from_config(person))
    get_config.cache_clear()


@pytest.fixture
def company():
    """A database holding the test company, and its settings."""
    from tle.config import get_config
    from tle.store import get_store

    store = get_store()
    seed(store)
    return get_config(), store


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch, tmp_path):
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    # Each test gets its own SQLite database.
    monkeypatch.setenv("TLE_SQLITE_PATH", str(tmp_path / "test.db"))
    import tle.dashboard as dashboard
    from tle.config import get_config
    from tle.store import get_store

    get_config.cache_clear()
    get_store.cache_clear()
    # Never the machine's own Google sign-in.
    monkeypatch.setattr(dashboard, "_service_account_email", ["tle-runner@test-project.iam.gserviceaccount.com"])
    # Settings' lists start empty and unconnected; tests that need them stand in for them.
    for key, choices in dashboard._choices.items():
        monkeypatch.setattr(choices, "missing", lambda key=key: f"{key} isn't connected in tests.")
        monkeypatch.setattr(choices, "value", None)
        monkeypatch.setattr(choices, "error", "")
        monkeypatch.setattr(choices, "busy", False)
        monkeypatch.setattr(choices, "at", None)
        monkeypatch.setattr(choices, "thread", None)
    yield
    get_config.cache_clear()
    get_store.cache_clear()
