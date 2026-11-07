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

    store = get_store()
    assert prepare(store) is True and prepare(store) is False  # only the first time
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
    for path in ["/dashboard/activity", "/dashboard/activity?level=error", "/dashboard/review", "/dashboard/items",
                 "/dashboard/items?theme=Expertise+is+the+moat", f"/dashboard/items/{item.row_id}",
                 "/dashboard/people", f"/dashboard/people/{person.id}", "/dashboard/taxonomy",
                 "/dashboard/coverage", "/dashboard/settings"]:
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Saved meeting" in client.get("/dashboard/activity").text
    assert "Pricing" in client.get("/dashboard/review").text  # the theme idea


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
    page = client.get("/dashboard/settings").text
    assert "Tagging instructions" in page and "History" not in page
    assert "No Slack token found." in page and "Drive error: Request had insufficient" in page
    assert page.count('<fieldset class="section-body" disabled>') == 3  # Slack, Drive, ClickUp
    assert 'name="sections" value="slack"' not in page and 'name="sections" value="email"' in page
    client.post("/dashboard/settings", data={"csrf": client.csrf, "sections": ["slack"], "slack__channel__0__id": "CX"})
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
    r = client.post("/dashboard/settings", data={
        "csrf": client.csrf, "company": "Acme Group", "prompt": "Tag for {company}.",
        **{f"visibility_{t}": "private" for t in ["slack", "meeting", "web"]},
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
    assert slack["channels"][0] == {"id": "C1", "name": "general", "mode": "all", "include_threads": False,
                                    "include_files": True, "include_links": False}
    assert slack["channels"][1]["reaction"] == "star" and slack["channels"][1]["name"] == "ideas"
    email = config.sources["email"]
    assert email["only_people"] is True and email["gmail"]["inbox"] == "ideas@acme.com"
    assert not {"allow_people", "allowed_domains", "author_detection"} & set(email)
    assert config.sources["files"]["pdf_fallback"] == "off" and config.sources["files"]["min_chars_per_page"] == 200
    assert config.sources["links"]  # not on the page, so left as it was
    assert config.sources["drive"] == {"folders": [
        {"id": "abc", "name": "Meet recordings", "source_types": ["meeting", "interview"], "include_subfolders": True}]}
    page = client.get("/dashboard/settings").text
    assert 'value="abc"' in page and 'name="visibility_slack" value="private" checked' in page
    assert 'data-row="1">#ideas</button>' in page
    assert '<option value="interview" selected>' in page
    assert '<option value="C2" data-label="#ideas" selected>#ideas · Bot will join channel</option>' in page

    # Saving one channel comes back to it, with the message in its section.
    r = client.post("/dashboard/settings", data={"csrf": client.csrf, "sections": ["slack"], "slack__channel__0__id": "C2",
                                                 "focus": "slack|channels|C2"}, follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard/settings?focus=slack%7Cchannels%7CC2&msg=Saved")
    assert r.headers["location"].endswith("#section-slack")
    page = client.get(r.headers["location"].split("#")[0]).text
    section = page[page.index('id="section-slack"'):page.index('id="section-drive"')]
    assert "Saved. Running services" in section and page.count("Saved. Running services") == 1

    bad = client.post("/dashboard/settings", data={"csrf": client.csrf, "sections": "files", "files__max_pdf_pages": "lots"},
                      follow_redirects=False)
    assert "err=" in bad.headers["location"]


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
    assert count("source=meeting&source=interview") == 2 and count("source=meeting") == 1
    assert count("visibility=public&visibility=private") == 2 and count("status=new&status=used") == 2
    assert count("status=used") == 0
    assert "Source: meeting or interview" in client.get("/dashboard/items?source=meeting&source=interview").text
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
    monkeypatch.setattr(choices, "FIRST_WAIT", 0.05)
    started = time_module.monotonic()
    assert choices.get() is None and choices.loading  # didn't wait the whole 0.3s
    assert time_module.monotonic() - started < 0.2
    assert choices.get() is None and len(calls) == 1  # a reload doesn't fetch twice
    monkeypatch.setitem(__import__("tle.dashboard", fromlist=["x"])._choices, "slack", choices)
    assert _section_errors()["slack"][0] == "loading"
    # The page marks the section, so its script can swap it in once the list arrives.
    assert '<div class="panel source-panel" id="section-slack" data-loading>' in client.get("/dashboard/settings").text
    choices.thread.join()
    assert choices.get() == [{"id": "C1", "name": "general"}] and len(calls) == 1  # kept, not fetched again
    choices.clear()
    assert choices.get() == [{"id": "C1", "name": "general"}]  # still shown while it fetches again
    choices.thread.join()
    assert len(calls) == 2

    def failing():
        raise RuntimeError("ClickUp's rate limit was reached. Reload Settings in a minute.")

    broken = _Choices("ClickUp", failing, lambda: "")
    broken.get()
    broken.thread.join()
    assert broken.error == "ClickUp error: ClickUp's rate limit was reached. Reload Settings in a minute."


def test_branding_logo_and_colour(client):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 50

    def save(files=None, **data):
        return client.post("/dashboard/settings", data={"csrf": client.csrf, **data}, files=files or {},
                           follow_redirects=False)

    assert "msg=" in save({"logo": ("logo.svg", svg, "image/svg+xml")}, accent="#ffcc00").headers["location"]
    logo = client.get("/dashboard/logo-light.svg")
    assert logo.content == svg and logo.headers["content-type"].startswith("image/svg+xml")
    assert "default-src 'none'" in logo.headers["content-security-policy"]
    page = client.get("/dashboard/settings").text
    assert "--accent: #FFCC00" in page and "--on-accent: #111827" in page  # dark text on a light colour
    assert 'value="#FFCC00"' in page and "Go back to the default logo" in page
    assert '<span class="logo-chip"><img class="logo"' in page and 'class="theme-toggle"' in page

    save({"logo": ("logo.png", png, "image/png")}, accent="#FFCC00")
    assert client.get("/dashboard/logo-light.svg").headers["content-type"] == "image/png"

    # Unsafe or oversized files, and bad colours, are refused and nothing changes.
    for files, data in [({"logo": ("x.svg", b'<svg onload="alert(1)"></svg>', "image/svg+xml")}, {}),
                        ({"logo": ("x.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml")}, {}),
                        ({"logo": ("big.png", png + b"0" * 200_000, "image/png")}, {}),
                        ({"logo": ("x.txt", b"hello", "text/plain")}, {}),
                        ({}, {"accent": "red"})]:
        assert "err=" in save(files, **data).headers["location"]
    assert client.get("/dashboard/logo-light.svg").content == png

    save(logo_reset="on", accent="#1f5dff")  # the default colour picked again
    assert client.get("/dashboard/logo-light.svg").content != png
    page = client.get("/dashboard/settings").text
    assert "--accent:" not in page and 'value="#1F5DFF"' in page


def test_a_new_installation_uses_the_provider_with_a_key(monkeypatch):
    from tle.defaults import defaults

    assert defaults()["llm"]["provider"] == "anthropic" and defaults()["transcription"] is None  # no keys yet
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert defaults()["llm"] == {"provider": "openai", "model": "gpt-5", "max_input_chars": 150_000}
    assert defaults()["transcription"] == {"provider": "openai", "model": "gpt-4o-transcribe"}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert defaults()["llm"]["provider"] == "anthropic"  # Claude first; audio still on OpenAI
    assert defaults()["transcription"]["provider"] == "openai"
