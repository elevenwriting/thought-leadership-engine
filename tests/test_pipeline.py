# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""End-to-end tests on SQLite, with the LLM call replaced so no keys are needed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tle import analytics, playbooks
from tle.models import Person
from tle.store import SearchQuery

ROOT = Path(__file__).resolve().parents[1]
THEME = "Expertise is the moat"


class FakeLLM:
    """Stands in for the provider call, so the real tagger (prompt, schema, filtering) runs."""

    def __init__(self, config):
        pass

    def generate_json(self, system, prompt, schema, images=None):
        assert THEME in system and "theme_tags" in schema["properties"]
        return {
            "summary": "A summary.",
            "theme_tags": [THEME, "Not a real theme"],
            "narrative_tags": ["Counter-narrative"],
            "suggested_themes": [],
        }


@pytest.fixture
def setup(company, monkeypatch):
    monkeypatch.setenv("TLE_API_TOKEN", "secret")
    monkeypatch.setattr("tle.tagger.LLM", FakeLLM)
    return company


def test_ingest_applies_governance(setup):
    from tle.ingest import ingest_text

    config, store = setup
    item = ingest_text(
        "AI content isn't cheaper once you count the edits.", config=config, store=store,
        source_type="meeting", authors=["jane@acme.com"],
        date_created=datetime(2026, 9, 19), source_metadata={"label": "All-hands"},
    )
    assert item.authors == ["Jane Smith"]  # email resolved to the configured person
    assert item.visibility == "private"  # meetings default to private
    assert item.theme_tags == [THEME]  # unknown tags dropped
    assert item.citation == "JANE SMITH - MEETING ALL-HANDS - 19.09.2026"
    result = store.get(item.row_id).to_result()
    assert "private_flag" in result

    public = ingest_text("A post.", config=config, store=store, source_type="linkedin", authors=["Mo Jones"])
    assert "private_flag" not in public.to_result()


def test_tagging_failure_still_saves(setup, monkeypatch):
    from tle.ingest import ingest_text

    config, store = setup
    monkeypatch.setattr("tle.ingest.tag", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no key")))
    item = ingest_text("Something worth keeping.", config=config, store=store, source_type="braindump", authors=["Jane Smith"])
    assert store.get(item.row_id).source_metadata["tagging_error"] == "no key"


def test_search_coverage_trends(setup):
    from tle.ingest import ingest_text

    config, store = setup
    old = datetime.now(timezone.utc) - timedelta(days=45)
    ingest_text("Old opinion about expertise.", config=config, store=store, source_type="interview",
                authors=["Mo Jones"], date_created=old)
    new = ingest_text("New opinion about expertise.", config=config, store=store, source_type="slack",
                      authors=["U01234567"])
    store.update(new.row_id, {"deliverables": ["LinkedIn post: https://example.com/p/1"]})

    assert len(store.search(SearchQuery(themes=[THEME]))) == 2
    assert [i.row_id for i in store.search(SearchQuery(text="NEW OPINION"))] == [new.row_id]
    assert len(store.search(SearchQuery(visibility="public"))) == 1
    assert len(store.search(SearchQuery(authors=["Jane Smith"]))) == 1  # Slack ID resolved
    assert len(store.search(SearchQuery(date_from=datetime.now(timezone.utc) - timedelta(days=7)))) == 1

    cov = analytics.coverage(store, config)
    cell = next(c for c in cov["cells"] if c["theme"] == THEME and c["narrative"] == "Counter-narrative")
    assert cell["items"] == 2 and cell["formats_made"] == {"LinkedIn post": 1}
    assert "LinkedIn post" not in cell["formats_missing"]

    tr = analytics.trends(store, days=30)
    theme = next(t for t in tr["themes"] if t["name"] == THEME)
    assert (theme["current"], theme["previous"]) == (1, 1)


def test_api(setup):
    from fastapi.testclient import TestClient

    from tle.api import app

    client = TestClient(app)
    assert client.get("/items").status_code == 401
    auth = {"Authorization": "Bearer secret"}

    r = client.post("/ingest", headers=auth, json={"text": "Braindump on expertise.", "source_type": "braindump", "authors": ["Jane Smith"]})
    assert r.status_code == 200, r.text
    row_id = r.json()["row_id"]

    r = client.post("/ingest/file", headers=auth, files={"file": ("notes.vtt", b"WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHello there\n")},
                    data={"authors": "Mo Jones", "source_type": "meeting"})
    assert r.status_code == 200, r.text
    assert r.json()["source_metadata"]["label"] == "notes"

    items = client.get("/items", headers=auth, params={"themes": THEME}).json()
    assert items["count"] == 2 and all(i.get("private_flag") for i in items["items"])

    r = client.post(f"/items/{row_id}/deliverables", headers=auth, json={"format": "Blog article", "title_or_url": "Draft"})
    assert r.json()["status"] == "used"
    assert client.get("/coverage", headers=auth).status_code == 200
    assert "query" in client.get("/taxonomy", headers=auth).json()["playbooks"]
    assert client.get("/playbooks/briefs", headers=auth).status_code == 200
    assert client.get("/ingest").status_code == 200

    assert client.get("/items?token=secret").status_code == 401  # header only


def test_slack_ignores_channels_not_allowed(setup):
    from tle.connectors.slack import handle_event

    config, store = setup
    handle_event({"event": {"type": "reaction_added", "reaction": "round_pushpin", "item": {"channel": "CNOTALLOWED", "ts": "1.2"}}},
                 config, store)
    assert store.search(SearchQuery()) == []


def test_playbooks_and_mcp():
    assert len(playbooks.names()) == 14
    assert "Cite everything" in playbooks.get("debate")
    import asyncio

    from tle.mcp_server import mcp

    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"search_items", "get_coverage", "get_playbook", "add_item"} <= tools
    assert len(asyncio.run(mcp.list_prompts())) == 14


class FakeSlack:
    """Enough of slack_sdk.WebClient for the bot: one channel with a thread in it."""

    def __init__(self, thread):
        self.thread = thread

    def conversations_history(self, channel, latest, oldest, inclusive, limit):
        return {"messages": [m for m in self.thread if m["ts"] == latest and "parent" in m]}

    def conversations_replies(self, channel, ts, limit, cursor=None):
        return {"messages": self.thread, "response_metadata": {}}

    def users_info(self, user):
        return {"user": {"real_name": "Outside Person"}}

    def conversations_info(self, channel):
        return {"channel": {"name": "news"}}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.example/{message_ts}"}


def _thread():
    return [
        {"ts": "1726740000.000100", "user": "U01234567", "text": "AI content isn't cheaper.", "thread_ts": "1726740000.000100",
         "reply_count": 2, "parent": True},
        {"ts": "1726740060.000200", "user": "U07654321", "text": "Agree with <@U01234567>, the edits kill it.",
         "thread_ts": "1726740000.000100"},
        {"ts": "1726740120.000300", "user": "U09999999", "text": "Counterpoint: briefs fix that.",
         "thread_ts": "1726740000.000100"},
    ]


def _reaction(ts):
    return {"event": {"type": "reaction_added", "reaction": "round_pushpin", "item": {"channel": "C01234567", "ts": ts}}}


def test_slack_reaction_saves_whole_thread(setup, monkeypatch):
    from tle.connectors import slack

    config, store = setup
    thread = _thread()
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack(thread))

    slack.handle_event(_reaction("1726740060.000200"), config, store)  # react to a reply
    [item] = store.search(SearchQuery())
    assert item.row_id == "slack-C01234567-1726740000000100"
    assert item.authors == ["Jane Smith", "Mo Jones", "Outside Person"]
    assert "Mo Jones (19.09.2024 10:01): Agree with @Jane Smith" in item.extracted_text
    assert item.source_metadata["message_count"] == 3
    assert item.citation == "JANE SMITH, MO JONES, OUTSIDE PERSON - SLACK #NEWS - 19.09.2024"

    store.update(item.row_id, {"status": "reviewed"})
    thread.append({"ts": "1726740180.000400", "user": "U07654321", "text": "Fair.", "thread_ts": "1726740000.000100"})
    slack.handle_event(_reaction("1726740000.000100"), config, store)  # react again to the parent
    [item] = store.search(SearchQuery())
    assert item.source_metadata["message_count"] == 4 and item.status == "reviewed"


def test_slack_single_message_without_threads(setup, monkeypatch):
    from tle.connectors import slack

    config, store = setup
    config.sources["slack"]["channels"][0]["include_threads"] = False
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack(_thread()))
    slack.handle_event(_reaction("1726740060.000200"), config, store)
    [item] = store.search(SearchQuery())
    assert item.authors == ["Mo Jones"] and item.extracted_text == "Agree with @Jane Smith, the edits kill it."



def test_slack_rules_per_channel(setup, monkeypatch):
    from tle.connectors import slack

    config, store = setup
    config.sources["slack"] = {"channels": [
        {"id": "C01234567", "reaction": "star", "include_threads": False},
        {"id": "C0OTHER", "mode": "all"},
    ]}
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack(_thread()))
    slack.handle_event(_reaction("1726740060.000200"), config, store)  # 📍 isn't this channel's reaction
    assert store.search(SearchQuery()) == []
    starred = _reaction("1726740060.000200")
    starred["event"]["reaction"] = "star"
    slack.handle_event(starred, config, store)
    [item] = store.search(SearchQuery())
    assert item.extracted_text == "Agree with @Jane Smith, the edits kill it."  # threads off here
    assert slack.channel_settings(config, "C0OTHER")["mode"] == "all"
    assert slack.channel_settings(config, "C0NOPE") is None

