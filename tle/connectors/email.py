"""Email: a dedicated inbox (e.g. ideas@yourcompany.com) that people send or forward notes,
transcripts and voice notes to. Everything that arrives is ingested, subject to the
sender rules below. The inbox, in Google Workspace, is read by a scheduled job
(email_gmail.py).

- The sender isn't assumed to be the author, since people send in other people's work.
  Fields at the top of the body set the author and more (see tle/attribution.py):
      Author: Jane Smith
      Date: 2026-09-12
  Without them, an LLM works out the author (and the date, if the content shows it).
  If it can't, the sender is used and the item is flagged for review.
- Start the subject with a source type to set it, e.g. "meeting: Q3 all-hands" or
  "voicenote: pricing rant", or add a "Type:" field. Otherwise the body is stored as
  source type "email".
- Each attachment, and each link in a short email, becomes its own item. A short body
  (up to 500 characters, not counting links) alongside attachments or links is treated
  as a note about them: it guides tagging and author detection and is kept in each
  item's metadata. A longer body is saved as content itself, with its links left inline.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from ..attribution import attribute, parse_date, parse_fields
from ..attribution import metadata as attribution_metadata
from .. import events
from ..config import SOURCE_TYPES, Config
from ..extract import extract_text, guess_mime
from ..ingest import ingest_text
from ..links import URL, default_source_type, find_urls
from ..links import fetch as fetch_link
from ..models import Item
from ..store import Store

log = logging.getLogger(__name__)

SUBJECT_TYPE = re.compile(rf"^\s*({'|'.join(SOURCE_TYPES)})\s*:\s*(.*)$", re.IGNORECASE)
NOTE_MAX_CHARS = 500


@dataclass
class ParsedEmail:
    sender: str
    subject: str
    body: str
    date: datetime | None
    message_id: str
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw)
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def parse_message(raw: bytes) -> ParsedEmail:
    """Parses a raw RFC 822 message, as Gmail returns it."""
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
    _, sender = parseaddr(str(msg.get("From", "")))
    try:
        date = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
    except (TypeError, ValueError):
        date = None

    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)

    attachments = []
    for part in msg.iter_attachments():
        # Inline images are almost always signature logos and tracking pixels.
        if part.get_content_maintype() == "image" and (part.get_content_disposition() == "inline" or part["Content-ID"]):
            continue
        if part.get_content_type() == "text/calendar":
            continue
        data = part.get_payload(decode=True)
        if data:
            attachments.append((part.get_filename() or "attachment", data, part.get_content_type()))

    return ParsedEmail(
        sender=sender.lower(),
        subject=str(msg.get("Subject", "")),
        body=body.strip(),
        date=date,
        message_id=str(msg.get("Message-ID", "")).strip("<> "),
        attachments=attachments,
    )


def sender_allowed(sender: str, config: Config) -> bool:
    """Anyone may send, unless only_people is on: then only people with that email on People."""
    if config.sources.get("email", {}).get("only_people", False):
        return config.person_by_email(sender) is not None
    return True


def handle_email(email: ParsedEmail, config: Config, store: Store) -> list[Item]:
    if not sender_allowed(email.sender, config):
        raise PermissionError(f"{email.sender} isn't an allowed sender.")

    fields, body = parse_fields(email.body)
    source_type, title = None, email.subject.strip()
    match = SUBJECT_TYPE.match(email.subject or "")
    if match:
        source_type, title = match.group(1).lower(), match.group(2).strip()
    if fields.get("source_type", "").lower() in SOURCE_TYPES:
        source_type = fields["source_type"].lower()
    title = fields.get("label") or title
    visibility = (fields.get("visibility") or "").lower() or None
    given_date = parse_date(fields["date"]) if fields.get("date") else None

    # A short body alongside attachments or links is a note about them ("this is Jane's
    # take on pricing"): it guides the tagging and author detection rather than being
    # saved itself. A long body is content in its own right, and its links stay inline.
    sharing = len(URL.sub("", body).strip()) <= NOTE_MAX_CHARS
    links = find_urls(body) if sharing else []
    note = body if sharing else ""
    context = f"Subject: {email.subject}\n\n{body}"

    pieces = []  # (text, default source type, extra metadata, context for attribution, page date)
    for filename, data, mime in email.attachments:
        try:
            text = extract_text(data, filename, config, mime)
        except ValueError as exc:  # e.g. a spreadsheet
            events.problem(store, f"Couldn't read {filename}, attached to \"{email.subject}\" from {email.sender}: {exc}",
                           via="email", actor=email.sender, key=email.message_id and f"{email.message_id}:{filename}")
            continue
        default_type = "voicenote" if guess_mime(filename, mime).startswith("audio/") else "document"
        pieces.append((text, default_type, {"file_name": filename, "mime_type": mime,
                                            "label": title or filename.rsplit(".", 1)[0]}, context, None))
    for url in links:
        try:
            page = fetch_link(url, config)
        except Exception as exc:  # noqa: BLE001
            events.problem(store, f"Couldn't read the link {url} in \"{email.subject}\" from {email.sender}: {exc}",
                           via="email", actor=email.sender, key=email.message_id and f"{email.message_id}:{url}", url=url)
            continue
        pieces.append((page.text, default_source_type(page.final_url),
                       {**page.metadata(), "label": title or page.site or page.title},
                       f"{page.context()}\n\n{context}", page.date))
    if body and (not (email.attachments or links) or not sharing):
        pieces.append((body, "email", {"label": title or None}, f"Subject: {email.subject}", None))

    items = []
    for text, default_type, extra, piece_context, page_date in pieces:
        if not text.strip():
            continue
        is_body = text is body
        who = attribute(text, config=config, submitter=email.sender, context=piece_context, fields=fields)
        metadata = {"subject": email.subject, "message_id": email.message_id,
                    **attribution_metadata(who, email.sender), **extra}
        items.append(ingest_text(
            text, config=config, store=store, source_type=source_type or default_type,
            authors=who.authors, date_created=given_date or who.date or page_date or email.date,
            visibility=visibility, source_metadata=metadata, note="" if is_body else note,
            via="email", actor=email.sender,
        ))
    if not items:
        raise ValueError("The email had no text, attachments or links to save.")
    return items


def process_raw(raw: bytes, config: Config, store: Store) -> tuple[bool, int]:
    """Returns (accepted, items saved). Rejected mail (sender not allowed, nothing to save)
    is reported rather than raised, so the poller can file it away instead of retrying."""
    email = parse_message(raw)
    try:
        items = handle_email(email, config, store)
    except (PermissionError, ValueError) as exc:
        log.warning("Rejected email from %s (%s): %s", email.sender, email.subject, exc)
        events.record(store, "email_rejected", f"Rejected \"{email.subject}\" from {email.sender}: {exc}",
                      level="warning", via="email", actor=email.sender)
        return False, 0
    log.info("Saved %d item(s) from %s (%s)", len(items), email.sender, email.subject)
    return True, len(items)
