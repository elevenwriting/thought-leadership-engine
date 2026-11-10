"""Saving a chat channel as sessions, for any chat source (ClickUp and Slack).

A source hands over a `Channel`: how to read its top-level messages since a time, how to fetch a
message's thread replies, a message's link, and what to call things. Everything else is the same
for every source:

- the channel is read from its Starting from date (or from when its mode last changed), a batch
  of sessions per run, oldest first, and carries on where the last run stopped;
- a session is saved once the channel has been quiet for the gap, as one item, and its topics
  (and views, if the channel asks for them) are found;
- a saved session whose threads gain replies within the look-back days is rebuilt;
- a channel removed and added again is read from the start.

Progress is kept in the store under `<source>-sessions:<channel id>`: `cursor` is the last
top-level message in a saved session, and `recent` lists the sessions saved in the last few days
with each message's reply count, so new replies are noticed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import events, topics, views
from ..config import Config
from ..ingest import ingest_text, refresh_text
from ..store import Store
from . import sessions

log = logging.getLogger(__name__)


@dataclass
class Channel:
    source: str                      # "clickup" or "slack": the items' source type and way in
    title: str                       # "ClickUp" or "Slack", for where things were said
    id: str
    name: str
    rules: dict
    # Top-level messages posted at or after a time, oldest first, without their replies.
    messages: Callable[[datetime], list[sessions.Message]]
    # The same message with its thread replies.
    with_replies: Callable[[sessions.Message], sessions.Message]
    url: Callable[[sessions.Message], str]
    # Reads the links in (text, who, when) messages. True when they're only links (saved as
    # reading), so there's nothing of the team's own to keep; otherwise adds them to `metadata`.
    links_only: Callable[[list, str, dict], bool]
    channel_key: str                 # the metadata key holding the channel's ID
    on_session: Callable[[list[sessions.Message]], None] | None = None  # e.g. saving shared files

    @property
    def place(self) -> str:
        return f"{self.source}:{self.id}"

    def row_id(self, first: sessions.Message) -> str:
        return f"{self.source}-session-{self.id}-{first.id}".replace(".", "")


def start(rules: dict) -> datetime | None:
    """The earliest a message can be and still be saved: the Starting from date, or the moment
    the channel's mode last changed, whichever is later."""
    starts = []
    if rules.get("from"):
        starts.append(datetime.fromisoformat(str(rules["from"])).replace(tzinfo=timezone.utc))
    if rules.get("since"):
        since = datetime.fromisoformat(str(rules["since"]))
        starts.append(since if since.tzinfo else since.replace(tzinfo=timezone.utc))
    return max(starts) if starts else None


def days(channel: dict, fallback: int) -> int:
    """How long after a message a new reaction or reply still counts."""
    try:
        return int(float(channel.get("look_back_days") or fallback))
    except (TypeError, ValueError):
        return fallback


def save(channel: Channel, look_back: int, config: Config, store: Store, limit: int) -> int:
    """Saves the channel's sessions that have ended since the last one saved, oldest first and at
    most `limit` at a time, and rebuilds recent ones whose threads have new replies. Returns how
    many sessions were saved or rebuilt."""
    rules = channel.rules
    key = f"{channel.source}-sessions:{channel.id}"
    state = store.get_state(key) or {}
    if state and state.get("added") != rules.get("added"):
        # The channel was removed and added again: start over from its Starting from date. Sessions
        # saved before are found by their row IDs, so none is saved twice.
        log.info("Channel %s was added again; reading it from the start", channel.id)
        state = {}
    state["added"] = rules.get("added")
    now = datetime.now(timezone.utc)
    begin = start(rules)
    if begin is None:  # no Starting from date: from the first sync on
        begin = datetime.fromisoformat(state.get("start") or now.isoformat())
        state["start"] = begin.isoformat()
    cursor = datetime.fromisoformat(state["cursor"]) if state.get("cursor") else None
    recent = [r for r in state.get("recent") or []
              if datetime.fromisoformat(r["last"]) > now - timedelta(days=look_back)]
    new_from = max(begin, cursor) if cursor else begin
    read_from = max(min([new_from, *(datetime.fromisoformat(r["first"]) for r in recent)]), begin)

    read = channel.messages(read_from)
    by_id = {m.id: m for m in read}
    done = 0

    # Saved sessions whose threads have new replies are rebuilt, and ones whose topics or views
    # couldn't be found are tried again.
    for entry in recent:
        saved = store.get(entry["row_id"]) if entry.get("row_id") else None
        if saved and saved.source_metadata.get("topics_error"):
            find_topics(saved, channel.source, config, store)
        elif saved and saved.source_metadata.get("views_error"):
            pull_views(saved, channel.source, config, store)
        counts = entry.get("replies") or {}
        found = [by_id.get(i) for i in entry["messages"]]
        if any(m is None for m in found):
            continue  # deleted messages, or older than this read: leave the session as it is
        if all(m.reply_count == counts.get(m.id, 0) for m in found):
            continue
        session = [channel.with_replies(m) for m in found]
        if session_item(session, channel, config, store) is not None:
            done += 1
        entry["replies"] = {m.id: m.reply_count for m in session}

    # New sessions, once they've ended.
    fresh = [m for m in read if not cursor or m.when > cursor]
    quiet = sessions.gap(rules.get("gap_hours"))
    ended = [s for s in sessions.split(fresh, quiet) if sessions.closed(s, quiet, now)]
    for session in ended[:limit]:
        session = [channel.with_replies(m) for m in session]
        if session_item(session, channel, config, store) is not None:
            done += 1
        state["cursor"] = session[-1].when.isoformat()
        recent.append({"row_id": channel.row_id(session[0]), "first": session[0].when.isoformat(),
                       "last": session[-1].when.isoformat(), "messages": [m.id for m in session],
                       "replies": {m.id: m.reply_count for m in session}})
    state["recent"] = recent
    store.set_state(key, state)
    return done