def _raw_email(sender, subject, body, attachments=(), html=False, logo=False):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "ideas@acme.com", subject
    msg["Date"], msg["Message-ID"] = "Fri, 19 Sep 2026 10:00:00 +0000", "<abc@acme.com>"
    if html:
        msg.set_content("<p>Hello <b>there</b></p><p>Second line</p>", subtype="html")
    else:
        msg.set_content(body)
    if logo:
        msg.add_related(b"\x89PNG fake", maintype="image", subtype="png", cid="<logo>")
    for name, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def test_email_rules(setup):
    from tle.connectors.email import parse_message, process_raw

    config, store = setup
    vtt = b"WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nPricing per outcome.\n"
    raw = _raw_email("Jane <Jane@acme.com>", "meeting: Q3 all-hands", "See attached.",
                     [("q3.vtt", vtt, "text", "vtt"), ("invite.ics", b"BEGIN:VCALENDAR", "text", "calendar")])
    assert process_raw(raw, config, store) == (True, 1)  # the vtt; invite and cover note skipped
    items = store.search(SearchQuery(source_types=["meeting"]))
    assert items[0].authors == ["Jane Smith"] and items[0].date_created.year == 2026
    assert {i.source_metadata["label"] for i in items} == {"Q3 all-hands"}

    # Anyone may send, unless only registered people are allowed.
    assert process_raw(_raw_email("eve@other.com", "hi", "An opinion worth keeping."), config, store) == (True, 1)
    config.sources["email"]["only_people"] = True
    assert process_raw(_raw_email("eve@other.com", "hi", "Another opinion."), config, store) == (False, 0)

    parsed = parse_message(_raw_email("jane@acme.com", "x", "", html=True, logo=True))
    assert parsed.body == "Hello there\nSecond line" and parsed.attachments == []


class FakeGmail:
    def __init__(self, raws):
        import base64

        self.raw = {str(i): base64.urlsafe_b64encode(r).decode() for i, r in enumerate(raws)}
        self.labelled = {}

    def users(self): return self
    def labels(self): return self
    def messages(self): return self
    def execute(self): return self._result

    def list(self, userId, q=None, pageToken=None):
        if q is None:
            self._result = {"labels": [{"name": "tle-saved", "id": "L1"}]}
        else:
            self._result = {"messages": [{"id": i} for i in self.raw if i not in self.labelled]}
        return self

    def create(self, userId, body):
        self._result = {"id": "L2"}
        return self

    def get(self, userId, id, format):
        self._result = {"raw": self.raw[id]}
        return self

    def modify(self, userId, id, body):
        self.labelled[id] = body["addLabelIds"]
        self._result = {}
        return self


def test_gmail_poll_labels_mail(setup, monkeypatch):
    from tle.connectors import email_gmail

    config, store = setup
    config.sources["email"]["only_people"] = True
    fake = FakeGmail([_raw_email("jane@acme.com", "braindump: pricing", "Price on outcomes, not hours."),
                      _raw_email("eve@other.com", "spam", "Buy now")])
    monkeypatch.setattr(email_gmail, "_credentials", lambda *a: None)
    monkeypatch.setattr(email_gmail, "build", lambda *a, **k: fake)
    assert email_gmail.poll(config, store) == {"saved_emails": 1, "items": 1, "rejected": 1}
    assert fake.labelled == {"0": ["L1"], "1": ["L2"]}
    assert email_gmail.poll(config, store)["saved_emails"] == 0  # nothing reprocessed


class FakeIMAP:
    capabilities = ("IMAP4REV1", "MOVE")

    def __init__(self, raws):
        self.inbox = {str(i + 1).encode(): r for i, r in enumerate(raws)}
        self.moved = {}

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def login(self, user, password): self.user = user
    def create(self, name): return ("NO", [b"exists"])
    def select(self, name): return ("OK", [b"2"])

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(self.inbox)]
        if command == "FETCH":
            return "OK", [(b"1 (BODY[] {n}", self.inbox[args[0]]), b")"]
        if command == "MOVE":
            self.moved[args[0]] = args[1]
            del self.inbox[args[0]]
            return "OK", []


def test_attribution_fields():
    from tle.attribution import parse_fields, split_names

    fields, rest = parse_fields("Author: Jane Smith and Mo Jones\nDate: 12.09.2026\nType: Interview\n\nThe content.\nNote: not a field")
    assert fields == {"authors": "Jane Smith and Mo Jones", "date": "12.09.2026", "source_type": "Interview"}
    assert rest == "The content.\nNote: not a field"
    assert split_names(fields["authors"]) == ["Jane Smith", "Mo Jones"]
    assert parse_fields("Hi all,\nAuthor: someone") == ({}, "Hi all,\nAuthor: someone")


class AttributionLLM:
    answer: dict = {}

    def __init__(self, config):
        pass

    def generate_json(self, system, prompt, schema, images=None):
        assert "Submitted by: christian@acme.com" in prompt
        return AttributionLLM.answer


def test_email_authors(setup, monkeypatch):
    from tle.connectors.email import process_raw

    config, store = setup
    config.people.append(Person(name="Christian Rigg", emails=["christian@acme.com"]))
    monkeypatch.setattr("tle.attribution.LLM", AttributionLLM)

    # Fields in the body win, and are stripped from the saved text.
    raw = _raw_email("christian@acme.com", "Podcast notes",
                     "Author: Mo Jones\nDate: 2026-06-03\nType: interview\nVisibility: public\n\nRoadmaps are promises.")
    assert process_raw(raw, config, store) == (True, 1)
    [item] = store.search(SearchQuery(source_types=["interview"]))
    assert item.authors == ["Mo Jones"] and item.extracted_text == "Roadmaps are promises."
    assert item.visibility == "public" and item.date_created.date().isoformat() == "2026-06-03"
    assert item.source_metadata["author_source"] == "provided"
    assert item.source_metadata["submitted_by"] == "christian@acme.com"

    # No fields: the LLM decides, including the date.
    AttributionLLM.answer = {"authors": ["Jane Smith"], "date": "2026-05-01", "reason": "Forwarded from Jane."}
    process_raw(_raw_email("christian@acme.com", "Fwd: pricing", "---------- Forwarded message ---------\nFrom: Jane"), config, store)
    [item] = store.search(SearchQuery(text="Forwarded message"))
    assert item.authors == ["Jane Smith"] and item.source_metadata["author_source"] == "llm"
    assert item.date_created.date().isoformat() == "2026-05-01" and "author_needs_review" not in item.source_metadata

    # Someone outside the company: kept, but flagged.
    AttributionLLM.answer = {"authors": ["Pat Client"], "date": "", "reason": "Signed by Pat."}
    process_raw(_raw_email("christian@acme.com", "client view", "Thanks, Pat"), config, store)
    [item] = store.search(SearchQuery(text="Thanks, Pat"))
    assert item.authors == ["Pat Client"] and item.source_metadata["author_needs_review"]
    assert item.date_created.year == 2026  # the email's own date

    # Can't tell: the sender, flagged.
    AttributionLLM.answer = {"authors": [], "date": "", "reason": "No clues."}
    process_raw(_raw_email("christian@acme.com", "misc", "Some thoughts."), config, store)
    [item] = store.search(SearchQuery(text="Some thoughts"))
    assert item.authors == ["Christian Rigg"] and item.source_metadata["author_source"] == "submitter"
    assert item.source_metadata["author_needs_review"]


def test_drive_authors(setup, monkeypatch):
    from tle.connectors import drive_sync

    config, store = setup
    files = {
        "f1": b"Transcript\nMo Jones: Roadmaps are promises we can't keep.",
        "f2": b"Author: Jane Smith\nType: marketing\n\nOur pricing manifesto.",
    }
    monkeypatch.setattr(drive_sync, "_download", lambda service, f: (files[f["id"]], f["name"] + ".txt", "text/plain"))
    monkeypatch.setattr("tle.attribution.LLM", AttributionLLM)
    AttributionLLM.answer = {"authors": ["Mo Jones"], "date": "", "reason": "Mo gives the views."}
    # The email stand-in checks for an email submitter; here the submitter is the file owner.
    monkeypatch.setattr(AttributionLLM, "generate_json",
                        lambda self, system, prompt, schema, images=None: AttributionLLM.answer)
    folder = {"id": "folder", "source_type": "meeting"}

    def drive_file(id_):
        return {"id": id_, "name": f"doc-{id_}", "createdTime": "2026-09-01T09:00:00Z",
                "owners": [{"emailAddress": "jane@acme.com"}], "webViewLink": f"https://drive/{id_}"}

    drive_sync._save(None, drive_file("f1"), folder, config, store)
    mo = store.find_by_drive_file_id("f1")
    assert mo.authors == ["Mo Jones"] and mo.source_type == "meeting"
    assert mo.source_metadata["author_source"] == "llm" and mo.source_metadata["submitted_by"] == "jane@acme.com"

    drive_sync._save(None, drive_file("f2"), folder, config, store)
    doc = store.find_by_drive_file_id("f2")
    assert doc.authors == ["Jane Smith"] and doc.source_type == "marketing" and doc.visibility == "public"
    assert doc.extracted_text == "Our pricing manifesto." and doc.source_metadata["author_source"] == "provided"

    # Can't tell from the content: the owner, flagged for review.
    files["f3"] = b"Untitled notes."
    AttributionLLM.answer = {"authors": [], "date": "", "reason": "No clues."}
    drive_sync._save(None, drive_file("f3"), folder, config, store)
    owner = store.find_by_drive_file_id("f3")
    assert owner.authors == ["Jane Smith"] and owner.source_metadata["author_source"] == "submitter"
    assert "author_unclear" in owner.flags

    # A folder holding several kinds of content: each file's type is picked from them.
    mixed = {"id": "mixed", "source_types": ["meeting", "interview"]}
    files["f4"] = b"Q: What's broken in pricing?\nMo Jones: Hourly billing."
    AttributionLLM.answer = {"authors": ["Mo Jones"], "date": "", "reason": "Interviewee.", "source_type": "interview"}
    drive_sync._save(None, drive_file("f4"), mixed, config, store)
    assert store.find_by_drive_file_id("f4").source_type == "interview"
    files["f5"] = b"Notes."
    AttributionLLM.answer = {"authors": ["Mo Jones"], "date": "", "reason": "Mo.", "source_type": "linkedin"}
    drive_sync._save(None, drive_file("f5"), mixed, config, store)
    assert store.find_by_drive_file_id("f5").source_type == "meeting"  # not one of the folder's: the first
    assert drive_sync.folder_types({"source_type": "marketing"}) == ["marketing"]


