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
    from tle.connectors import email as email_connector
    from tle.links import Page

    config, store = setup
    monkeypatch.setattr(email_connector, "fetch_link", lambda url, config: Page(
        url=url, final_url=url, text="Pricing on outcomes\n\nThe article body.", title="Pricing on outcomes",
        author="Mo Jones", site="acme.com", date=datetime(2026, 8, 1)))
    raw = _raw_email("jane@acme.com", "", "Mo's piece, the bit on retainers matters: https://acme.com/blog/pricing")
    assert email_connector.process_raw(raw, config, store) == (True, 1)
    [item] = store.search(SearchQuery())
    assert item.source_type == "web" and item.visibility == "public"
    assert item.source_metadata["note"].startswith("Mo's piece")
    assert item.source_metadata["page_author"] == "Mo Jones" and item.date_created.date().isoformat() == "2026-08-01"


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
    from tle.connectors import slack
    from tle.links import Page

    config, store = setup

    def fetch(url, config):
        if "broken" in url:
            raise ValueError("404")
        return Page(url=url, final_url=url, text="Why retainers beat projects.\n\nThe post body.",
                    title="Retainers", author="Mo Jones", site="linkedin.com", date=datetime(2026, 8, 1))

    monkeypatch.setattr("tle.connectors.shared.fetch_link", fetch)
    link = "https://www.linkedin.com/posts/mo-jones_retainers"
    # A link and a line about it: only the linked post is saved, with the line as its note.
    lone = {"ts": "1726740000.000100", "user": "U01234567", "text": f"Worth a read <{link}|linkedin.com>", "parent": True}
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack([lone]))
    slack.handle_event(_reaction(lone["ts"]), config, store)
    slack.handle_event(_reaction(lone["ts"]), config, store)  # again: nothing duplicated
    [item] = store.search(SearchQuery())
    assert item.row_id.startswith("slack-link-") and item.source_type == "linkedin"
    assert item.source_metadata["note"] == f"Worth a read {link}" and item.source_metadata["url"] == link
    assert item.source_metadata["submitted_by"] == "Jane Smith" and item.date_created.year == 2026

    # A thread talking about a link: the post and the discussion are both saved.
    thread = [{**lone, "ts": "1726750000.000100", "thread_ts": "1726750000.000100", "reply_count": 1},
              {"ts": "1726750060.000200", "user": "U07654321", "text": "Agree, retainers win.",
               "thread_ts": "1726750000.000100"}]
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack(thread))
    slack.handle_event(_reaction("1726750000.000100"), config, store)
    ids = {i.row_id for i in store.search(SearchQuery())}
    assert len(ids) == 2 and "slack-C01234567-1726750000000100" in ids  # the same post isn't saved twice

    # A link that can't be read is a problem, and the message is saved instead.
    broken = {"ts": "1726760000.000100", "user": "U01234567", "text": "Look <https://acme.com/broken>", "parent": True}
    monkeypatch.setattr(slack, "_client", lambda: FakeSlack([broken]))
    slack.handle_event(_reaction(broken["ts"]), config, store)
    assert "slack-C01234567-1726760000000100" in {i.row_id for i in store.search(SearchQuery())}
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

    assert clickup.sync(config, store, client=fake) == {"tasks": 1, "docs": 1, "chat": 1}
    items = {i.row_id: i for i in store.search(SearchQuery(limit=50))}
    task = items["clickup-task-t1"]
    assert task.source_type == "clickup" and "Mo Jones (" in task.extracted_text and task.source_metadata["comment_count"] == 1
    assert items["clickup-file-a1"].extracted_text == "Meeting notes on pricing."
    assert any(k.startswith("clickup-link-") for k in items)
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
