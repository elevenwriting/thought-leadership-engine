"""What chat-style connectors (Slack, ClickUp) and the inbox share: the links in a conversation.

The database holds the company's own thinking, so what people say about a link is what's kept:
- A message or thread with commentary is the item, credited to the people who wrote it. The
  pages it links to are kept on it as context (title, site, author, an excerpt), which the
  tagger reads so it knows what they were responding to.
- A linked page written by someone on People (a colleague's own LinkedIn post, say) is their
  thought leadership, so it's also saved as an item of its own, credited to them.
- A link shared with no commentary is saved as reading: someone else's content, marked as not
  the company's view, left out of searches, citations and coverage unless asked for.
- The company's own docs (Google Docs and Drive files, Granola notes) are never reading. Each is
  stored once, as a meeting or a document of its own (a Drive file is matched on its file ID, so
  one already saved from a Drive folder is reused), and the item that shares it links to it.
  Drive files are read through the Drive API as the service account; one it can't open is kept
  as a bare link. A sign-in page is never kept as a page's text.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from .. import events
from ..attribution import Attribution, attribute, detect
from ..attribution import metadata as attribution_metadata
from ..config import Config
from ..ingest import ingest_text
from ..links import URL, Page, default_source_type, find_urls
from ..links import fetch as fetch_link
from ..store import Store

log = logging.getLogger(__name__)

# Fewer words than this beside the links ("worth a read", "FYI") isn't commentary.
COMMENT_MIN_WORDS = 4
LINKS_PER_THREAD = 5
EXCERPT_CHARS = 600
INTERNAL_HOSTS = ("docs.google.com", "drive.google.com", "granola.ai", "granola.so")
SIGN_IN = re.compile(r"\b(sign in|log in|login|sign-in)\b", re.IGNORECASE)
INTERNAL_TYPES = ["meeting", "document"]


def internal(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in INTERNAL_HOSTS)


def sign_in_page(page: Page) -> bool:
    """A page that asks to sign in rather than showing the content (a private Google Doc, say)."""
    host = urlparse(page.final_url).hostname or ""
    if host.startswith("accounts.") or "/signin" in page.final_url or "/login" in page.final_url:
        return True
    return len(page.text) < 2000 and bool(SIGN_IN.search(page.title or page.text[:300]))


def has_commentary(texts: list[str]) -> bool:
    """Whether the messages say anything of their own beyond the links they share."""
    return sum(len(URL.sub(" ", t or "").split()) for t in texts) >= COMMENT_MIN_WORDS


@dataclass
class Linked:
    url: str
    page: Page
    sharer: str
    when: datetime
    message: str
    row_id: str
    authors: list[str]       # who wrote the page, as far as can be told
    ours: bool                # every author is on People
    reason: str = ""
    internal: bool = False    # one of the company's own docs: stored once, never reading
    unread: bool = False      # couldn't be opened: kept as a bare link
    existing: str = ""        # the item it's already saved as
    drive_file_id: str = ""
    who: Attribution | None = None
    extra: dict = field(default_factory=dict)

    def context(self) -> dict:
        """What the item that shares it keeps about the page."""
        saved = self.existing or (self.row_id if (self.ours or self.internal) and not self.unread else None)
        return {k: v for k, v in {
            "url": self.page.final_url, "title": self.page.title, "site": self.page.site,
            "author": ", ".join(self.authors) or self.page.author, "shared_by": self.sharer,
            "excerpt": " ".join(self.page.text.split())[:EXCERPT_CHARS],
            "saved_as": saved, "internal": self.internal or None,
            "unread": "Couldn't be opened, so only the link is kept" if self.unread else None,
        }.items() if v}


def _authors(page: Page, config: Config, context: str) -> tuple[list[str], bool, str]:
    """Who wrote a linked page. Never falls back to the person who shared it: someone on People is
    only credited when the page itself shows them (its byline, title or opening, or their profile
    in its address), so a page someone shared with comments of their own isn't taken for theirs."""
    who = detect(page.text, config=config, submitter="", context=context)
    names = [a for a in who.authors if a.strip()] or ([page.author] if page.author else [])
    reason = who.reason
    unshown = [n for n in names if (p := config.find_person(n)) and not on_page(p, page)]
    if unshown:
        names = [n for n in names if n not in unshown] or ([page.author] if page.author and page.author not in unshown else [])
        reason = (f"{reason} " if reason else "") + f"Not credited to {', '.join(unshown)}: the page doesn't show them."
    people = [config.find_person(n) for n in names]
    ours = bool(names) and all(people)
    return ([p.name for p in people] if ours else names), ours, reason