def test_preview_then_save_without_retagging(setup, monkeypatch):
    from fastapi.testclient import TestClient

    from tle import tagger
    from tle.api import app

    calls = []
    real_tag = tagger.tag
    monkeypatch.setattr("tle.ingest.tag", lambda *a, **k: calls.append(k.get("note")) or real_tag(*a, **k))
    client, auth = TestClient(app), {"Authorization": "Bearer secret"}

    draft = client.post("/ingest/preview", headers=auth, json={
        "text": "Expertise is all we have.", "authors": ["Jane Smith"], "note": "Jane's core belief"}).json()
    assert draft["theme_tags"] == [THEME] and calls == ["Jane's core belief"]
    assert client.get("/items", headers=auth).json()["count"] == 0  # preview saves nothing

    saved = client.post("/ingest", headers=auth, json={
        "text": draft["extracted_text"], "authors": ["Mo Jones"], "label": "Offsite",
        "source_type": "meeting", "visibility": "public", "note": "Jane's core belief",
        "summary": "Edited summary.", "theme_tags": ["Measuring content beyond traffic", "Made up"],
        "narrative_tags": [], "source_metadata": {},
    }).json()
    assert len(calls) == 1  # no second tagging call
    assert saved["summary"] == "Edited summary." and saved["theme_tags"] == ["Measuring content beyond traffic"]
    assert saved["citation"].startswith("MO JONES - MEETING OFFSITE - ") and saved["visibility"] == "public"
    assert saved["source_metadata"]["note"] == "Jane's core belief"


def test_find_urls_and_blocked_addresses():
    from tle.links import check_url, find_urls

    body = ("Worth a read: https://acme.com/blog/pricing. Also https://bit.ly/x1, "
            "https://acme.com/blog/pricing again. Unsubscribe: https://list-manage.com/unsubscribe?u=1 "
            "Join: https://meet.google.com/abc-defg-hij")
    assert find_urls(body) == ["https://acme.com/blog/pricing", "https://bit.ly/x1"]
    for bad in ["http://169.254.169.254/", "http://localhost/", "http://10.1.2.3/", "ftp://acme.com/"]:
        with pytest.raises(ValueError):
            check_url(bad)


def test_email_link_with_note(setup, monkeypatch):
    """What the sender says about a link is the item; a colleague's own page is also saved, as theirs."""
    from tle.connectors import email as email_connector
    from tle.links import Page

    config, store = setup
    page = lambda url, config: Page(  # noqa: E731
        url=url, final_url=url, text="Pricing on outcomes\n\nThe article body.", title="Pricing on outcomes",
        author="Mo Jones", site="acme.com", date=datetime(2026, 8, 1))
    monkeypatch.setattr(email_connector, "fetch_link", page)
    monkeypatch.setattr("tle.connectors.shared.fetch_link", page)
    raw = _raw_email("jane@acme.com", "", "Mo's piece, the bit on retainers matters: https://acme.com/blog/pricing")
    assert email_connector.process_raw(raw, config, store) == (True, 1)
    items = {i.via + ":" + i.source_type: i for i in store.search(SearchQuery())}
    comment, post = items["email:email"], items["email:web"]
    assert comment.extracted_text.startswith("Mo's piece") and comment.source_metadata["links"][0]["saved_as"] == post.row_id
    assert post.authors == ["Mo Jones"] and post.date_created.date().isoformat() == "2026-08-01"

    # A link sent with no comment is a deliberate save, like the web form's.
    raw = _raw_email("jane@acme.com", "", "https://acme.com/blog/other")
    assert email_connector.process_raw(raw, config, store) == (True, 1)
    assert len(store.search(SearchQuery())) == 3 and not store.search(SearchQuery(reading=True))


def test_slack_files(setup, monkeypatch):
    from tle.connectors import slack

    config, store = setup
    message = {"ts": "1726740000.000100", "user": "U01234567", "text": "Mo's deck from the offsite",
               "files": [{"id": "F1", "name": "offsite.txt", "mimetype": "text/plain",
                          "url_private_download": "https://files.slack.com/F1", "permalink": "https://slack/F1"}],
               "parent": True}
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack([message]))
    monkeypatch.setattr(slack, "_download_file", lambda f: b"Author: Mo Jones\n\nOur offsite plan.")
    slack.handle_event(_reaction("1726740000.000100"), config, store)
    slack.handle_event(_reaction("1726740000.000100"), config, store)  # again: nothing duplicated
    items = {i.row_id: i for i in store.search(SearchQuery())}
    assert set(items) == {"slack-file-F1", "slack-C01234567-1726740000000100"}
    file_item = items["slack-file-F1"]
    assert file_item.authors == ["Mo Jones"] and file_item.extracted_text == "Our offsite plan."
    assert file_item.source_metadata["note"] == "Mo's deck from the offsite"
    assert file_item.source_metadata["submitted_by"] == "Jane Smith"


def test_tagging_prompt_can_be_replaced(setup):
    from tle.tagger import DEFAULT_INSTRUCTIONS, _system

    config, _ = setup

    assert DEFAULT_INSTRUCTIONS.splitlines()[0].replace("{company}", config.company or "a company") in _system(config)
    config.prompts = {"tagging": "Tag for {company}. Be strict."}
    system = _system(config)
    assert system.startswith(f"Tag for {config.company or 'a company'}. Be strict.")
    assert "Themes (what it's about):" in system and DEFAULT_INSTRUCTIONS[:40] not in system


def _scanned_pdf(pages: int = 3) -> bytes:
    import io

    from PIL import Image as PILImage

    images = [PILImage.new("RGB", (600, 800), "white") for _ in range(pages)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def test_scanned_pdf_is_read_by_the_model(setup, monkeypatch):
    from tle.extract import extract_text

    config, _ = setup
    calls = []

    class PageReader:
        def __init__(self, model):
            self.model = model

        def generate_json(self, system, prompt, schema, images=None, max_tokens=4096):
            calls.append((self.model.model, len(images)))
            return {"text": f"Read: {prompt}"}

    monkeypatch.setattr("tle.extract.LLM", PageReader)
    config.sources["files"] = {"pages_per_call": 2}
    text = extract_text(_scanned_pdf(3), "scan.pdf", config)
    assert calls == [(config.llm.model, 2), (config.llm.model, 1)]
    assert "pages 1 to 2" in text and "pages 3 to 3" in text

    config.sources["files"] = {"pdf_fallback": "off"}
    with pytest.raises(ValueError, match="no text layer"):
        extract_text(_scanned_pdf(1), "scan.pdf", config)


def test_structured_output_schemas_are_closed():
    from tle.llm import _closed

    schema = _closed({"type": "object", "properties": {"a": {"type": "array", "items": {
        "type": "object", "properties": {"b": {"type": "string"}}}}}})
    assert schema["additionalProperties"] is False
    assert schema["properties"]["a"]["items"]["additionalProperties"] is False


def test_unknown_source_type_becomes_other(setup):
    from tle.ingest import prepare_item

    config, _ = setup
    item = prepare_item("Some thoughts.", config=config, source_type="Podcast", authors=["Jane Smith"], tags={})
    assert item.source_type == "other"
    assert item.source_metadata["source_type_given"] == "Podcast"
    assert prepare_item("x", config=config, source_type="MEETING", authors=["Jane"], tags={}).source_type == "meeting"


def test_slack_links(setup, monkeypatch):
    """Shared links: what our people say is the item; other people's pages are context or reading."""
    from tle.connectors import slack
    from tle.ingest import _tagging_note
    from tle.links import Page

    config, store = setup

    def fetch(url, config):
        if "broken" in url:
            raise ValueError("404")
        author = "Mo Jones" if "mo-jones" in url else "Pat Outsider"
        return Page(url=url, final_url=url, text="Why retainers beat projects.\n\nThe post body.",
                    title="Retainers", author=author, site="linkedin.com", date=datetime(2026, 8, 1))

    monkeypatch.setattr("tle.connectors.shared.fetch_link", fetch)
    ours = "https://www.linkedin.com/posts/mo-jones_retainers"
    theirs = "https://www.linkedin.com/posts/pat-outsider_pricing"

    # A colleague's own post, shared with no comment: saved as theirs.
    lone = {"ts": "1726740000.000100", "user": "U01234567", "text": f"Worth a read <{ours}|linkedin.com>", "parent": True}
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack([lone]))
    slack.handle_event(_reaction(lone["ts"]), config, store)
    slack.handle_event(_reaction(lone["ts"]), config, store)  # again: nothing duplicated
    [item] = store.search(SearchQuery())
    assert item.row_id.startswith("slack-link-") and item.source_type == "linkedin" and item.via == "slack"
    assert item.authors == ["Mo Jones"] and not item.reading and item.source_metadata["submitted_by"] == "Jane Smith"
    assert item.source_metadata["url"] == ours and item.date_created.year == 2026

    # Someone else's post with no comment: reading, kept out of searches and coverage.
    bare = {"ts": "1726745000.000100", "user": "U01234567", "text": f"FYI <{theirs}>", "parent": True}
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack([bare]))
    slack.handle_event(_reaction(bare["ts"]), config, store)
    [reading] = store.search(SearchQuery(reading=True))
    assert reading.authors == ["Pat Outsider"] and reading.flags == [] and "READING" in reading.to_result()["reading_flag"]
    assert len(store.search(SearchQuery())) == 1 and all("Pat Outsider" not in r.authors for r in store.tag_rows())

    # A thread responding to someone else's post: the discussion is the item, the post its context.
    thread = [{"ts": "1726750000.000100", "user": "U01234567", "text": f"<{theirs}>", "parent": True,
               "thread_ts": "1726750000.000100", "reply_count": 1},
              {"ts": "1726750060.000200", "user": "U07654321", "text": "Wrong: retainers beat projects for us.",
               "thread_ts": "1726750000.000100"}]
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack(thread))
    slack.handle_event(_reaction("1726750000.000100"), config, store)
    talk = store.get("slack-C01234567-1726750000000100")
    assert talk.authors == ["Jane Smith", "Mo Jones"] and not talk.reading
    [link] = talk.source_metadata["links"]
    assert link["author"] == "Pat Outsider" and link["url"] == theirs and "saved_as" not in link
    assert "responding to" in _tagging_note(talk) and "Pat Outsider" in _tagging_note(talk)
    assert len(store.search(SearchQuery(reading=None))) == 3  # no new item for the post itself

    # A link that can't be read is a problem; a comment beside it is still saved.
    broken = {"ts": "1726760000.000100", "user": "U01234567", "text": "Look at this terrible take <https://acme.com/broken>",
              "parent": True}
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack([broken]))
    slack.handle_event(_reaction(broken["ts"]), config, store)
    assert store.get("slack-C01234567-1726760000000100")
    assert store.count_unread(level="error") == 1


