"""What chat-style connectors (Slack, ClickUp) share: saving the pages linked in a conversation."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from urllib.parse import urlparse

from .. import events
from ..attribution import attribute
from ..attribution import metadata as attribution_metadata
from ..config import Config
from ..ingest import ingest_text
from ..links import URL, default_source_type, find_urls
from ..links import fetch as fetch_link
from ..store import Store

log = logging.getLogger(__name__)

# A message with no more than this beside its links is a note about them, not saved on its own.
NOTE_MAX_CHARS = 500
LINKS_PER_THREAD = 5


def mostly_links(text: str) -> bool:
    return len(URL.sub("", text).strip()) <= NOTE_MAX_CHARS


def save_links(
    shared: list[tuple[str, str, datetime]],  # (message text, who shared it, when)
    *,
    via: str,
    place: str,
    row_prefix: str,
    metadata: dict,
    config: Config,
    store: Store,
    skip_hosts: tuple[str, ...] = (),
) -> int:
    """Each page linked in the messages becomes its own item, credited to whoever wrote it, with
    the message as its note. Returns how many of them are now saved, so a caller can tell
    whether a message that was just a link has been kept."""
    seen, stored = set(), 0
    for text, sharer, when in shared:
        for url in find_urls(text):
            host = urlparse(url).hostname or ""
            if url in seen or len(seen) >= LINKS_PER_THREAD or any(host.endswith(h) for h in skip_hosts):
                continue
            seen.add(url)
            row_id = f"{row_prefix}-{hashlib.sha1(url.encode()).hexdigest()[:12]}"
            if store.get(row_id):
                stored += 1
                continue
            try:
                page = fetch_link(url, config)
            except Exception as exc:  # noqa: BLE001
                events.problem(store, f"Couldn't read the link {url} shared in {place} by {sharer}: {exc}",
                               via=via, key=row_id, url=url)
                continue
            context = f"{page.context()}\n\nShared in {place} by {sharer}\nMessage: {text}"
            who = attribute(page.text, config=config, submitter=sharer, context=context)
            ingest_text(
                page.text, config=config, store=store, row_id=row_id, via=via,
                source_type=default_source_type(page.final_url), authors=who.authors,
                date_created=who.date or page.date or when, note=text,
                source_metadata={**page.metadata(), **metadata, "label": page.site or page.title or place,
                                 **attribution_metadata(who, sharer)},
            )
            stored += 1
            log.info("Saved link %s from %s", url, place)
    return stored