def on_page(person, page: Page) -> bool:
    """Whether a page shows this person as its author: their name in its byline, title or
    opening, or their name in its address (a LinkedIn profile or post, say)."""
    shown = " ".join([page.author or "", page.title or "", (page.text or "")[:3000]]).lower()
    address = (page.final_url or page.url or "").lower()
    for name in [person.name, *person.aliases]:
        name = name.strip().lower()
        if not name:
            continue
        if name in shown or name.replace(" ", "") in address or name.replace(" ", "-") in address:
            return True
    return False


def read_links(
    shared: list[tuple[str, str, datetime]],  # (message text, who shared it, when)
    *,
    via: str,
    place: str,
    row_prefix: str,
    config: Config,
    store: Store,
    skip_hosts: tuple[str, ...] = (),
    skip_saved: bool = False,
) -> list[Linked]:
    """Fetches the pages linked in the messages and works out who wrote each one. With
    skip_saved, pages already saved as items are left out rather than read again."""
    seen, found = set(), []
    for text, sharer, when in shared:
        for url in find_urls(text):
            host = urlparse(url).hostname or ""
            if url in seen or len(seen) >= LINKS_PER_THREAD or any(host.endswith(h) for h in skip_hosts):
                continue
            seen.add(url)
            row_id = f"{row_prefix}-{hashlib.sha1(url.encode()).hexdigest()[:12]}"
            if skip_saved and store.get(row_id):
                continue
            if internal(url):
                found.append(_read_internal(url, sharer, when, text, row_id, place, via, config, store))
                continue
            try:
                page = fetch_link(url, config)
                if sign_in_page(page):
                    raise ValueError("it asks to sign in")
            except Exception as exc:  # noqa: BLE001
                events.problem(store, f"Couldn't read the link {url} shared in {place} by {sharer}: {exc}",
                               via=via, key=row_id, url=url)
                continue
            context = f"{page.context()}\n\nShared in {place} by {sharer}, who may not be its author.\nMessage: {text}"
            authors, ours, reason = _authors(page, config, context)
            found.append(Linked(url, page, sharer, when, text, row_id, authors, ours, reason))
    return found