class FakeClickUp:
    """Enough of the ClickUp API for the connector: one list with a tagged task, a Doc, a channel."""

    def __init__(self, now_ms):
        self.now = now_ms
        self.comments = [{"id": "c1", "comment_text": "Agree, see https://acme.com/retainers", "date": str(now_ms - 5000),
                          "user": {"id": 2, "username": "Mo Jones"}}]
        self.doc_updated = now_ms - 1000

    def get(self, path, **params):
        if path == "/v2/team":
            return {"teams": [{"id": 9, "members": [{"user": {"id": 1, "username": "Jane Smith"}},
                                                    {"user": {"id": 2, "username": "Mo Jones"}}]}]}
        if path == "/v2/team/9/task":
            assert params["tags[]"] == "ingest" and params["list_ids[]"] == "77"
            return {"tasks": [{"id": "t1", "name": "Pricing post"}], "last_page": True}
        if path == "/v2/task/t1":
            return {"id": "t1", "name": "Pricing post", "markdown_description": "Retainers beat hourly billing.",
                    "creator": {"id": 1, "username": "Jane Smith"}, "date_created": str(self.now - 9000),
                    "url": "https://app.clickup.com/t/t1", "list": {"name": "Ideas"},
                    "attachments": [{"id": "a1", "title": "notes.txt", "url": "https://attachments.example/a1",
                                     "date": str(self.now - 8000)}]}
        if path == "/v2/task/t1/comment":
            return {"comments": list(reversed(self.comments))}
        if path == "/v2/space/5/folder":
            return {"folders": [{"id": "66", "lists": [{"id": "77"}]}]}
        if path == "/v2/space/5/list":
            return {"lists": []}
        if path == "/v3/workspaces/9/docs/d1/pages":
            return [{"name": "Playbook", "content": "How we price retainers.", "pages": [
                {"name": "Examples", "content": "Three examples."}]}]
        raise AssertionError(f"unexpected GET {path}")

    def pages(self, path, key="data", **params):
        if path == "/v3/workspaces/9/docs":
            yield {"id": "d1", "name": "Pricing playbook", "creator": 2, "date_created": self.now - 20000,
                   "date_updated": self.doc_updated, "parent": {"id": "66", "type": 5}}
            yield {"id": "d2", "name": "Elsewhere", "creator": 2, "date_created": self.now, "parent": {"id": "99", "type": 5}}
        elif path == "/v3/workspaces/9/chat/channels/ch1/messages":
            yield {"id": "m1", "content": "Hourly billing is dead.", "user_id": "1", "date": self.now - 3000, "replies_count": 1}
            yield {"id": "m2", "content": "Lunch?", "user_id": "2", "date": self.now - 4000, "replies_count": 0}
            yield {"id": "m0", "content": "Old idea.", "user_id": "2", "date": self.now - 30 * 86400_000, "replies_count": 0}
        elif path == "/v3/workspaces/9/chat/messages/m1/replies":
            yield {"id": "r1", "content": "Agreed.", "user_id": "2", "date": self.now - 2000}
        elif path.endswith("/reactions"):
            if "/m1/" in path:
                yield {"reaction": "📍", "user_id": "2", "date": self.now}
        else:
            raise AssertionError(f"unexpected pages {path}")


def test_clickup_sync(setup, monkeypatch):
    import time as time_module

    from tle.connectors import clickup
    from tle.links import Page

    config, store = setup
    config.sources["clickup"] = {
        "places": [{"id": "list:77", "include_links": True}, {"id": "space:5", "tasks": False, "docs": True}],
        "channels": [{"id": "ch1", "name": "ideas"}],
    }
    fake = FakeClickUp(int(time_module.time() * 1000))
    monkeypatch.setattr("tle.connectors.shared.fetch_link", lambda url, config: Page(
        url=url, final_url=url, text="Retainers win.", title="Retainers", site="acme.com"))
    monkeypatch.setattr(clickup.httpx, "get", lambda url, **kw: type("R", (), {
        "content": b"Meeting notes on pricing.", "headers": {"content-type": "text/plain"},
        "raise_for_status": lambda self: None})())

    assert clickup.sync(config, store, client=fake) == {"tasks": 1, "docs": 1, "chat": 1, "sessions": 0}
    items = {i.row_id: i for i in store.search(SearchQuery(limit=50))}
    task = items["clickup-task-t1"]
    assert task.source_type == "clickup" and "Mo Jones (" in task.extracted_text and task.source_metadata["comment_count"] == 1
    assert items["clickup-file-a1"].extracted_text == "Meeting notes on pricing."
    # The comment links to someone else's page: it's context on the task, not an item of its own.
    assert not any(k.startswith("clickup-link-") for k in items)
    assert task.source_metadata["links"][0]["url"] == "https://acme.com/retainers" and task.via == "clickup"
    doc = items["clickup-doc-d1"]
    assert doc.extracted_text == "How we price retainers.\n\n## Examples\n\nThree examples."
    chat = items["clickup-chat-m1"]  # 📍 on it; m2 had no reaction, m0 is older than the look-back
    assert chat.authors == ["Jane Smith", "Mo Jones"] and "Agreed." in chat.extracted_text
    assert not {"clickup-chat-m2", "clickup-chat-m0", "clickup-doc-d2"} & set(items)

    # Run again: nothing new. Add a comment: the task is rebuilt.
    count = len(items)
    clickup.sync(config, store, client=fake)
    assert len(store.search(SearchQuery(limit=50))) == count
    fake.comments.append({"id": "c2", "comment_text": "Shipping it Friday.", "date": str(fake.now - 1000),
                          "user": {"id": 1, "username": "Jane Smith"}})
    clickup.sync(config, store, client=fake)
    assert "Shipping it Friday." in store.get("clickup-task-t1").extracted_text


class FakeChat:
    """A ClickUp channel in session mode: messages newest first, with thread replies."""

    def __init__(self, now_ms):
        hour = 3600_000
        self.messages = [  # oldest first here; served newest first
            {"id": "a1", "content": "Should the playbook be a deck or an ebook?", "user_id": "1", "date": now_ms - 30 * hour},
            {"id": "a2", "content": "Ebook. Decks don't get read.", "user_id": "2", "date": now_ms - 29 * hour},
            {"id": "a3", "content": "Fair.", "user_id": "1", "date": now_ms - 28 * hour},
            {"id": "b1", "content": "H1 should lead with the keyword.", "user_id": "2", "date": now_ms - 10 * hour},
            {"id": "b2", "content": "Not at the cost of resonance.", "user_id": "1", "date": now_ms - 9 * hour},
            {"id": "c1", "content": "Webinar recorded.", "user_id": "2", "date": now_ms - 1 * hour},
        ]
        # A reply that comes 9 hours after its message doesn't hold the session open.
        self.replies = {"a2": [{"id": "a2r", "content": "Agreed, ebook.", "user_id": "99", "date": now_ms - 20 * hour}]}
        for m in self.messages:
            m["replies_count"] = len(self.replies.get(m["id"], []))

    def get(self, path, **params):
        if path == "/v2/team":
            return {"teams": [{"id": 9, "members": [{"user": {"id": 1, "username": "Jane Smith"}},
                                                    {"user": {"id": 2, "username": "Mo Jones"}}]}]}
        raise AssertionError(f"unexpected GET {path}")

    def pages(self, path, key="data", **params):
        if path == "/v3/workspaces/9/chat/channels/ch1/messages":
            yield from sorted(self.messages, key=lambda m: -m["date"])
        elif path.startswith("/v3/workspaces/9/chat/messages/") and path.endswith("/replies"):
            yield from self.replies.get(path.split("/")[-2], [])
        else:
            raise AssertionError(f"unexpected pages {path}")


def test_clickup_sessions(setup, monkeypatch):
    import time as time_module
    from datetime import date, timedelta

    from tle.connectors import clickup

    config, store = setup
    start = (date.today() - timedelta(days=3)).isoformat()
    config.sources["clickup"] = {"channels": [{"id": "ch1", "name": "marketing", "mode": "sessions", "from": start,
                                               "include_links": False}]}
    fake = FakeChat(int(time_module.time() * 1000))
    sessions = lambda: sorted(i.row_id for i in store.search(SearchQuery(limit=50)))

    # History is read a batch at a time, oldest first; the session still going isn't saved.
    monkeypatch.setattr(clickup, "SESSIONS_PER_SYNC", 1)
    assert clickup.sync(config, store, client=fake)["sessions"] == 1
    assert sessions() == ["clickup-session-ch1-a1"]
    assert clickup.sync(config, store, client=fake)["sessions"] == 1
    assert clickup.sync(config, store, client=fake)["sessions"] == 0
    assert sessions() == ["clickup-session-ch1-a1", "clickup-session-ch1-b1"]

    first = store.get("clickup-session-ch1-a1")
    lines = first.extracted_text.splitlines()
    assert lines[0] == f"#1 Jane Smith ({first.date_created:%d.%m.%Y %H:%M}): Should the playbook be a deck or an ebook?"
    assert lines[1].startswith("#2 Mo Jones (") and lines[1].endswith("Decks don't get read.")
    assert lines[2].startswith("    ↳ Unknown (") and lines[2].endswith("Agreed, ebook.")
    assert lines[3].startswith("#3 Jane Smith (") and len(lines) == 4
    assert first.authors == ["Jane Smith", "Mo Jones"]  # the unknown sender is left out
    meta = first.source_metadata
    assert meta["session"] and meta["message_count"] == 4 and meta["title"].startswith("#marketing, ")
    assert [m["id"] for m in meta["messages"]] == ["a1", "a2", "a2r", "a3"] and meta["messages"][2]["reply_to"] == "a2"
    assert "messages" not in first.to_result()["source_metadata"]

    # A late reply in a saved session rebuilds it; nothing else is saved twice.
    fake.replies["b1"] = [{"id": "b1r", "content": "Can we test both?", "user_id": "1", "date": fake.messages[-1]["date"]}]
    fake.messages[3]["replies_count"] = 1
    assert clickup.sync(config, store, client=fake)["sessions"] == 1
    assert "Can we test both?" in store.get("clickup-session-ch1-b1").extracted_text
    assert clickup.sync(config, store, client=fake)["sessions"] == 0

    # Once the last burst has been quiet for the gap, it's saved too.
    fake.messages[-1]["date"] -= 4 * 3600_000
    assert clickup.sync(config, store, client=fake)["sessions"] == 1
    assert sessions() == ["clickup-session-ch1-a1", "clickup-session-ch1-b1", "clickup-session-ch1-c1"]