def session_item(session: list[sessions.Message], channel: Channel, config: Config, store: Store) -> int | None:
    """Saves a session, or rebuilds it if it was saved before. Returns how many messages it holds,
    or None when there was nothing of the team's own to keep (only links, saved as reading)."""
    if channel.on_session:
        channel.on_session(session)
    text, listed = sessions.transcript(session)
    if not text:
        return None
    first = session[0]
    metadata = {
        "label": f"#{channel.name}", "title": sessions.title(channel.name, session), "place": channel.place,
        channel.channel_key: channel.id,
        "channel_name": channel.name, "session": True, "message_count": len(listed), "messages": listed,
        "url": channel.url(first),
        "first_message": first.when.isoformat(), "last_message": session[-1].when.isoformat(),
        "views": bool(channel.rules.get("views")),
    }
    row_id = channel.row_id(first)
    existing = store.get(row_id)
    if existing and existing.extracted_text == text:
        return len(listed)
    if channel.rules.get("include_links", True):
        shared = [(m.text, m.who, m.when) for message in session for m in [message, *message.replies]]
        if channel.links_only(shared, f"{channel.title} #{channel.name}", metadata):
            return None
    authors = sessions.authors(session)
    if existing:
        # Its topics stay: they list top-level messages, and a new reply goes with its message.
        metadata.pop("title")
        refresh_text(existing, text, config=config, store=store, authors=authors, source_metadata=metadata,
                     via=channel.source)
        log.info("Updated %s session %s", channel.title, row_id)
    else:
        item = ingest_text(text, config=config, store=store, row_id=row_id, source_type=channel.source,
                           via=channel.source, authors=authors, visibility=channel.rules.get("visibility") or None,
                           date_created=first.when, source_metadata=metadata)
        log.info("Saved %s session %s (%d messages)", channel.title, row_id, len(listed))
        find_topics(item, channel.source, config, store)
    return len(listed)


def find_topics(item, via: str, config: Config, store: Store) -> None:
    """Finds a session's topics, then its views if the channel asks for them. A failure keeps the
    session, notes the error on it, and is tried again on later syncs."""
    try:
        topics.find(item, config, store)
    except Exception as exc:  # noqa: BLE001
        log.exception("Finding topics failed for %s", item.row_id)
        _failed(item, "topics_error", f"Couldn't find the topics in {item.source_metadata.get('title')}", exc,
                via, store)
        return
    if views.wanted(item):
        pull_views(item, via, config, store)


def pull_views(item, via: str, config: Config, store: Store) -> None:
    try:
        views.pull(store.get(item.row_id) or item, config, store)
    except Exception as exc:  # noqa: BLE001
        log.exception("Pulling out views failed for %s", item.row_id)
        _failed(item, "views_error", f"Couldn't pull out the views in {item.source_metadata.get('title')}", exc,
                via, store)


def _failed(item, key: str, message: str, exc: Exception, via: str, store: Store) -> None:
    current = store.get(item.row_id) or item
    store.update(item.row_id, {"source_metadata": {**current.source_metadata, key: str(exc)[:500]}})
    events.problem(store, f"{message}: {exc}", via=via, key=f"{key}:{item.row_id}", row_id=item.row_id)
