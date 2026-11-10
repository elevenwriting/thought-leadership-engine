"""The dashboard, sign-in, events, people and versioned settings, on SQLite."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tle.config import get_config
from tle.models import Event, Item, Person
from tle.store import EventQuery, SearchQuery, get_store

ROOT = Path(__file__).resolve().parents[1]


class FakeTagger:
    def __init__(self, config):
        pass

    def generate_json(self, system, prompt, schema, images=None, max_tokens=4096):
        return {"summary": "A view on pricing.", "theme_tags": ["Expertise is the moat"],
                "narrative_tags": ["Predictions"], "suggested_themes": ["Pricing"]}


@pytest.fixture
def env(company, monkeypatch):
    monkeypatch.setenv("TLE_API_TOKEN", "secret")
    monkeypatch.setattr("tle.tagger.LLM", FakeTagger)
    import tle.dashboard as dashboard

    dashboard._unread.clear()
    return company


@pytest.fixture
def client(env, monkeypatch):
    """Signed in as Jane, through TLE_DEV_USER (what IAP's header does when deployed)."""
    from tle.api import app

    monkeypatch.setenv("TLE_DEV_USER", "jane@acme.com")
    c = TestClient(app)
    c.csrf = _csrf(c.get("/dashboard/settings").text)
    return c


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _item(store, **kwargs) -> Item:
    from tle.ingest import ingest_text

    defaults = dict(source_type="meeting", authors=["Jane Smith"], via="test")
    return ingest_text("Pricing should follow value, not hours.", config=get_config(), store=store,
                       **{**defaults, **kwargs})


def test_store_tables(tmp_path):
    from tle.store.sqlite import SQLiteStore

    store = SQLiteStore(str(tmp_path / "empty.db"))
    store.init()
    store.init()  # running it twice is fine

    store.add_event(Event(kind="error", level="error", message="Couldn't read scan.pdf", via="email"))
    assert store.count_unread() == 1
    assert store.list_events(EventQuery(level="error"))[0].message == "Couldn't read scan.pdf"
    store.mark_read()
    assert store.count_unread() == 0

    person = Person(name="Jane Smith", emails=["jane@acme.com"], aliases=["Jen"])
    store.save_person(person)
    store.save_person(person.model_copy(update={"role": "CEO"}))
    assert [(p.name, p.role) for p in store.list_people()] == [("Jane Smith", "CEO")]

    v1 = store.save_settings({"company": "Acme"}, saved_by="a@acme.com")
    v2 = store.save_settings({"company": "Acme Ltd"}, saved_by="b@acme.com", note="Renamed")
    assert (v1.version, v2.version) == (1, 2)
    assert store.latest_settings().data == {"company": "Acme Ltd"}
    assert store.get_settings_version(1).data == {"company": "Acme"}

    item = Item(source_type="slack", extracted_text="x", authors=["Unknown"], theme_tags=["A", "B"],
                source_metadata={"tagging_error": "no key"})
    store.insert(item)
    assert store.get(item.row_id).flags == ["tagging_failed", "author_unclear"]
    assert store.search(SearchQuery(flagged=True))[0].row_id == item.row_id
    store.update(item.row_id, {"authors": ["Jane Smith"], "source_metadata": {}})
    assert store.get(item.row_id).flags == []
    assert store.replace_in_lists("theme_tags", "A", "B") == 1
    assert store.get(item.row_id).theme_tags == ["B"]
    store.delete(item.row_id)
    assert store.get(item.row_id) is None


def test_a_fresh_database_starts_from_the_built_in_settings(monkeypatch, tmp_path):
    from tle.settings import prepare, save

    store = get_store()  # the first use makes the tables and saves the defaults
    assert prepare(store) is False  # so there's nothing left to do
    config = get_config()
    assert config.company == "" and config.themes == [] and config.people == []
    assert config.llm.model == "claude-sonnet-5-5" and config.transcription is None
    assert "Predictions" in {n.name for n in config.narratives} and config.settings_version == 1
    # The dashboard opens on Settings, then Taxonomy, until there's a company and themes.
    from tle.api import app

    monkeypatch.setenv("TLE_DEV_USER", "jane@acme.com")
    c = TestClient(app)
    assert c.get("/dashboard", follow_redirects=False).headers["location"].startswith("/dashboard/settings?msg=Welcome")
    save(store, {**config.database_settings(), "company": "Renamed Ltd"}, actor="jane@acme.com")
    get_config.cache_clear()
    assert get_config().company == "Renamed Ltd" and get_config().settings_version == 2
    assert c.get("/dashboard", follow_redirects=False).headers["location"].startswith("/dashboard/taxonomy")
    # On Cloud Run there's no SQLite to fall back to.
    monkeypatch.setenv("K_SERVICE", "thought-leadership")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        __import__("tle.config", fromlist=["x"]).storage()


def test_sign_in_is_iap_or_the_dev_user(env, monkeypatch):
    from tle import auth
    from tle.api import app

    c = TestClient(app)
    r = c.get("/dashboard/items", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/dashboard/login")
    assert c.get("/dashboard/login").status_code == 401
    # A forged IAP header isn't accepted, with or without an audience to check it against.
    r = c.get("/dashboard/items", headers={"x-goog-iap-jwt-assertion": "forged"}, follow_redirects=False)
    assert r.status_code == 303
    monkeypatch.setenv("TLE_IAP_AUDIENCE", "/projects/1/locations/x/services/y")
    r = c.get("/dashboard/items", headers={"x-goog-iap-jwt-assertion": "forged"}, follow_redirects=False)
    assert r.status_code == 303
    # TLE_DEV_USER signs you in when developing, and never on Cloud Run.
    monkeypatch.setenv("TLE_DEV_USER", "jane@acme.com")
    assert c.get("/dashboard/items").status_code == 200
    monkeypatch.setenv("K_SERVICE", "thought-leadership")
    assert auth.dev_user() is None
    # A properly signed IAP header signs you in; one for another audience doesn't.
    import time as time_module

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from google.auth import crypt, jwt

    monkeypatch.delenv("K_SERVICE")
    monkeypatch.delenv("TLE_DEV_USER")
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(auth._iap_keys, "get", lambda refresh=False: {"k1": public.decode()})
    signer = crypt.ES256Signer.from_string(pem, "k1")
    now = int(time_module.time())

    def header(audience):
        claims = {"iss": auth.IAP_ISSUER, "aud": audience, "email": "mo@acme.com", "iat": now, "exp": now + 600}
        return {auth.IAP_HEADER: jwt.encode(signer, claims).decode()}

    assert auth.current_user(type("R", (), {"headers": header("/projects/1/locations/x/services/y")})()).email == "mo@acme.com"
    assert auth.current_user(type("R", (), {"headers": header("/projects/1/locations/x/services/other")})()) is None
    # Signing out goes through IAP.
    assert "gcp-iap-mode=CLEAR_LOGIN_COOKIE" in c.get("/dashboard/logout", follow_redirects=False).headers["location"]


def test_every_page_renders(client):
    store = get_store()
    item = _item(store)
    person = store.list_people()[0]
    for path in ["/dashboard/items?view=review", "/dashboard/items?view=problems", "/dashboard/items",
                 "/dashboard/items?theme=Expertise+is+the+moat", f"/dashboard/items/{item.row_id}",
                 "/dashboard/people", f"/dashboard/people/{person.id}", "/dashboard/taxonomy",
                 "/dashboard/coverage", "/dashboard/sources", "/dashboard/settings"]:
        r = client.get(path)
        assert r.status_code == 200, path
    review = client.get("/dashboard/items?view=review").text
    assert "Pricing" in review and 'class="review on"' in review  # the theme idea, and the view lit up
    assert 'class="problems off"' in review
    # The old pages go to the matching view.
    assert client.get("/dashboard/activity", follow_redirects=False).headers["location"] == "/dashboard/items?view=problems"
    assert client.get("/dashboard/review?flag=theme_ideas", follow_redirects=False).headers["location"] == \
        "/dashboard/items?view=review&flag=theme_ideas"


def test_forms_need_the_csrf_value(client):
    item = _item(get_store())
    r = client.post(f"/dashboard/items/{item.row_id}", data={"summary": "Changed"})
    assert r.status_code == 400


def test_editing_an_item_records_history(client):
    store = get_store()
    item = _item(store, authors=["Unknown"], source_metadata={"author_needs_review": True})
    assert "author_unclear" in item.flags
    r = client.post(f"/dashboard/items/{item.row_id}", data={
        "csrf": client.csrf, "authors": "jane@acme.com", "summary": "Edited summary", "status": "reviewed",
        "has_tags": "1", "theme_tags": ["Expertise is the moat"]}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    saved = store.get(item.row_id)
    assert saved.authors == ["Jane Smith"] and saved.author_ids and saved.summary == "Edited summary"
    assert "author_unclear" not in saved.flags
    history = store.list_events(EventQuery(row_id=item.row_id))
    assert history[0].kind == "item_updated" and history[0].actor == "jane@acme.com"
    assert set(history[0].details["changes"]) >= {"authors", "summary", "status"}
    page = client.get(f"/dashboard/items/{item.row_id}").text
    assert "Edited summary" in page and "<s>new</s> → reviewed" in page
    assert client.get("/dashboard/activity").status_code == 200


def test_renaming_a_theme_renames_it_on_items(client):
    store = get_store()
    item = _item(store)
    r = client.post("/dashboard/taxonomy/themes", data={
        "csrf": client.csrf, "old": "Expertise is the moat", "name": "Experience is the moat", "description": "x"},
        follow_redirects=False)
    assert "on 1 items" in r.headers["location"].replace("+", " ")
    assert store.get(item.row_id).theme_tags == ["Experience is the moat"]
    get_config.cache_clear()
    assert "Experience is the moat" in [t.name for t in get_config().themes]
    history = store.settings_history()
    assert history[0].note == "Renamed theme Expertise is the moat to Experience is the moat"


def test_unmatched_names_can_be_credited_to_a_person(client):
    store = get_store()
    client.get("/dashboard/people")  # imports the people from config.yaml
    jane = next(p for p in store.list_people() if p.name == "Jane Smith")
    item = _item(store, authors=["J. Smith"])
    assert "J. Smith" in client.get("/dashboard/people").text
    client.post("/dashboard/people/unmatched", data={"csrf": client.csrf, "name": "J. Smith", "person_id": jane.id})
    saved = store.get(item.row_id)
    assert saved.authors == ["Jane Smith"] and jane.id in saved.author_ids
    assert "J. Smith" in next(p for p in store.list_people() if p.id == jane.id).aliases


def test_settings_save(client, monkeypatch):
    from tle import dashboard
    from tle.connectors import slack as slack_connector

    # Without access to Slack or Drive, the section says why, is greyed out, and saving leaves it alone.
    for choices in dashboard._choices.values():
        monkeypatch.setattr(choices, "get", lambda: None)
    monkeypatch.setattr(dashboard._choices["slack"], "error", "No Slack token found.")
    monkeypatch.setattr(dashboard._choices["drive"], "error", "Drive error: Request had insufficient authentication scopes.")
    monkeypatch.setattr(dashboard._choices["clickup_places"], "error", "No ClickUp token found.")
    assert "Tagging instructions" in client.get("/dashboard/settings").text
    page = client.get("/dashboard/sources").text
    assert "Tagging instructions" not in page and "History" not in page
    assert "No Slack token found." in page and "Drive error: Request had insufficient" in page
    assert page.count('<fieldset class="section-body" disabled>') == 3  # Slack, Drive, ClickUp
    assert 'name="sections" value="slack"' not in page and 'name="sections" value="email"' in page
    client.post("/dashboard/sources", data={"csrf": client.csrf, "sections": ["slack"], "slack__channel__0__id": "CX"})
    get_config.cache_clear()
    assert get_config().sources["slack"]["channels"][0]["id"] == "C01234567"
    for choices in dashboard._choices.values():
        monkeypatch.setattr(choices, "error", "")

    # With Slack and Drive answering, they're picked from lists, and the bot joins public channels.
    for key, value in {"slack": [{"id": "C1", "name": "general", "private": False, "member": True},
                                 {"id": "C2", "name": "ideas", "private": False, "member": False}],
                       "drive": [{"id": "abc", "name": "Meet recordings"}]}.items():
        monkeypatch.setattr(dashboard._choices[key], "get", lambda value=value: value)
    joined = []
    monkeypatch.setattr(slack_connector, "join_channel", joined.append)
    r = client.post("/dashboard/settings", data={"csrf": client.csrf, "company": "Acme Group", "prompt": "Tag for {company}."},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard/settings?msg=Saved")
    r = client.post("/dashboard/sources", data={
        "csrf": client.csrf, **{f"visibility_{t}": "private" for t in ["slack", "meeting", "web"]},
        "sections": ["slack", "email", "drive", "files"],
        "slack__channel__0__id": "C1", "slack__channel__0__mode": "all", "slack__channel__0__include_files": "on",
        "slack__channel__2__id": "C2", "slack__channel__2__mode": "reaction",
        "slack__channel__2__reaction": "star", "slack__channel__2__include_threads": "on",
        "slack__channel__3__id": "C1",  # a repeat is dropped
        "email__provider": "gmail", "email__only_people": "on", "email__gmail__inbox": "ideas@acme.com",
        "files__min_chars_per_page": "200",  # the fallback switch is off
        "drive__folder__0__id": "abc", "drive__folder__0__source_types": ["meeting", "interview", "nonsense"],
        "drive__folder__0__include_subfolders": "on", "drive__folder__3__id": " "}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and "joined+%23ideas" in r.headers["location"] and joined == ["C2"]
    get_config.cache_clear()
    config = get_config()
    assert config.company == "Acme Group" and config.prompts["tagging"] == "Tag for {company}."
    slack = config.sources["slack"]
    assert "allowed_channels" not in slack and "mode" not in slack
    assert [c["id"] for c in slack["channels"]] == ["C1", "C2"]
    first = slack["channels"][0]
    assert {k: first[k] for k in ("id", "name", "include_threads", "include_files", "include_links")} == \
        {"id": "C1", "name": "general", "include_threads": False, "include_files": True, "include_links": False}
    # Like a ClickUp channel: the retired "every message" mode is saved as sessions, from the day it's added.
    assert first["mode"] == "all" or first["mode"] == "sessions"
    assert first["from"] and first["added"] and first["views"] is False
    assert slack["channels"][1]["reaction"] == "star" and slack["channels"][1]["name"] == "ideas"
    email = config.sources["email"]
    assert email["only_people"] is True and email["gmail"]["inbox"] == "ideas@acme.com"
    assert not {"allow_people", "allowed_domains", "author_detection"} & set(email)
    assert config.sources["files"]["pdf_fallback"] == "off" and config.sources["files"]["min_chars_per_page"] == 200
    assert config.sources["links"]  # not on the page, so left as it was
    assert config.sources["drive"] == {"folders": [
        {"id": "abc", "name": "Meet recordings", "source_types": ["meeting", "interview"], "include_subfolders": True}]}
    page = client.get("/dashboard/sources").text
    assert 'value="abc"' in page and 'name="visibility_slack" value="private" checked' in page
    assert 'data-row="1">#ideas</button>' in page
    assert '<option value="interview" selected>' in page
    assert '<option value="C2" data-label="#ideas" selected>#ideas · Bot will join channel</option>' in page

    # Saving one channel comes back to it, with the message in its section.
    r = client.post("/dashboard/sources", data={"csrf": client.csrf, "sections": ["slack"], "slack__channel__0__id": "C2",
                                                "focus": "slack|channels|C2"}, follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard/sources?focus=slack%7Cchannels%7CC2&msg=Saved")
    assert r.headers["location"].endswith("#section-slack")
    page = client.get(r.headers["location"].split("#")[0]).text
    section = page[page.index('id="section-slack"'):page.index('id="section-drive"')]
    assert "saved as they happen" in section and page.count("saved as they happen") == 1

    bad = client.post("/dashboard/sources", data={"csrf": client.csrf, "sections": "files", "files__max_pdf_pages": "lots"},
                      follow_redirects=False)
    assert "err=" in bad.headers["location"]

    # Each page saves only what it shows: the other page's values are left as they were.
    get_config.cache_clear()
    assert get_config().company == "Acme Group" and get_config().prompts["tagging"] == "Tag for {company}."
    client.post("/dashboard/settings", data={"csrf": client.csrf, "company": "Acme Ltd", "prompt": "Tag for {company}."})
    get_config.cache_clear()
    config = get_config()
    assert config.company == "Acme Ltd" and [c["id"] for c in config.sources["slack"]["channels"]] == ["C2"]
    assert config.sources["default_visibility"]["slack"] == "private"


def test_web_form_works_with_a_dashboard_sign_in(client):
    r = client.get("/taxonomy", headers={"X-TLE-Client": "web-form"})
    assert r.status_code == 200
    # The session alone isn't enough without the header, so other sites can't use it.
    assert client.get("/taxonomy").status_code == 401


def test_problems_are_recorded(client):
    r = client.post("/ingest", json={"text": "   "}, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 400
    problems = get_store().list_events(EventQuery(level="error"))
    assert problems and problems[0].via == "api"


def test_delete_needs_confirming(client):
    store = get_store()
    item = _item(store)
    client.post(f"/dashboard/items/{item.row_id}/delete", data={"csrf": client.csrf})
    assert store.get(item.row_id) is not None
    client.post(f"/dashboard/items/{item.row_id}/delete", data={"csrf": client.csrf, "confirm": "yes"})
    assert store.get(item.row_id) is None
    assert store.list_events(EventQuery(kinds=["item_deleted"]))


def test_a_problem_that_keeps_happening_is_one_event(env):
    from tle import events

    store = env[1]
    store.init()
    for attempt in range(3):
        events.problem(store, f"Couldn't process an email; it will be retried: timeout {attempt}", via="email",
                       key="gmail:abc")
    events.problem(store, "Couldn't read scan.pdf", via="email")
    events.problem(store, "Couldn't read scan.pdf", via="email")
    problems = store.list_events(EventQuery(level="error"))
    assert sorted(e.count for e in problems) == [2, 3]
    retried = next(e for e in problems if e.count == 3)
    assert retried.last_time and retried.message.endswith("timeout 0")
    assert store.count_unread() == 2


def test_items_columns_and_coverage_buckets(client):
    from tle.dashboard import _heat

    _item(get_store())
    page = client.get("/dashboard/items?cols=date&cols=saved").text
    assert "<th>Saved</th>" in page and "<th>Themes</th>" not in page
    assert "<th>Themes</th>" in client.get("/dashboard/items").text
    # Several values in one filter match any of them; both visibilities is no filter.
    _item(get_store(), source_type="interview", authors=["Mo Jones"])
    count = lambda q: client.get(f"/dashboard/items?{q}").text.count('class="item-title"')  # noqa: E731
    assert count("type=meeting&type=interview") == 2 and count("type=meeting") == 1
    assert count("source=test") == 2 and count("source=slack") == 0
    assert count("visibility=public&visibility=private") == 2 and count("status=new&status=used") == 2
    assert count("status=used") == 0
    assert "Type: Meeting or Interview" in client.get("/dashboard/items?type=meeting&type=interview").text
    assert [_heat(n) for n in (0, 1, 2, 3, 5, 6, 9, 10)] == ["h0", "h1", "h1", "h2", "h2", "h3", "h3", "h4"]
    coverage = client.get("/dashboard/coverage").text
    assert "Pairs with no material" in coverage and 'class="cell h' in coverage


def test_add_form_signed_in_and_out(client, env, monkeypatch):
    from tle.api import app

    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    signed_in = client.get("/ingest").text
    assert "Signed in, so no API token is needed" in signed_in and 'aria-label="Main"' in signed_in
    monkeypatch.delenv("TLE_DEV_USER")
    signed_out = TestClient(app).get("/ingest").text
    assert 'id="toggle-settings"' in signed_out and 'aria-label="Main"' not in signed_out


def test_slow_lists_dont_hold_up_settings(client, monkeypatch):
    import time as time_module

    from tle.dashboard import _Choices, _section_errors

    calls = []

    def slow():
        calls.append(1)
        time_module.sleep(0.3)
        return [{"id": "C1", "name": "general"}]

    choices = _Choices("Slack", slow, lambda: "")
    started = time_module.monotonic()
    assert choices.get() is None and choices.loading  # the page never waits for a list
    assert time_module.monotonic() - started < 0.1
    assert choices.get() is None and len(calls) == 1  # a reload doesn't fetch twice
    monkeypatch.setitem(__import__("tle.dashboard", fromlist=["x"])._choices, "slack", choices)
    assert _section_errors()["slack"] == ("loading", "Loading lists from Slack…")
    # The page marks the section, so its script can swap it in once the list arrives.
    assert '<div class="panel source-panel" id="section-slack" data-loading>' in client.get("/dashboard/sources").text
    choices.thread.join()
    assert choices.get() == [{"id": "C1", "name": "general"}] and len(calls) == 1  # kept, not fetched again
    choices.clear()
    assert choices.get() == [{"id": "C1", "name": "general"}]  # still shown while it fetches again
    choices.thread.join()
    assert len(calls) == 2

    # Being asked to slow down isn't an error: it's shown as loading, and tried again in a minute.
    def rate_limited():
        raise RuntimeError("ClickUp's rate limit was reached.")

    busy = _Choices("ClickUp", rate_limited, lambda: "")
    busy.get()
    busy.thread.join()
    assert busy.busy and busy.error == "" and busy.at < time_module.monotonic() - busy.TTL + busy.RETRY_AFTER_ERROR + 1
    for key in ("clickup_places", "clickup_channels"):
        monkeypatch.setitem(__import__("tle.dashboard", fromlist=["x"])._choices, key, busy)
    kind, message = _section_errors()["clickup"]
    assert kind == "loading" and "asked us to slow down" in message and "Reload" not in message

    def refused():
        raise RuntimeError("401 Unauthorized")

    broken = _Choices("ClickUp", refused, lambda: "")
    broken.get()
    broken.thread.join()
    assert broken.error == "ClickUp error: 401 Unauthorized" and not broken.busy


def test_a_new_installation_uses_the_provider_with_a_key(monkeypatch):
    from tle.defaults import defaults

    assert defaults()["llm"]["provider"] == "anthropic" and defaults()["transcription"] is None  # no keys yet
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert defaults()["llm"] == {"provider": "openai", "model": "gpt-5", "max_input_chars": 150_000}
    assert defaults()["transcription"] == {"provider": "openai", "model": "gpt-4o-transcribe"}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert defaults()["llm"]["provider"] == "anthropic"  # Claude first; audio still on OpenAI
    assert defaults()["transcription"]["provider"] == "openai"


def test_database_trouble_is_explained(client, monkeypatch):
    import sqlite3

    from tle import check
    from tle.store import get_store

    # A database that can't be reached gets a page saying so (and a short reply from the API).
    def refused(*a, **k):
        raise sqlite3.OperationalError('FATAL:  password authentication failed for user "tle"')

    monkeypatch.setattr(type(get_store()), "search", refused)
    r = client.get("/dashboard/items")
    assert r.status_code == 503 and "can't reach its database" in r.text and "password authentication failed" in r.text
    r = client.get("/items", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 503 and "can't be reached" in r.json()["detail"]
    assert {r.name: r for r in check.run()}["Database"].fix.startswith("Rerun the deploy")


def test_tables_are_made_on_first_use_and_retried(monkeypatch, tmp_path):
    from tle import check, settings
    from tle.store import get_store

    # The check only reads, so it says when the tables haven't been made...
    result = {r.name: r for r in check.run()}["Database"]
    assert result.status == check.FAIL and "haven't been made yet" in result.detail
    # ...and the first use makes them. If that fails, the next use tries again.
    calls = []

    def flaky(store):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database still starting")
        return original(store)

    original = settings.prepare
    monkeypatch.setattr(settings, "prepare", flaky)
    with pytest.raises(RuntimeError):
        get_store()
    store = get_store()
    assert len(calls) == 2 and store.latest_settings() is not None
    get_store()
    assert len(calls) == 2  # once it worked, not again


def test_drive_settings_show_the_service_account(client):
    page = client.get("/dashboard/sources").text
    assert '<code class="mono" id="service-account">tle-runner@test-project.iam.gserviceaccount.com</code>' in page


def test_opens_on_items_and_counts_only_problems(client):
    from tle import events
    from tle.store import get_store

    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/dashboard/items"
    store = get_store()
    _item(store)  # a routine save: in Activity, but not counted
    import tle.dashboard as dashboard

    dashboard._unread.clear()
    assert 'class="count problem"' not in client.get("/dashboard/items").text
    events.problem(store, "Couldn't read scan.pdf", via="email")
    dashboard._unread.clear()
    page = client.get("/dashboard/items").text
    assert '<span class="count problem" title="Problems">1</span>' in page and 'class="problems on"' in page
    assert "Couldn&#39;t read scan.pdf" in client.get("/dashboard/items?view=problems").text
    # Dismissing it clears the count and the view.
    client.post("/dashboard/problems/dismiss", data={"csrf": client.csrf})
    dashboard._unread.clear()
    assert 'class="problems off"' in client.get("/dashboard/items").text


def test_slack_id_is_found_from_email(client, monkeypatch):
    from tle.connectors import slack

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(slack, "member_id", lambda email: "U0FOUND123" if email == "priya@acme.com" else None)
    client.post("/dashboard/people", data={"csrf": client.csrf, "name": "Priya Shah", "emails": "priya@acme.com"})
    priya = next(p for p in get_store().list_people() if p.name == "Priya Shah")
    assert priya.slack_id == "U0FOUND123"
    # A Slack ID typed in is kept as it is.
    client.post("/dashboard/people", data={"csrf": client.csrf, "name": "Tom Reid", "emails": "priya@acme.com",
                                            "slack_id": "U0TYPED"})
    assert next(p for p in get_store().list_people() if p.name == "Tom Reid").slack_id == "U0TYPED"


def test_taxonomy_saves_everything_at_once(client):
    store = get_store()
    item = _item(store)  # tagged "Expertise is the moat" and "Predictions"
    page = client.get("/dashboard/taxonomy").text
    assert 'class="tax-view tax-name">Expertise is the moat<' in page and "Save taxonomy" in page
    themes = [t.name for t in get_config().themes]
    narratives = get_config().narratives
    data = {"csrf": client.csrf, "formats": "Blog article\nLinkedIn post\n\n"}
    for n, name in enumerate(themes):
        data[f"themes__{n}__old"] = name
        data[f"themes__{n}__name"] = {"Expertise is the moat": "Experience is the moat"}.get(name, name)
        data[f"themes__{n}__description"] = "d"
        data[f"themes__{n}__remove"] = "on" if name == "Buyers distrust AI-generated content" else ""
    data.update({"themes__9__old": "", "themes__9__name": "Pricing", "themes__9__description": "How we charge",
                 "themes__10__old": "", "themes__10__name": ""})  # a new one, and a blank new row
    for n, tag in enumerate(narratives):
        data.update({f"narratives__{n}__old": tag.name, f"narratives__{n}__name": tag.name,
                     f"narratives__{n}__description": tag.description,
                     f"narratives__{n}__remove": "on" if tag.name == "Predictions" else "",
                     f"narratives__{n}__from_items": "on" if tag.name == "Predictions" else ""})
    r = client.post("/dashboard/taxonomy", data=data, follow_redirects=False)
    assert "Updated+2+items" in r.headers["location"] or "Updated 2 items" in r.headers["location"]
    get_config.cache_clear()
    config = get_config()
    assert [t.name for t in config.themes] == ["Experience is the moat", "Measuring content beyond traffic", "Pricing"]
    assert "Predictions" not in [n.name for n in config.narratives] and config.formats == ["Blog article", "LinkedIn post"]
    saved = store.get(item.row_id)
    assert saved.theme_tags == ["Experience is the moat"] and "Predictions" not in saved.narrative_tags

    # A duplicate or a blanked name saves nothing.
    before = store.latest_settings().version
    bad = client.post("/dashboard/taxonomy", follow_redirects=False, data={
        "csrf": client.csrf, "themes__0__old": "Pricing", "themes__0__name": "Pricing", "themes__1__old": "",
        "themes__1__name": "pricing"})
    assert "err=" in bad.headers["location"] and store.latest_settings().version == before
    bad = client.post("/dashboard/taxonomy", follow_redirects=False, data={
        "csrf": client.csrf, "themes__0__old": "Pricing", "themes__0__name": " "})
    assert "err=" in bad.headers["location"] and store.latest_settings().version == before


def test_lists_can_be_refreshed(client, monkeypatch):
    from tle import dashboard

    choices = dashboard._choices["drive"]
    monkeypatch.setattr(choices, "value", [{"id": "old", "name": "Old folder"}])
    monkeypatch.setattr(choices, "at", 5.0)
    r = client.get("/dashboard/sources/refresh/drive", follow_redirects=False)
    assert r.headers["location"] == "/dashboard/sources#section-drive" and choices.value is None and choices.at is None
    assert client.get("/dashboard/sources/refresh/nonsense").status_code == 404


def test_older_items_get_their_way_in(tmp_path):
    """Items saved before `via` existed take it from their "saved" event."""
    import sqlite3

    from tle.store.sqlite import SQLiteStore

    path = str(tmp_path / "old.db")
    store = SQLiteStore(path)
    store.init()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE items DROP COLUMN via")
    conn.execute("ALTER TABLE items DROP COLUMN reading")
    conn.execute("INSERT INTO items (row_id, date_created, date_ingested, source_type, extracted_text) "
                 "VALUES ('old1', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00', 'linkedin', 'Text')")
    conn.commit()
    conn.close()
    store.add_event(Event(kind="item_saved", via="clickup", row_id="old1", message="Saved"))
    store.init()
    item = store.get("old1")
    assert item.via == "clickup" and item.reading is False


def test_item_page_shows_the_text_and_what_it_responds_to(client):
    store = get_store()
    item = _item(store, via="slack", source_metadata={"links": [{
        "url": "https://linkedin.com/posts/pat", "title": "Pricing is dead", "author": "Pat Outsider",
        "site": "linkedin.com", "excerpt": "Nobody pays for hours."}]})
    page = client.get(f"/dashboard/items/{item.row_id}").text
    assert "Pricing should follow value, not hours." in page and "Responding to" in page
    assert "Pat Outsider" in page and "Meeting from Slack" in page
    _item(store, source_type="linkedin", authors=["Pat Outsider"], reading=True)
    assert client.get("/dashboard/items").text.count('class="item-title"') == 1
    reading = client.get("/dashboard/items?view=reading").text
    assert reading.count('class="item-title"') == 1 and "Reading" in reading
    assert "Pat Outsider" not in client.get("/dashboard/people").text

    # The pencil by Text edits it in place; saving sends only the text, so nothing else changes.
    assert 'class="text-edit"' in page and "Edit the text</summary>" not in page
    before = store.get(item.row_id)
    client.post(f"/dashboard/items/{item.row_id}", data={"csrf": client.csrf, "extracted_text": "Value, not hours. Edited."})
    after = store.get(item.row_id)
    assert after.extracted_text == "Value, not hours. Edited."
    assert (after.theme_tags, after.status, after.authors, after.reading) == \
        (before.theme_tags, before.status, before.authors, before.reading)


def test_clickup_channel_rows():
    """A new channel starts from today; changing its mode records when; keys the form doesn't show stay."""
    from datetime import date

    from tle import settings_form

    def post(mode, start=""):
        return {"sections": "clickup", "clickup__channel__0__id": "ch1", "clickup__channel__0__mode": mode,
                "clickup__channel__0__from": start, "clickup__channel__0__gap_hours": "4"}

    saved = settings_form.read(post("sessions"), {})
    row = saved["clickup"]["channels"][0]
    assert row["from"] == date.today().isoformat() and "since" not in row and row["gap_hours"] == 4

    again = settings_form.read(post("sessions", "2026-10-01"), saved)
    assert again["clickup"]["channels"][0]["from"] == "2026-10-01" and "since" not in again["clickup"]["channels"][0]

    switched = settings_form.read(post("reaction", "2026-10-01"), again)["clickup"]["channels"][0]
    assert switched["since"] and switched["mode"] == "reaction"
    kept = settings_form.read(post("reaction", "2026-10-01"), {"clickup": {"channels": [switched]}})
    assert kept["clickup"]["channels"][0]["since"] == switched["since"]

    import pytest
    with pytest.raises(ValueError, match="should be a date"):
        settings_form.read(post("sessions", "next week"), {})


def test_session_page(client, env):
    """A chat session's page lists its topics, their messages and the chains they're on."""
    from datetime import datetime, timezone

    from tle.ingest import ingest_text
    from tle.models import Link, Topic

    _, store = env

    def session(row_id, day, text, messages):
        return ingest_text(text, config=env[0], store=store, row_id=row_id, source_type="clickup", via="clickup",
                           authors=["Jane Smith"], date_created=datetime(2026, 11, day, 9, tzinfo=timezone.utc),
                           source_metadata={"label": "ClickUp #marketing", "title": f"Session {day}", "session": True,
                                            "place": "clickup:ch1", "messages": messages})

    one = session("s1", 3, "#1 Jane Smith (03.11.2026 09:00): The H1 should lead with resonance.",
                  [{"id": "m1", "n": 1, "who": "Jane Smith", "when": "", "reply_to": None}])
    two = session("s2", 6, "#1 Jane Smith (06.11.2026 09:00): Back to the H1.\n    ↳ Mo Jones (06.11.2026 09:05): Keyword first.",
                  [{"id": "m2", "n": 1, "who": "Jane Smith", "when": "", "reply_to": None},
                   {"id": "r2", "n": 1, "who": "Mo Jones", "when": "", "reply_to": "m2"}])
    a = Topic(row_id="s1", place="clickup:ch1", title="Homepage H1", messages=["m1"], date_created=one.date_created)
    b = Topic(row_id="s2", place="clickup:ch1", title="H1 again", summary="Keyword or resonance.", messages=["m2"],
              people=["Jane Smith", "Mo Jones"], date_created=two.date_created)
    store.save_topics("s1", [a])
    store.save_topics("s2", [b])
    store.add_link(Link(from_id=f"topic:{b.id}", to_id=f"topic:{a.id}"))

    from tle.models import View
    store.save_views(b.id, [View(topic_id=b.id, row_id="s2", person="Mo Jones", view="Lead with the keyword.",
                                 stance="disagrees", responds_to="Jane Smith", messages=["m2"])])
    page = client.get("/dashboard/items/s2").text
    assert "<strong>Mo Jones</strong>" in page and "disagrees Jane Smith" in page and "Lead with the keyword." in page
    assert "<h2>Topics</h2>" in page and "H1 again" in page and "Keyword or resonance." in page
    assert 'Continues <a href="/dashboard/items/s1">Homepage H1</a>' in page
    assert "↳ Mo Jones" in page  # the reply shows under its message
    assert 'Continued in <a href="/dashboard/items/s2">H1 again</a>' in client.get("/dashboard/items/s1").text
    assert "Session 6" in client.get("/dashboard/items").text  # listed by its title

    # Corrections: rename the topic, remove the link and add it back, each credited to the person.
    def post(**data):
        return client.post("/dashboard/items/s2/topics", data={"csrf": client.csrf, **data}, follow_redirects=False)

    assert post(action="rename", topic=b.id, title="Homepage H1, again", summary="").status_code == 303
    [link] = store.links_for([f"topic:{b.id}"])
    assert "Edit topic" in client.get("/dashboard/items/s2").text and ">Remove</button>" in client.get("/dashboard/items/s2").text
    post(action="unlink", link=link.id)
    assert not store.links_for([f"topic:{b.id}"])
    post(action="link", topic=b.id, to=a.id)
    [link] = store.links_for([f"topic:{b.id}"])
    assert link.made_by != "model" and store.get_topics([b.id])[0].title == "Homepage H1, again"
    assert "err=" in post(action="link", topic=b.id, to="nope").headers["location"]
    assert "err=" in post(action="move", message="zz", to=b.id).headers["location"]
    assert store.get("s2").source_metadata["title"] == "Homepage H1, again"
    # Messages to move are listed with their first words, and a session's title can be changed.
    assert ">#1 Jane Smith: Back to the H1.</option>" in client.get("/dashboard/items/s2").text
    client.post("/dashboard/items/s2", data={"csrf": client.csrf, "title": "H1 debate", "label": "ClickUp #marketing"})
    assert store.get("s2").source_metadata["title"] == "H1 debate"


def test_lists_load_on_a_machine_that_just_started(monkeypatch):
    """A new Cloud Run instance has only been up for seconds, which time.monotonic() counts from."""
    from tle import dashboard

    monkeypatch.setattr(dashboard.time, "monotonic", lambda: 5.0)
    choices = dashboard._Choices("Test", lambda: [{"id": "c1", "name": "general"}], lambda: "")
    choices.get()
    choices.thread.join()
    assert choices.get() == [{"id": "c1", "name": "general"}]
    count = dashboard._Count(lambda store: 3)
    assert count.get(None) == 3


def test_read_now(client, env, monkeypatch):
    """Settings shows when each scheduled source was last read, and "Read now" starts it."""
    from tle import syncs

    _, store = env
    started = []
    monkeypatch.setattr(syncs, "_run_here", lambda source: started.append(source))
    syncs.run("clickup", lambda: "2 chat sessions saved or updated", store)
    page = client.get("/dashboard/sources").text
    assert "Last read just now: 2 chat sessions saved or updated" in page and "Not read yet" in page

    r = client.post("/dashboard/sources/read/drive", data={"csrf": client.csrf}, follow_redirects=False)
    assert "msg=Reading+Drive+now" in r.headers["location"]
    import time as time_module
    time_module.sleep(0.1)
    assert started == ["drive"] and "Reading now…" in client.get("/dashboard/sources").text
    # Asked again while it's running: refused.
    r = client.post("/dashboard/sources/read/drive", data={"csrf": client.csrf}, follow_redirects=False)
    assert "being+read+already" in r.headers["location"]
    assert "err=" in client.post("/dashboard/sources/read/nope", data={"csrf": client.csrf},
                                 follow_redirects=False).headers["location"]


def test_read_now_from_the_page_script(client, env, monkeypatch):
    """The page's script starts a run without leaving the page and follows it by its status."""
    from tle import syncs

    _, store = env
    monkeypatch.setattr(syncs, "_run_here", lambda source: None)
    json = {"Accept": "application/json"}
    started = client.post("/dashboard/sources/read/clickup", data={"csrf": client.csrf}, headers=json).json()
    assert started["running"] and started["line"] == "Reading now…" and started["requested"]

    # Asked again while it runs: refused, with the run's status so the page can follow it.
    again = client.post("/dashboard/sources/read/clickup", data={"csrf": client.csrf}, headers=json)
    assert again.status_code == 409 and again.json()["running"]

    syncs.begin(store, "clickup")
    syncs.end(store, "clickup", summary="4 chat sessions saved or updated")
    status = client.get("/dashboard/sources/read/clickup/status").json()
    assert not status["running"] and status["line"] == "Last read just now: 4 chat sessions saved or updated"
    assert status["finished"] > started["requested"]
    assert 'data-sync="clickup"' in client.get("/dashboard/sources").text


def test_items_sort_by_date_added(client):
    """Something said long ago but added today tops the list when it's sorted by date added."""
    from datetime import datetime, timezone

    store = get_store()
    _item(store, date_created=datetime(2026, 11, 1, tzinfo=timezone.utc), source_metadata={"label": "Recent call"})
    _item(store, date_created=datetime(2025, 9, 1, tzinfo=timezone.utc), source_metadata={"label": "Old call"})
    said = client.get("/dashboard/items").text
    assert said.index("Recent call") < said.index("Old call") and 'aria-current="true">date said' in said
    added = client.get("/dashboard/items?sort=added").text
    assert added.index("Old call") < added.index("Recent call") and 'aria-current="true">date added' in added
    assert "<th" in added and ">Saved<" in added  # the order the list is in, shown beside the date said
    assert 'name="sort" value="added"' in added  # filtering keeps the order
    # Filters keep the sort and the sort keeps the filters; neither shows as a filter.
    filtered = client.get("/dashboard/items?sort=added&type=meeting").text
    assert 'href="/dashboard/items?type=meeting">date said' in filtered and "Sort:" not in filtered


def test_items_per_page(client):
    store = get_store()
    for n in range(25):
        _item(store, source_metadata={"label": f"Call {n}"})
    count = lambda html: html.count('class="item-title"')
    first = client.get("/dashboard/items").text
    assert count(first) == 20 and '<option value="20" selected>' in first and "page=2" in first
    assert count(client.get("/dashboard/items?per=10").text) == 10
    assert count(client.get("/dashboard/items?per=50").text) == 25
    assert count(client.get("/dashboard/items?per=7").text) == 20  # only 10, 20 or 50
    # Paging and filtering keep the size; picking a size keeps the filters and the order.
    ten = client.get("/dashboard/items?per=10&type=meeting&sort=added").text
    assert "per=10" in ten[ten.index("Next") - 200:ten.index("Next")]
    assert 'name="per" value="10"' in ten
    picker = ten[ten.index('class="per-page"'):ten.index('id="per"')]
    assert 'name="type" value="meeting"' in picker and 'name="sort" value="added"' in picker and 'name="per"' not in picker


def test_no_company_name_shows_none(client):
    """A company that hasn't named itself yet isn't shown as Eleven Content."""
    client.post("/dashboard/settings", data={"csrf": client.csrf, "company": ""})
    page = client.get("/dashboard/items").text
    assert "Experiment</span>" in page and "•</span>" not in page
    assert "Eleven Content · Expertise Database" not in page and "<span>Expertise Database</span>" in page
    client.post("/dashboard/settings", data={"csrf": client.csrf, "company": "Acme Ltd"})
    assert '•</span> Acme Ltd' in client.get("/dashboard/items").text