def test_clickup_session_start(setup):
    """With no Starting from date, a channel starts from its first sync; a mode change starts from
    when it was made."""
    import time as time_module

    from tle.connectors import clickup

    config, store = setup
    config.sources["clickup"] = {"channels": [{"id": "ch1", "name": "marketing", "mode": "sessions",
                                               "include_links": False}]}
    fake = FakeChat(int(time_module.time() * 1000))
    assert clickup.sync(config, store, client=fake)["sessions"] == 0
    assert store.get_state("clickup-sessions:ch1")["start"]

    config.sources["clickup"]["channels"][0].update(
        {"from": "2020-01-01", "since": datetime.now(timezone.utc).isoformat()})
    store.set_state("clickup-sessions:ch1", {})
    assert clickup.sync(config, store, client=fake)["sessions"] == 0


def test_check_says_what_to_fix(setup, monkeypatch):
    from tle import check

    setup[0].sources["email"]["gmail"]["inbox"] = ""
    monkeypatch.setattr("tle.connectors.drive_sync.list_folders", lambda: (_ for _ in ()).throw(RuntimeError("no credentials")))
    results = {r.name: r for r in check.run()}
    assert results["Config"].status == check.OK and results["Database"].status == check.OK
    assert results["Model for tagging and authors"].status == check.FAIL
    assert "ANTHROPIC_API_KEY" in results["Model for tagging and authors"].fix
    assert results["Slack"].status == check.SKIP and results["ClickUp"].status == check.SKIP
    assert results["Google Drive"].status == check.SKIP and results["Email inbox"].status == check.SKIP

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxp-user-token")
    assert {r.name: r for r in check.run()}["Slack"].fix.startswith("Use the Bot User OAuth Token")

    text = check.report(check.run())
    assert "✗ Model for tagging and authors" in text and text.rstrip().endswith("to fix.")


def test_mcp_over_http(setup):
    from fastapi.testclient import TestClient

    from tle.api import app

    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    with TestClient(app) as c:
        assert c.post("/mcp", json=init, headers=headers).status_code == 401
        assert c.post("/mcp", json=init, headers={**headers, "authorization": "Bearer wrong"}).status_code == 401
        signed = {**headers, "authorization": "Bearer secret"}
        r = c.post("/mcp", json=init, headers=signed, follow_redirects=False)
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "thought-leadership"
        r = c.post("/mcp", headers=signed, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                 "params": {"name": "get_taxonomy", "arguments": {}}})
        assert "Acme Ltd" in r.json()["result"]["content"][0]["text"]
        assert "/mcp" not in c.get("/openapi.json").text


# Made-up keys, put together here so the publish script's scan for real keys doesn't stop on them.
FAKE_CLICKUP = "pk" + "_123456_ABCDEFGHIJKL"
FAKE_ANTHROPIC = "sk-" + "ant-api03-abcdefghijklmnop"
FAKE_SLACK = "xo" + "xb-1234567890-abcdefghijkl"


def test_secrets_are_trimmed_and_never_shown(setup, monkeypatch):
    from tle import check, events, secrets
    from tle.store import EventQuery

    config, store = setup
    monkeypatch.setenv("CLICKUP_API_TOKEN", FAKE_CLICKUP + "\n")
    secrets.trim()
    import os

    assert os.environ["CLICKUP_API_TOKEN"] == FAKE_CLICKUP
    # An error that quotes a key, in the check, a Settings message or Activity, shows [hidden].
    leaked = f"Illegal header value b'{FAKE_CLICKUP}' and {FAKE_ANTHROPIC}"
    assert secrets.redact(leaked) == "Illegal header value b'[hidden]' and [hidden]"
    assert secrets.redact("postgresql://tle:s3cretpass@/db") == "postgresql://[hidden]@/db"
    monkeypatch.setattr(check, "CHECKS", [("ClickUp", lambda: (_ for _ in ()).throw(ValueError(leaked)))])
    assert "pk_123456" not in check.report(check.run())
    events.problem(store, f"Couldn't read ClickUp: {leaked}", via="clickup")
    assert "pk_123456" not in store.list_events(EventQuery())[0].message


def test_log_lines_never_show_secrets(caplog):
    import logging

    from tle import secrets  # noqa: F401  (installs the redaction)

    log = logging.getLogger("tle.test")
    with caplog.at_level(logging.WARNING):
        log.warning("Couldn't list ClickUp: %s", "Illegal header value b'" + "pk" + "_999_ABCDEFGHIJKLMN'")
        try:
            raise ValueError(f"token {FAKE_SLACK} refused")
        except ValueError:
            log.exception("Slack failed")
    text = caplog.text
    assert "pk_999" not in text and "xoxb-1234567890" not in text and text.count("[hidden]") >= 2


def test_slack_speakers_are_matched_by_email(setup, monkeypatch):
    from tle.connectors import slack

    config, store = setup
    # A message from someone whose Slack ID isn't on People, but whose Slack email is.
    message = {"ts": "1726740000.000100", "user": "U0NEWPERSON", "text": "Retainers beat hourly billing, every time.",
               "parent": True}
    fake = FakeSlack([message])
    monkeypatch.setattr(fake, "users_info", lambda user: {"user": {"real_name": "jane s",
                                                                   "profile": {"email": "Jane@acme.com"}}}, raising=False)
    monkeypatch.setattr(slack, "_client", lambda: fake)
    slack.handle_event(_reaction("1726740000.000100"), config, store)
    [item] = store.search(SearchQuery())
    assert item.authors == ["Jane Smith"]


class FakeTopics:
    """The quick model finding topics: one topic per session, the last one linked to the H1 topic.
    It leaves the first session's "Fair." out, and fails once when told to."""

    fail = False

    def __init__(self, config):
        pass

    def generate_json(self, system, prompt, schema, images=None, max_tokens=4096):
        import re

        if FakeTopics.fail:
            FakeTopics.fail = False
            raise RuntimeError("overloaded")
        session = prompt.split("SESSION", 1)[1]
        if "deck or an ebook" in session:
            return {"topics": [{"title": "Playbook format", "summary": "Ebook over deck.", "messages": [1, 2],
                                "continues": "made-up-id", "why": "x"}]}
        if "H1" in session:
            return {"topics": [{"title": "Homepage H1", "summary": "Keyword or resonance.", "messages": [1, 2],
                                "continues": None, "why": "new"}]}
        h1 = re.search(r"- (\w+) \([^)]*\): Homepage H1", prompt).group(1)
        return {"topics": [{"title": "Homepage H1 follow-up", "summary": "Webinar talk.", "messages": [1],
                            "continues": h1, "why": "Same page."}]}


def test_session_topics(setup, monkeypatch):
    import time as time_module
    from datetime import date

    from fastapi.testclient import TestClient

    from tle import topics
    from tle.api import app
    from tle.connectors import clickup

    config, store = setup
    monkeypatch.setattr("tle.topics.LLM", FakeTopics)
    start = (date.today() - timedelta(days=3)).isoformat()
    config.sources["clickup"] = {"channels": [{"id": "ch1", "name": "marketing", "mode": "sessions", "from": start,
                                               "include_links": False}]}
    fake = FakeChat(int(time_module.time() * 1000))
    fake.messages[-1]["date"] -= 3.5 * 3600_000  # 4.5 hours ago: every session has ended

    # The first session's topic call fails: the session is kept, and the call is tried again next sync.
    FakeTopics.fail = True
    assert clickup.sync(config, store, client=fake)["sessions"] == 3
    first = store.get("clickup-session-ch1-a1")
    assert first.source_metadata["topics_error"] == "overloaded" and not store.topics_for([first.row_id])
    clickup.sync(config, store, client=fake)
    first = store.get("clickup-session-ch1-a1")
    assert "topics_error" not in first.source_metadata
    found = store.topics_for([first.row_id])
    # A message the model left out goes under Unassigned; a link to a topic it wasn't offered is dropped.
    assert [t.title for t in found] == ["Playbook format", "Unassigned"] and found[1].messages == ["a3"]
    assert not store.links_for([f"topic:{t.id}" for t in found])
    assert first.source_metadata["title"] == "Playbook format"  # Unassigned doesn't count as a topic
    assert found[0].people == ["Jane Smith", "Mo Jones"]  # a reply's unknown sender isn't one of them

    # The last session continues the H1 discussion: the two form a chain.
    h1 = store.topics_for(["clickup-session-ch1-b1"])[0]
    later = store.topics_for(["clickup-session-ch1-c1"])[0]
    assert [t.id for t in topics.chain(later.id, store)] == [h1.id, later.id]
    assert store.get("clickup-session-ch1-c1").source_metadata["title"] == "Homepage H1 follow-up"

    client, auth = TestClient(app), {"Authorization": "Bearer secret"}
    data = client.get("/items/clickup-session-ch1-c1", headers=auth).json()
    assert data["topics"][0]["messages"] == [1]
    assert data["topics"][0]["chain"] == [{"topic_id": h1.id, "row_id": "clickup-session-ch1-b1",
                                           "date": h1.date_created.date().isoformat(), "title": "Homepage H1",
                                           "summary": "Keyword or resonance.", "when": "before"}]
    chain = client.get(f"/topics/{h1.id}/chain", headers=auth).json()["chain"]
    assert [c["title"] for c in chain] == ["Homepage H1", "Homepage H1 follow-up"]
    assert "#1 Mo Jones" in chain[0]["messages"] and "#2 Jane Smith" in chain[0]["messages"]
    assert client.get("/topics/nope/chain", headers=auth).status_code == 404

    # Text search finds a session by its topics, and lists them.
    hits = client.get("/items", headers=auth, params={"text": "homepage"}).json()["items"]
    assert {h["row_id"] for h in hits} == {"clickup-session-ch1-b1", "clickup-session-ch1-c1"}
    assert all(h["topics"] and "chain" not in h["topics"][0] for h in hits)

    # Deleting a session removes its topics and the links to them.
    store.delete("clickup-session-ch1-b1")
    assert not store.topics_for(["clickup-session-ch1-b1"]) and not store.links_for([f"topic:{later.id}"])