def _read_internal(url: str, sharer: str, when: datetime, text: str, row_id: str, place: str, via: str,
                   config: Config, store: Store) -> Linked:
    """One of the company's own docs: a Drive file through the Drive API, anything else (a
    Granola share page) as a web page. Never fails: what can't be opened is a bare link."""
    from . import drive_sync

    bare = Page(url=url, final_url=url, text="", site=urlparse(url).hostname or "")
    linked = Linked(url, bare, sharer, when, text, row_id, [], False, internal=True)
    fid = drive_sync.file_id(url) if "google.com" in url else None
    saved = store.get(row_id) or (store.find_by_drive_file_id(fid) if fid else None)
    if saved:  # read before: reuse it rather than reading it again
        linked.existing, linked.authors, linked.drive_file_id = saved.row_id, saved.authors, fid or ""
        linked.page = Page(url=url, final_url=saved.source_metadata.get("url") or url, text=saved.extracted_text,
                           title=str(saved.source_metadata.get("label") or ""), site=bare.site)
        return linked
    if fid:
        linked.drive_file_id = fid
        try:
            read = drive_sync.read_file(fid, config)
        except Exception as exc:  # noqa: BLE001
            log.warning("Couldn't read Drive file %s: %s", fid, exc)
            read = None
        if read is None:
            linked.unread = True
            events.record(store, "link_unread", f"Couldn't open the Drive file {url} shared in {place} by {sharer}. "
                          "Share it (or its folder) with the service account to have it read.",
                          level="warning", via=via, key=f"unread:{fid}")
            return linked
        file, body = read
        owner = drive_sync.owner(file)
        page = Page(url=url, final_url=file.get("webViewLink") or url, text=body, title=file.get("name", ""),
                    site="Google Drive", date=datetime.fromisoformat(file["createdTime"].replace("Z", "+00:00"))
                    if file.get("createdTime") else None, method="drive")
        linked.extra = {"file_name": file.get("name"), "mime_type": file.get("mimeType")}
    else:
        owner = sharer
        try:
            page = fetch_link(url, config)
        except Exception as exc:  # noqa: BLE001
            log.warning("Couldn't read %s: %s", url, exc)
            page = None
        if page is None or sign_in_page(page) or not page.text.strip():
            linked.unread = True
            return linked
    linked.page = page
    linked.who = attribute(page.text, config=config, submitter=owner, source_types=INTERNAL_TYPES,
                           context=f"{page.context()}\n\nShared in {place} by {sharer}.\nMessage: {text}")
    linked.authors = linked.who.authors
    linked.reason = linked.who.reason
    linked.extra["submitted_by"] = owner
    return linked


def _save_internal(link: Linked, *, via: str, place: str, config: Config, store: Store) -> None:
    """Stores one of the company's own docs once, as a meeting or document of its own."""
    if link.existing or link.unread or store.get(link.row_id) or (
            link.drive_file_id and store.find_by_drive_file_id(link.drive_file_id)):
        return
    who = link.who or Attribution(link.authors or [link.sharer])
    ingest_text(
        link.page.text, config=config, store=store, row_id=link.row_id, via=via,
        source_type=who.source_type or "document", authors=who.authors or [link.sharer],
        date_created=who.date or link.page.date or link.when, drive_file_id=link.drive_file_id or None,
        note=f"Shared in {place} by {link.sharer}" + (f": {link.message}" if link.message.strip() else ""),
        source_metadata={**link.page.metadata(), "label": link.page.title or link.page.site or place,
                         **{k: v for k, v in link.extra.items() if k != "submitted_by" and v},
                         **attribution_metadata(who, link.extra.get("submitted_by") or link.sharer)},
    )
    log.info("Saved %s from %s as its own item", link.url, place)


def _save_page(link: Linked, *, reading: bool, via: str, place: str, metadata: dict, config: Config,
               store: Store) -> None:
    if store.get(link.row_id):
        return
    who = {"submitted_by": link.sharer, "author_source": "llm" if link.reason else "page",
           **({"author_reason": link.reason} if link.reason else {})}
    ingest_text(
        link.page.text, config=config, store=store, row_id=link.row_id, via=via, reading=reading,
        source_type=default_source_type(link.page.final_url), authors=link.authors or [link.page.site or "Unknown"],
        date_created=link.page.date or link.when,
        note=f"Shared in {place} by {link.sharer}" + (f": {link.message}" if link.message.strip() else ""),
        source_metadata={**link.page.metadata(), **metadata, "label": link.page.title or link.page.site or place,
                         **who},
    )
    log.info("Saved %s %s from %s", "reading" if reading else "page", link.url, place)


def save_pages(links: list[Linked], *, commentary: bool, via: str, place: str, metadata: dict, config: Config,
               store: Store) -> None:
    """Pages written by People are saved as theirs. With no commentary to keep instead, the
    rest are saved as reading."""
    for link in links:
        if link.internal:
            _save_internal(link, via=via, place=place, config=config, store=store)
        elif link.ours or not commentary:
            _save_page(link, reading=not link.ours, via=via, place=place, metadata=metadata, config=config,
                       store=store)


def link_metadata(links: list[Linked]) -> dict:
    """What the item with the commentary records about the pages it shares."""
    return {"links": [link.context() for link in links]} if links else {}