class FakeViews:
    """The main model pulling out views: two for the H1 topic (one credited to someone who didn't
    write in it, so it's dropped), none elsewhere. Fails once when told to."""

    fail = False
    prompts: list[str] = []

    def __init__(self, config):
        pass

    def generate_json(self, system, prompt, schema, images=None, max_tokens=4096):
        FakeViews.prompts.append(prompt)
        if FakeViews.fail:
            FakeViews.fail = False
            raise RuntimeError("overloaded")
        if prompt.startswith("TOPIC: Homepage H1\n"):
            return {"views": [
                {"person": "Jane Smith", "view": "The H1 should lead with resonance, not the keyword.",
                 "messages": [2, 99], "responds_to": "Mo Jones", "stance": "disagrees"},
                {"person": "Pat Nobody", "view": "Made up.", "messages": [1], "responds_to": None, "stance": "new"}]}
        return {"views": []}


def test_session_views(setup, monkeypatch):
    import time as time_module
    from datetime import date

    from fastapi.testclient import TestClient

    from tle.api import app
    from tle.connectors import clickup

    config, store = setup
    monkeypatch.setattr("tle.topics.LLM", FakeTopics)
    monkeypatch.setattr("tle.views.LLM", FakeViews)
    start = (date.today() - timedelta(days=3)).isoformat()
    config.sources["clickup"] = {"channels": [{"id": "ch1", "name": "marketing", "mode": "sessions", "from": start,
                                               "include_links": False, "views": True}]}
    fake = FakeChat(int(time_module.time() * 1000))
    fake.messages[-1]["date"] -= 3.5 * 3600_000

    # The first session's views call fails: noted on the session, and tried again next sync.
    FakeViews.fail = True
    clickup.sync(config, store, client=fake)
    assert store.get("clickup-session-ch1-a1").source_metadata["views_error"] == "overloaded"
    clickup.sync(config, store, client=fake)
    assert "views_error" not in store.get("clickup-session-ch1-a1").source_metadata

    [view] = store.views_for(["clickup-session-ch1-b1"])
    assert (view.person, view.stance, view.responds_to) == ("Jane Smith", "disagrees", "Mo Jones")
    assert view.messages == ["b2"]  # a #number outside the topic is dropped
    # The later topic's call gets the H1 topic it continues as context.
    assert any(p.startswith("TOPIC: Homepage H1 follow-up") and "Homepage H1. Keyword or resonance." in p
               for p in FakeViews.prompts)

    data = TestClient(app).get("/items/clickup-session-ch1-b1", headers={"Authorization": "Bearer secret"}).json()
    assert data["topics"][0]["views"] == [{"person": "Jane Smith", "view": view.view, "stance": "disagrees",
                                           "responds_to": "Mo Jones", "messages": [2]}]
    store.delete("clickup-session-ch1-b1")
    assert not store.views_for(["clickup-session-ch1-b1"])


def test_topic_corrections(setup, monkeypatch):
    import time as time_module
    from datetime import date

    import pytest

    from tle import topics
    from tle.connectors import clickup

    config, store = setup
    monkeypatch.setattr("tle.topics.LLM", FakeTopics)
    start = (date.today() - timedelta(days=3)).isoformat()
    config.sources["clickup"] = {"channels": [{"id": "ch1", "name": "marketing", "mode": "sessions", "from": start,
                                               "include_links": False}]}
    fake = FakeChat(int(time_module.time() * 1000))
    fake.messages[-1]["date"] -= 3.5 * 3600_000
    clickup.sync(config, store, client=fake)
    first = store.get("clickup-session-ch1-a1")
    playbook, unassigned = store.topics_for([first.row_id])

    # Moving the last message out of Unassigned removes it; the topic gets the message and is credited.
    topics.move(first, "a3", playbook.id, "jane@acme.com", store)
    [moved] = store.topics_for([first.row_id])
    assert moved.messages == ["a1", "a2", "a3"] and moved.made_by == "jane@acme.com"
    # A message can start a new topic, which becomes the title if it's the biggest.
    topics.move(first, "a1", "new", "jane@acme.com", store, title="Deck vs ebook")
    assert [(t.title, t.messages) for t in store.topics_for([first.row_id])] == \
        [("Playbook format", ["a2", "a3"]), ("Deck vs ebook", ["a1"])]
    topics.rename(first, moved.id, "Playbook: ebook", "Ebook it is.", "jane@acme.com", store)
    assert store.get(first.row_id).source_metadata["title"] == "Playbook: ebook, and 1 more topic"
    with pytest.raises(ValueError):
        topics.move(first, "zz", moved.id, "jane@acme.com", store)

    # Links: remove the model's, add one by hand. The model then leaves the session's topics alone.
    later = store.get("clickup-session-ch1-c1")
    [follow_up] = store.topics_for([later.row_id])
    [(model_link, h1)] = topics.direct_links(follow_up, store)
    topics.unlink(later, model_link.id, "jane@acme.com", store)
    assert topics.chain(follow_up.id, store) == [store.get_topics([follow_up.id])[0]]
    assert topics.corrected(later.row_id, store)
    assert [t.id for t in topics.candidates(later, store)] == [h1.id, *[t.id for t in reversed(store.topics_for([first.row_id]))]]
    topics.link(later, follow_up.id, h1.id, "jane@acme.com", store)
    with pytest.raises(ValueError):
        topics.link(later, follow_up.id, h1.id, "jane@acme.com", store)
    assert [t.id for t in topics.chain(follow_up.id, store)] == [h1.id, follow_up.id]
    assert topics.find(later, config, store)[0].made_by == "jane@acme.com"  # kept, not redone
    assert topics.direct_links(follow_up, store)[0][0].made_by == "jane@acme.com"


def test_quick_model():
    """Quick jobs use the provider's cheaper model, or the tagging model when set to "Same as tagging"."""
    from tle.config import build_config

    config = build_config()
    assert config.quick.model == "claude-haiku-5-5" and config.llm.model == "claude-sonnet-5-5"
    same = build_config({"fast": None})
    assert same.fast is None and same.quick is same.llm


TRANSCRIPT = """00:00:04
Jane Smith: Thanks for joining. I want to talk about pricing.
Mo Jones: Sure. I think we undercharge for strategy work.
00:01:10
Jane Smith: Agreed, value pricing is the way to go.
Mo Jones: Clients pay for outcomes, not hours."""

NOTES = """Notes by Gemini
Summary
Jane Smith and Mo Jones discussed pricing. Mo argued the team undercharges for strategy.
Details
Jane agreed that value pricing is better.
Suggested next steps
Mo will draft a pricing page."""


def test_meeting_records(setup, monkeypatch):
    from tle import meetings
    from tle.ingest import ingest_text

    config, store = setup
    assert meetings.classify(TRANSCRIPT) == "transcript"
    assert meetings.classify(NOTES) == "notes"
    assert meetings.classify(f"📝 Notes\n{NOTES}\n\n📖 Transcript\n{TRANSCRIPT}") == "both"
    assert meetings.classify("Our view on pricing, in three paragraphs.") == ""
    assert meetings.classify("Our view on pricing.", suggested="notes") == "notes"  # the author check's answer

    notes_seen = []
    real_tag = __import__("tle.ingest", fromlist=["tag"]).tag
    monkeypatch.setattr("tle.ingest.tag", lambda *a, **k: notes_seen.append(k.get("note")) or real_tag(*a, **k))

    def save(text, source_type="meeting", **metadata):
        return ingest_text(text, config=config, store=store, source_type=source_type, authors=["Jane Smith"],
                           date_created=datetime(2026, 11, 6), source_metadata={"label": "Pricing call", **metadata})

    # Notes: kept, but marked as paraphrased in the citation and results, and the tagger is told.
    notes = save(NOTES)
    assert notes.source_metadata["record"] == "notes"
    assert notes.citation == "JANE SMITH - MEETING NOTES PRICING CALL - 06.11.2026"
    result = notes.to_result()
    assert result["paraphrased"] is True and "never in quotation marks" in result["paraphrased_flag"]
    assert meetings.TAGGING_NOTE in notes_seen[-1]

    # A transcript is unchanged.
    transcript = save(TRANSCRIPT)
    assert transcript.source_metadata["record"] == "transcript" and "paraphrased" not in transcript.to_result()

    # Both in one doc: the transcript is the text, the notes are kept beside it and given to the tagger.
    both = save(f"📝 Notes\n{NOTES}\n\n📖 Transcript\n{TRANSCRIPT}")
    assert both.source_metadata["record"] == "both" and both.extracted_text == TRANSCRIPT
    assert "Mo will draft a pricing page." in both.source_metadata["notes"]
    assert "The meeting's notes, for context" in notes_seen[-1]
    assert "paraphrased" not in both.to_result()

    # A document with the same headings isn't treated as notes unless the author check says so.
    doc = save(NOTES, source_type="document")
    assert "record" not in doc.source_metadata and "paraphrased" not in doc.to_result()
    forwarded = save(NOTES, source_type="email", record="notes")
    assert forwarded.to_result()["paraphrased"] is True


class InternalDocLLM:
    """The author check for shared docs: Jane wrote them, and they're meeting notes."""

    def __init__(self, config):
        pass

    def generate_json(self, system, prompt, schema, images=None):
        return {"authors": ["Jane Smith"], "date": "", "reason": "Her notes.", "record": "notes",
                "source_type": "meeting"}


def test_shared_docs_are_context(setup, monkeypatch):
    from tle.connectors import drive_sync, shared
    from tle.ingest import ingest_text
    from tle.links import Page

    config, store = setup
    monkeypatch.setattr("tle.attribution.LLM", InternalDocLLM)
    opened = []

    def read_file(fid, config):
        opened.append(fid)
        if fid.startswith("private"):
            return None
        return ({"id": fid, "name": "Pricing call notes", "mimeType": "application/vnd.google-apps.document",
                 "createdTime": "2026-11-05T10:00:00Z", "webViewLink": f"https://docs.google.com/document/d/{fid}/edit",
                 "owners": [{"emailAddress": "jane@acme.com"}]}, NOTES)

    monkeypatch.setattr(drive_sync, "read_file", read_file)
    monkeypatch.setattr(shared, "fetch_link", lambda url, config: Page(
        url=url, final_url="https://accounts.google.com/signin", text="Sign in to continue", title="Sign in"))
    when = datetime(2026, 11, 6, tzinfo=timezone.utc)
    doc = "https://docs.google.com/document/d/" + "a" * 30 + "/edit"
    hidden = "https://docs.google.com/document/d/private" + "b" * 25 + "/edit"
    message = f"Notes from the pricing call, the key point is value pricing: {doc} and {hidden}"

    def share(text, prefix="clickup-link"):
        links = shared.read_links([(text, "Mo Jones", when)], via="clickup", place="ClickUp #marketing",
                                  row_prefix=prefix, config=config, store=store)
        shared.save_pages(links, commentary=shared.has_commentary([text]), via="clickup", place="ClickUp #marketing",
                          metadata={}, config=config, store=store)
        return links

    links = share(message)
    # The doc is read through Drive and stored once, as Jane's meeting notes: never reading.
    [saved] = [i for i in store.search(SearchQuery(source_types=["meeting"]))]
    assert saved.drive_file_id == "a" * 30 and not saved.reading and saved.authors == ["Jane Smith"]
    assert saved.source_metadata["record"] == "notes" and saved.source_metadata["label"] == "Pricing call notes"
    context = shared.link_metadata(links)["links"]
    assert context[0]["saved_as"] == saved.row_id and context[0]["internal"]
    # The one the service account can't open is kept as a bare link.
    assert context[1]["unread"] and "saved_as" not in context[1]

    # The chat item that shared it links to it, and the doc's page shows where it was shared.
    chat = ingest_text(message, config=config, store=store, source_type="clickup", authors=["Mo Jones"],
                       source_metadata={"label": "ClickUp #marketing", **shared.link_metadata(links)})
    [link] = store.links_for([f"item:{saved.row_id}"])
    assert (link.from_id, link.kind, link.reason) == (f"item:{chat.row_id}", "related", "Shared in ClickUp #marketing")

    # Shared again, even with no comment: reused, not read again, and still not reading.
    opened.clear()
    share(doc, prefix="clickup-link-2")
    assert opened == [] and len(store.search(SearchQuery(source_types=["meeting"]))) == 1

    # A Google sign-in page is never kept as a page's text.
    assert shared.sign_in_page(Page(url="x", final_url="https://accounts.google.com/v3/signin", text="..."))
    assert not shared.sign_in_page(Page(url="x", final_url="https://example.com/post", text="A long post. " * 300,
                                        title="How to sign in to everything"))


class FakeRelated:
    """The quick model relating items: the pricing notes are the same meeting as the transcript,
    and the chat session is related. Everything else is unrelated."""

    prompts: list[str] = []

    def __init__(self, config):
        pass

    def generate_json(self, system, prompt, schema, images=None, max_tokens=4096):
        import re

        FakeRelated.prompts.append(prompt)
        out = []
        for i, line in re.findall(r"- id (\d+): (.*)", prompt):
            if "Pricing call notes" in line and "Pricing call (meeting" in prompt.split("CANDIDATES")[0]:
                out.append({"id": i, "relation": "same_meeting", "reason": "Both are the 5 Nov pricing call."})
            elif line.startswith("ClickUp #marketing"):
                out.append({"id": i, "relation": "related", "reason": "The pricing page drafted after the call."})
        return {"related": out}


def test_related_links(setup, monkeypatch):
    from tle import related
    from tle.ingest import ingest_text

    config, store = setup
    monkeypatch.setattr("tle.related.LLM", FakeRelated)
    day = datetime.now(timezone.utc) - timedelta(days=1)

    def save(text, label, source_type="meeting", authors=("Jane Smith", "Mo Jones"), when=day, **metadata):
        return ingest_text(text, config=config, store=store, source_type=source_type, authors=list(authors),
                           date_created=when, source_metadata={"label": label, **metadata})

    transcript = save(TRANSCRIPT + "\nMo Jones: I'll draft the pricing page for value pricing.", "Pricing call")
    notes = save(NOTES, "Pricing call notes", source_type="email")
    store.update(notes.row_id, {"source_metadata": {**notes.source_metadata, "record": "notes"}})
    chat = save("#1 Mo Jones (06.11.2026 09:00): First draft of the pricing page, built on value pricing and outcomes.",
                "ClickUp #marketing", source_type="clickup", authors=["Mo Jones"], when=day + timedelta(hours=20))
    save("Pricing for value, outcomes and strategy: a long-held view.", "Old post", source_type="linkedin",
         authors=["Mo Jones"], when=day - timedelta(days=60))  # too long before to be a candidate

    assert related.catch_up(config, store) == 4  # every item saved since the last run
    def ends(item):
        return {lk.to_id if lk.from_id == f"item:{item.row_id}" else lk.from_id
                for lk in store.links_for([f"item:{item.row_id}"])}
    assert ends(transcript) == {f"item:{notes.row_id}", f"item:{chat.row_id}"}
    # The notes are kept on the transcript, as if they'd come in one doc.
    joined = store.get(transcript.row_id).source_metadata
    assert joined["record"] == "both" and "Mo will draft a pricing page." in joined["notes"]
    assert joined["notes_from"] == notes.row_id
    assert not any("Old post" in p for p in FakeRelated.prompts)

    # Chat from the same channel is never compared with itself.
    other = save("#1 Mo Jones (06.11.2026 10:00): The pricing page needs value pricing outcomes.", "#marketing",
                 source_type="clickup", authors=["Mo Jones"], when=day + timedelta(hours=21),
                 clickup_channel_id="ch1")
    store.update(chat.row_id, {"source_metadata": {**chat.source_metadata, "clickup_channel_id": "ch1"}})
    assert chat.row_id not in {c.row_id for c in related.candidates(other, store)}
    store.delete(other.row_id)

    # Nothing new: nothing is looked at again, and pairs already linked aren't offered again.
    calls = len(FakeRelated.prompts)
    assert related.catch_up(config, store) == 0 and len(FakeRelated.prompts) == calls
    assert notes.row_id not in {c.row_id for c in related.candidates(transcript, store)}


def test_look_back_days_per_channel():
    from tle.connectors.clickup import _days

    assert _days({"look_back_days": 14}, 7) == 14
    assert _days({"look_back_days": "3"}, 7) == 3
    assert _days({}, 10) == 10  # a channel saved before it was per channel: the old shared setting
    assert _days({"look_back_days": "soon"}, 7) == 7


def test_sync_runs_are_recorded(setup):
    from tle import syncs

    config, store = setup
    assert syncs.status(store, "clickup")["finished"] is None
    assert syncs.run("clickup", lambda: "3 chat sessions", store) == "3 chat sessions"
    done = syncs.status(store, "clickup")
    assert done["finished"] and done["summary"] == "3 chat sessions" and not done["running"]

    # A run that starts while another is going steps aside.
    assert syncs.begin(store, "clickup")
    assert syncs.status(store, "clickup")["running"]
    assert syncs.run("clickup", lambda: pytest.fail("ran twice"), store).startswith("Another run")
    syncs.end(store, "clickup", summary="1 chat session")

    # A failure is recorded, and raised for the job to report.
    with pytest.raises(RuntimeError):
        syncs.run("drive", lambda: (_ for _ in ()).throw(RuntimeError("no access")), store)
    assert syncs.status(store, "drive")["error"] == "no access"


def test_channel_added_again_starts_over(setup, monkeypatch):
    """Removing a channel and adding it again re-reads it from its Starting from date."""
    import time as time_module
    from datetime import date

    from tle.connectors import clickup

    config, store = setup
    start = (date.today() - timedelta(days=3)).isoformat()
    channel = {"id": "ch1", "name": "marketing", "mode": "sessions", "from": start, "include_links": False}
    config.sources["clickup"] = {"channels": [channel]}
    fake = FakeChat(int(time_module.time() * 1000))
    monkeypatch.setattr(clickup, "SESSIONS_PER_SYNC", 1)
    clickup.sync(config, store, client=fake)
    clickup.sync(config, store, client=fake)
    cursor = store.get_state("clickup-sessions:ch1")["cursor"]
    store.delete("clickup-session-ch1-a1")

    # Saved before rows were stamped: carries on where it was, so the deleted session stays gone.
    assert clickup.sync(config, store, client=fake)["sessions"] == 0
    assert store.get_state("clickup-sessions:ch1")["cursor"] == cursor and not store.get("clickup-session-ch1-a1")

    # Added again: read from the start, a batch at a time.
    channel["added"] = "2026-11-09T08:00:00+00:00"
    assert clickup.sync(config, store, client=fake)["sessions"] == 1
    assert store.get("clickup-session-ch1-a1")
    assert store.get_state("clickup-sessions:ch1")["added"] == channel["added"]


def test_new_channel_rows_are_stamped():
    from tle import settings_form

    form = {"sections": "clickup", "clickup__channel__0__id": "ch1", "clickup__channel__0__mode": "sessions"}
    [row] = settings_form.read(form, {})["clickup"]["channels"]
    assert row["added"]
    again = settings_form.read(form, {"clickup": {"channels": [row]}})["clickup"]["channels"][0]
    assert again["added"] == row["added"]  # kept while the channel stays


def test_drive_files_edited_since_are_read_again(setup, monkeypatch):
    from datetime import timezone as tz

    from tle.connectors import drive_sync

    config, store = setup
    config.sources["drive"] = {"folders": [{"id": "folder", "source_types": ["document"]}]}
    texts = {"d1": "Our view: value pricing beats hourly rates."}
    downloads = []

    def download(service, f):
        downloads.append(f["id"])
        return texts[f["id"]].encode(), f["name"] + ".txt", "text/plain"

    now = datetime.now(tz.utc)
    stamp = lambda ago: (now - ago).isoformat().replace("+00:00", "Z")
    listing = [{"id": "d1", "name": "Pricing", "createdTime": stamp(timedelta(days=3)), "modifiedTime": stamp(timedelta(days=3)),
                "owners": [{"emailAddress": "jane@acme.com"}], "webViewLink": "https://drive/d1"}]
    monkeypatch.setattr(drive_sync, "_service", lambda: None)
    monkeypatch.setattr(drive_sync, "_files", lambda service, folder_id: iter(listing))
    monkeypatch.setattr(drive_sync, "_download", download)
    monkeypatch.setattr("tle.attribution.LLM", AttributionLLM)
    monkeypatch.setattr(AttributionLLM, "generate_json", lambda self, system, prompt, schema, images=None:
                        {"authors": ["Jane Smith"], "date": "", "reason": "Hers."})

    assert drive_sync.sync(config, store) == {"saved": 1, "updated": 0}
    item = store.find_by_drive_file_id("d1")
    store.update(item.row_id, {"status": "reviewed", "authors": ["Mo Jones"]})  # corrected in the dashboard

    # Not edited: skipped without even downloading.
    downloads.clear()
    assert drive_sync.sync(config, store) == {"saved": 0, "updated": 0} and downloads == []

    # Edited a few minutes ago: may still be being written, so it waits.
    texts["d1"] = "Our view: value pricing beats hourly rates, and retainers beat both."
    listing[0]["modifiedTime"] = stamp(timedelta(minutes=5))
    assert drive_sync.sync(config, store) == {"saved": 0, "updated": 0} and downloads == []

    # Left alone for half an hour: read again and tagged again, keeping its status and authors.
    listing[0]["modifiedTime"] = stamp(timedelta(minutes=40))
    assert drive_sync.sync(config, store) == {"saved": 0, "updated": 1}
    again = store.find_by_drive_file_id("d1")
    assert "retainers beat both" in again.extracted_text and again.row_id == item.row_id
    assert again.status == "reviewed" and again.authors == ["Mo Jones"]
    assert again.source_metadata["drive_modified"] == listing[0]["modifiedTime"]

    # Touched without a change to its text (a comment, say): noted, not tagged again.
    listing[0]["modifiedTime"] = stamp(timedelta(minutes=35))
    assert drive_sync.sync(config, store) == {"saved": 0, "updated": 0}
    assert store.find_by_drive_file_id("d1").source_metadata["drive_modified"] == listing[0]["modifiedTime"]

    # A file saved before edit times were recorded counts from when it was saved.
    old = store.find_by_drive_file_id("d1").model_copy(update={"source_metadata": {}, "date_ingested": now - timedelta(days=1)})
    assert not drive_sync.changed(old, {"modifiedTime": stamp(timedelta(days=2))})  # before it was saved
    assert drive_sync.changed(old, {"modifiedTime": stamp(timedelta(minutes=31))})  # after


def test_shared_page_is_only_credited_to_people_it_shows(setup, monkeypatch):
    """A page someone shared with comments of their own isn't theirs unless the page shows them."""
    from tle.connectors import shared
    from tle.links import Page

    config, store = setup
    pages = {
        "https://developers.google.com/search/docs/helpful-content": Page(
            url="", final_url="https://developers.google.com/search/docs/helpful-content", site="Google for Developers",
            title="Creating helpful, reliable, people-first content", text="Google's automated ranking systems... " * 40),
        "https://www.linkedin.com/posts/jane-smith_pricing-activity-1": Page(
            url="", final_url="https://www.linkedin.com/posts/jane-smith_pricing-activity-1", site="LinkedIn",
            title="Jane Smith on LinkedIn: value pricing", text="Jane Smith\\nValue pricing beats hourly rates. " * 20),
    }
    monkeypatch.setattr(shared, "fetch_link", lambda url, config: Page(**{**pages[url].__dict__, "url": url}))
    # The model credits whoever shared it, as it did with Google's documentation.
    monkeypatch.setattr("tle.attribution.LLM", lambda c: type("L", (), {"generate_json": lambda self, *a, **k:
                        {"authors": ["Jane Smith"], "date": "", "reason": "Jane wrote the comments.", "record": "neither"}})())
    when = datetime(2026, 10, 4, tzinfo=timezone.utc)
    message = "Google updated their helpful content page, some highlights: effort, originality, accuracy " \
              "https://developers.google.com/search/docs/helpful-content and mine https://www.linkedin.com/posts/jane-smith_pricing-activity-1"
    links = shared.read_links([(message, "Jane Smith", when)], via="clickup", place="ClickUp #news",
                              row_prefix="clickup-link", config=config, store=store)
    google, post = links
    assert not google.ours and google.authors == [] and "the page doesn't show them" in google.reason
    assert post.ours and post.authors == ["Jane Smith"]  # her own post, with her name on it

    shared.save_pages(links, commentary=True, via="clickup", place="ClickUp #news", metadata={}, config=config, store=store)
    assert store.get(google.row_id) is None  # context for her comments, not an item of its own
    assert store.get(post.row_id).authors == ["Jane Smith"] and not store.get(post.row_id).reading


class FakeSlackHistory:
    """A Slack channel's history for the session job: the same conversation as FakeChat (ClickUp),
    with a bot post and a join that aren't anyone's words, and a file shared in a thread."""

    def __init__(self, now):
        hour = 3600
        ts = lambda h: f"{now - h * hour:.6f}"  # noqa: E731
        jane, mo = "U01234567", "U07654321"
        self.top = [
            {"ts": ts(30), "user": jane, "text": "Should the playbook be a deck or an ebook?"},
            {"ts": ts(29), "user": mo, "text": "Ebook. Decks don't get read.", "reply_count": 1},
            {"ts": ts(28.5), "subtype": "channel_join", "user": "U0BOT", "text": "joined"},
            {"ts": ts(28.2), "bot_id": "B1", "text": "Reminder: standup"},
            {"ts": ts(28), "user": jane, "text": "Fair :joy:"},
            {"ts": ts(10), "user": mo, "text": "H1 should lead with the keyword."},
            {"ts": ts(9), "user": jane, "text": "Not at the cost of resonance."},
            {"ts": ts(1), "user": mo, "text": "Webinar recorded."},
        ]
        self.ebook = self.top[1]["ts"]
        self.thread = [self.top[1], {"ts": ts(20), "user": "U09999999", "text": "Agreed, ebook.", "thread_ts": self.ebook,
                                     "files": [{"id": "F1", "name": "outline.txt", "url_private_download": "https://files/F1"}]}]
        self.history_calls = []

    def conversations_history(self, channel, oldest, limit, cursor=None):
        self.history_calls.append(float(oldest))
        newest_first = sorted((m for m in self.top if float(m["ts"]) >= float(oldest)), key=lambda m: -float(m["ts"]))
        return {"messages": newest_first, "response_metadata": {}}

    def conversations_replies(self, channel, ts, limit, cursor=None):
        assert ts == self.ebook
        return {"messages": self.thread, "response_metadata": {}}

    def users_info(self, user):
        return {"user": {"real_name": "Outside Person"}}

    def conversations_info(self, channel):
        return {"channel": {"name": "marketing"}}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.example/{message_ts}"}


def test_slack_sessions_work_like_clickup(setup, monkeypatch):
    import time as time_module
    from datetime import date

    from tle.connectors import slack

    config, store = setup
    monkeypatch.setattr("tle.topics.LLM", FakeTopics)
    monkeypatch.setattr(slack, "_download_file", lambda f: b"Outline: why ebooks beat decks.")
    monkeypatch.setattr("tle.attribution.LLM", AttributionLLM)
    monkeypatch.setattr(AttributionLLM, "generate_json", lambda self, *a, **k: {"authors": [], "date": "", "reason": ""})
    start = (date.today() - timedelta(days=3)).isoformat()
    config.sources["slack"] = {"channels": [{"id": "C1", "name": "marketing", "mode": "sessions", "from": start,
                                             "include_links": False}]}
    fake = FakeSlackHistory(time_module.time())
    sessions_saved = lambda: sorted(i.row_id for i in store.search(SearchQuery(source_types=["slack"], limit=50)))

    # A batch at a time, oldest first; the session still going isn't saved; bots and joins aren't words.
    monkeypatch.setattr(slack, "SESSIONS_PER_SYNC", 1)
    assert slack.sync(config, store, client=fake) == {"channels": 1, "sessions": 1}
    first = store.get(f"slack-session-C1-{fake.top[0]['ts']}".replace(".", ""))
    lines = first.extracted_text.splitlines()
    assert lines[0].startswith("#1 Jane Smith (") and lines[1].startswith("#2 Mo Jones (")
    assert lines[2].startswith("    ↳ Outside Person (") and lines[2].endswith("Agreed, ebook.")
    assert lines[3].startswith("#3 Jane Smith (") and lines[3].endswith("Fair 😂")  # Slack's emoji codes, as emoji
    assert "Reminder" not in first.extracted_text and "joined" not in first.extracted_text
    meta = first.source_metadata
    assert meta["place"] == "slack:C1" and meta["channel_id"] == "C1" and meta["label"] == "#marketing" and meta["session"]
    assert first.citation.startswith("JANE SMITH, MO JONES, OUTSIDE PERSON - SLACK #MARKETING")
    # Its topics are found, and the file shared in the thread is an item of its own.
    assert [t.title for t in store.topics_for([first.row_id])] == ["Playbook format", "Unassigned"]
    assert store.get("slack-file-F1") is not None
    slack.sync(config, store, client=fake)
    assert len(sessions_saved()) == 2 and slack.sync(config, store, client=fake)["sessions"] == 0

    # Channels saving reacted messages aren't read here; their events still are.
    config.sources["slack"]["channels"][0]["mode"] = "reaction"
    assert slack.sync(config, store, client=fake) == {"channels": 0, "sessions": 0}
    # And a channel set to sessions (or the old every-message mode) ignores live message events.
    config.sources["slack"]["channels"][0]["mode"] = "all"
    slack.handle_event({"event": {"type": "message", "channel": "C1", "user": "U01234567", "ts": "1.1", "text": "hi"}},
                       config, store)
    assert len(sessions_saved()) == 2


def test_slack_text_and_names_of_people_who_left(setup):
    from tle.connectors.slack import _Names

    config, _ = setup

    class Users:
        def users_info(self, user):
            return {"user": {"name": "bibsasha", "real_name": None, "deleted": True,
                             "profile": {"real_name": "Aleks", "display_name": "Aleks"}}}

    names = _Names(Users(), config)
    assert names("U0LEFT") == "Aleks"  # left the workspace: the name is only in the profile
    assert names.text("Agreed :+1::skin-tone-2: see <https://x.com/?a=1&amp;b=2|this> &lt;3") == \
        "Agreed 👍🏻 see https://x.com/?a=1&b=2 <3"
