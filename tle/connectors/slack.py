# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Slack: the channels picked on Sources, each saved in one of two ways, exactly as ClickUp Chat.

- `sessions` (every message): a scheduled job reads the channel's history and saves it as
  sessions, split wherever the channel went quiet, with topics, chains and optional views. See
  channel_sessions.py, which ClickUp uses too. The retired `all` mode is read as sessions.
- `reaction`: the bot saves a message someone reacts to with the channel's emoji, as it
  happens. With include_threads on, a thread is saved as one item: the parent message and all
  its replies, in order, with each speaker named. Reacting to any message in a thread saves the
  whole thread, and another reaction rebuilds it.

Files shared in those messages (documents, PDFs, images, voice clips) are saved as items
of their own, with the message they were posted with as a note. Turn this off with
include_files: false.

Links work the same way (include_links): each linked page, such as a LinkedIn post or a news
article, is read and saved as its own item, credited to whoever wrote it, with the message
as a note. The conversation is saved too, unless it's one message that's little more than
the link and a line about it.
"""

from __future__ import annotations

import logging
import os
import re
import ssl
from datetime import datetime, timezone

import certifi
import httpx
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

from ..attribution import attribute, parse_fields
from ..attribution import metadata as attribution_metadata
from .. import events
from ..config import Config
from ..emoji import CHAT as CHAT_EMOJI
from ..extract import extract_text, guess_mime
from ..ingest import ingest_text, refresh_text
from ..links import find_urls
from ..settings_form import CHANNEL_DEFAULTS
from . import channel_sessions, sessions
from .shared import has_commentary, link_metadata, read_links, save_pages
from ..store import Store

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 25 * 1024 * 1024
# Sessions saved per channel per run, so reading a long history never runs past the job's time limit.
SESSIONS_PER_SYNC = 20
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|[^>]*)?>")
EMOJI_CODE = re.compile(r":([a-z0-9_+-]+):")


def verify(body: bytes, headers: dict) -> bool:
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    return bool(secret) and SignatureVerifier(secret).is_valid_request(body, headers)


def _client() -> WebClient:
    # certifi's certificates, as httpx uses: some Python installs (python.org's on macOS) have none of their own.
    return WebClient(token=os.environ["SLACK_BOT_TOKEN"], ssl=ssl.create_default_context(cafile=certifi.where()))


def list_channels() -> list[dict]:
    """Channels the bot can see, for picking in Settings: every public one, and the private
    ones it's been invited to."""
    channels, cursor = [], None
    client = _client()
    while True:
        page = client.conversations_list(types="public_channel,private_channel", exclude_archived=True,
                                         limit=1000, cursor=cursor)
        channels += [{"id": c["id"], "name": c.get("name", c["id"]), "private": bool(c.get("is_private")),
                      "member": bool(c.get("is_member"))} for c in page.get("channels", [])]
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return sorted(channels, key=lambda c: c["name"])


def join_channel(channel: str) -> None:
    """Adds the bot to a public channel, so it receives the channel's events."""
    _client().conversations_join(channel=channel)


def _when(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _is_reply(message: dict) -> bool:
    return bool(message.get("thread_ts")) and message["thread_ts"] != message.get("ts")


def _fetch_thread(client: WebClient, channel: str, thread_ts: str) -> list[dict]:
    """The parent and every reply, oldest first. `ts` may be any message in the thread."""
    messages, cursor = [], None
    while True:
        page = client.conversations_replies(channel=channel, ts=thread_ts, limit=200, cursor=cursor)
        messages += page["messages"]
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return messages


def _fetch_message(client: WebClient, channel: str, ts: str) -> dict | None:
    history = client.conversations_history(channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
    if history["messages"]:
        return history["messages"][0]
    # Thread replies don't appear in channel history.
    return next((m for m in _fetch_thread(client, channel, ts) if m.get("ts") == ts), None)


class _Names:
    """Slack user ID -> the person's name: from People by Slack ID, then by the email on their
    Slack profile (with the users:read.email permission), then their Slack name."""

    def __init__(self, client: WebClient, config: Config):
        self.client, self.config, self.cache = client, config, {}

    def __call__(self, user_id: str) -> str:
        if user_id not in self.cache:
            person = self.config.person_by_slack_id(user_id)
            if person:
                self.cache[user_id] = person.name
            else:
                user = self.client.users_info(user=user_id)["user"]
                email = (user.get("profile") or {}).get("email") or ""
                person = self.config.person_by_email(email) if email else None
                # People who've left keep their name only in their profile.
                profile = user.get("profile") or {}
                self.cache[user_id] = person.name if person else (
                    user.get("real_name") or profile.get("real_name") or profile.get("display_name")
                    or user.get("name") or user_id)
        return self.cache[user_id]


    def text(self, text: str) -> str:
        """Slack's markup as plain text: mentions as names, links as addresses, emoji codes as emoji,
        and the &amp; &lt; &gt; Slack escapes back to & < >."""
        text = SLACK_LINK.sub(lambda m: m.group(1), MENTION.sub(lambda m: "@" + self(m.group(1)), text))
        text = EMOJI_CODE.sub(lambda m: CHAT_EMOJI.get(m.group(1), m.group(0)), text)
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

def member_id(email: str) -> str | None:
    """A person's Slack member ID from their email, or None if Slack doesn't know it (or the app
    lacks the users:read.email permission)."""
    from slack_sdk.errors import SlackApiError

    try:
        return _client().users_lookupByEmail(email=email)["user"]["id"]
    except SlackApiError as exc:
        error = (exc.response.data or {}).get("error") if exc.response is not None else None
        if error != "users_not_found":
            log.warning("Couldn't look up a Slack member by email: %s", error or exc)
        return None


def scopes() -> set[str]:
    """The permissions the bot token has."""
    response = _client().auth_test()
    return {s.strip() for s in (response.headers.get("x-oauth-scopes") or "").split(",") if s.strip()}


def _download_file(file: dict) -> bytes:
    response = httpx.get(file["url_private_download"], timeout=60, follow_redirects=True,
                         headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"})
    response.raise_for_status()
    if len(response.content) > MAX_FILE_BYTES:
        raise ValueError("File is larger than 25 MB.")
    return response.content


def channel_settings(config: Config, channel: str | None) -> dict | None:
    """The rules for one channel, or None if it isn't set up. Each entry under `channels`
    has its own rules; the older `allowed_channels` list shares the section's."""
    settings = config.sources.get("slack", {})
    shared = {k: settings.get(k, v) for k, v in CHANNEL_DEFAULTS.items()}
    for entry in settings.get("channels") or []:
        if entry.get("id") == channel:
            return {**CHANNEL_DEFAULTS, **{k: v for k, v in entry.items() if v is not None}}
    if channel in (settings.get("allowed_channels") or []):
        return shared
    return None


def _save_files(files: list[tuple[dict, dict]], channel: str, channel_name: str, names: "_Names",
                config: Config, store: Store) -> None:
    """Each file shared in the message or thread becomes its own item. The message it was
    posted with is the note, and the author is worked out from the file (the uploader if
    nothing points elsewhere)."""
    for message, file in files:
        row_id = f"slack-file-{file['id']}"
        if store.get(row_id):
            continue
        filename = file.get("name") or file["id"]
        try:
            text = extract_text(_download_file(file), filename, config, file.get("mimetype"))
        except Exception as exc:  # noqa: BLE001
            events.problem(store, f"Couldn't read Slack file {filename} in #{channel_name}: {exc}", via="slack",
                           url=file.get("permalink"))
            continue
        fields, text = parse_fields(text)
        uploader = names(message["user"])
        note = names.text(message.get("text", ""))
        who = attribute(text, config=config, submitter=uploader, fields=fields,
                        context=f"Shared in Slack #{channel_name} by {uploader}\nFile: {filename}\nMessage: {note}")
        mime = guess_mime(filename, file.get("mimetype"))
        ingest_text(
            text, config=config, store=store, row_id=row_id, via="slack",
            source_type="voicenote" if mime.startswith("audio/") or file.get("subtype") == "slack_audio" else "document",
            authors=who.authors, date_created=_when(message["ts"]), note=note,
            source_metadata={
                "channel_id": channel, "channel_name": channel_name, "label": f"#{channel_name}",
                "file_name": filename, "mime_type": mime, "url": file.get("permalink"),
                **attribution_metadata(who, uploader),
            },
        )
        log.info("Saved Slack file %s", filename)


def handle_event(payload: dict, config: Config, store: Store) -> None:
    event = payload.get("event", {})
    channel = (event.get("item") or {}).get("channel") if event.get("type") == "reaction_added" else event.get("channel")
    # Only automate from channels you've explicitly marked safe.
    settings = channel_settings(config, channel)
    if settings is None:
        return
    mode, include_threads = settings["mode"], settings["include_threads"]

    if mode == "reaction" and event.get("type") == "reaction_added":
        if event.get("reaction") != settings.get("reaction", "round_pushpin"):
            return
        channel, ts = event["item"].get("channel"), event["item"].get("ts")
        message = None
    else:
        return  # every message is read from the channel's history by the scheduled job, as sessions

    client = _client()
    message = message or _fetch_message(client, channel, ts)
    if not message:
        return

    in_thread = include_threads and (_is_reply(message) or message.get("reply_count"))
    if in_thread:
        root_ts = message.get("thread_ts") or message["ts"]
        messages = _fetch_thread(client, channel, root_ts)
    else:
        root_ts = message["ts"]
        messages = [message]
    messages = [m for m in messages if m.get("user") and not m.get("bot_id")]
    files = [(m, f) for m in messages for f in m.get("files", []) if f.get("url_private_download")]
    messages = [m for m in messages if m.get("text")]
    if not messages and not files:
        return

    names = _Names(client, config)
    channel_name = client.conversations_info(channel=channel)["channel"].get("name", channel)
    if files and settings["include_files"]:
        _save_files(files, channel, channel_name, names, config, store)
    if not messages:
        return

    if len(messages) == 1:
        text = names.text(messages[0]["text"])
    else:
        text = "\n\n".join(
            f"{names(m['user'])} ({_when(m['ts']).strftime('%d.%m.%Y %H:%M')}): {names.text(m['text'])}"
            for m in messages
        )
    authors = [names(m["user"]) for m in messages]

    metadata = {
        "channel_id": channel,
        "channel_name": channel_name,
        "label": f"#{channel_name}",
        "url": client.chat_getPermalink(channel=channel, message_ts=root_ts).get("permalink"),
        "thread": len(messages) > 1,
        "message_count": len(messages),
    }

    row_id = f"slack-{channel}-{root_ts}".replace(".", "")
    existing = store.get(row_id)
    if existing and existing.extracted_text == text:
        return
    commentary = has_commentary([names.text(m["text"]) for m in messages])
    if settings["include_links"]:
        place = f"Slack #{channel_name}"
        shared = [(names.text(m["text"]), names(m["user"]), _when(m["ts"])) for m in messages]
        links = read_links(shared, via="slack", place=place, row_prefix=f"slack-link-{channel}", config=config,
                           store=store, skip_saved=not commentary)
        save_pages(links, commentary=commentary, via="slack", place=place, config=config, store=store,
                   metadata={"channel_id": channel, "channel_name": channel_name})
        if not commentary and (links or SLACK_LINK.search(" ".join(m["text"] for m in messages))):
            return  # just links: saved as reading (or as theirs), with nothing of our own to keep
        metadata.update(link_metadata(links))

    if existing:
        refresh_text(existing, text, config=config, store=store, authors=authors, source_metadata=metadata,
                     via="slack")
        log.info("Updated Slack item %s (%d messages)", row_id, len(messages))
        return

    ingest_text(
        text, config=config, store=store, row_id=row_id, source_type="slack", authors=authors, via="slack",
        date_created=_when(root_ts), source_metadata=metadata,
    )
    log.info("Saved Slack item %s (%d messages)", row_id, len(messages))


# --- Every message, as sessions (the scheduled job) ---------------------------------

def _person(raw: dict) -> bool:
    """A message a person wrote: not a bot's, a join or a channel event (those arrive as subtypes)."""
    return bool(raw.get("user")) and not raw.get("bot_id") and raw.get("subtype") in (None, "file_share", "thread_broadcast")


def _top_level(raw: dict) -> bool:
    return _person(raw) and not _is_reply(raw)


def _history(client: WebClient, channel: str, since: datetime) -> list[dict]:
    """The channel's top-level messages posted at or after `since`, oldest first."""
    found, cursor = [], None
    while True:
        page = client.conversations_history(channel=channel, oldest=f"{since.timestamp():.6f}", limit=200,
                                            cursor=cursor)
        found += [m for m in page["messages"] if _top_level(m)]
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return sorted(found, key=lambda m: float(m["ts"]))


def _chat_channel(client: WebClient, entry: dict, rules: dict, names: "_Names", config: Config,
                  store: Store) -> channel_sessions.Channel:
    """A Slack channel, for saving as sessions (see channel_sessions.py)."""
    channel = entry["id"]
    name = entry.get("name") or client.conversations_info(channel=channel)["channel"].get("name", channel)
    raws: dict[str, dict] = {}

    def message(raw: dict, replies: list[sessions.Message] | None = None) -> sessions.Message:
        raws[raw["ts"]] = raw
        return sessions.Message(id=raw["ts"], who=names(raw["user"]), when=_when(raw["ts"]),
                                text=names.text(raw.get("text") or "").strip(), replies=replies or [],
                                reply_count=int(raw.get("reply_count") or 0))

    def messages(since: datetime) -> list[sessions.Message]:
        return [message(m) for m in _history(client, channel, since)]

    def with_replies(top: sessions.Message) -> sessions.Message:
        raw = raws[top.id]
        replies = []
        if raw.get("reply_count"):
            replies = [message(r) for r in _fetch_thread(client, channel, raw["ts"]) if r["ts"] != raw["ts"] and _person(r)]
        return message(raw, replies)

    def url(first: sessions.Message) -> str:
        try:
            return client.chat_getPermalink(channel=channel, message_ts=first.id).get("permalink") or ""
        except Exception:  # noqa: BLE001
            return ""

    def links_only(shared: list, place: str, metadata: dict) -> bool:
        commentary = has_commentary([text for text, _, _ in shared])
        links = read_links(shared, via="slack", place=place, row_prefix=f"slack-link-{channel}", config=config,
                           store=store, skip_saved=not commentary)
        save_pages(links, commentary=commentary, via="slack", place=place, config=config, store=store,
                   metadata={"channel_id": channel, "channel_name": name})
        if not commentary and any(find_urls(text) for text, _, _ in shared):
            return True
        metadata.update(link_metadata(links))
        return False

    def files(session: list[sessions.Message]) -> None:
        if not rules.get("include_files", True):
            return
        shared = [(raws[m.id], f) for top in session for m in [top, *top.replies]
                  for f in raws[m.id].get("files", []) if f.get("url_private_download")]
        if shared:
            _save_files(shared, channel, name, names, config, store)

    return channel_sessions.Channel(
        source="slack", title="Slack", id=channel, name=name, rules=rules, messages=messages,
        with_replies=with_replies, url=url, links_only=links_only, channel_key="channel_id", on_session=files)


def sync(config: Config, store: Store, client: WebClient | None = None) -> dict[str, int]:
    """Reads every channel set to save every message, as sessions. Reacted channels are saved as
    reactions happen, so there's nothing to read for them here."""
    entries = [e for e in config.sources.get("slack", {}).get("channels") or []
               if e.get("id") and (e.get("mode") or "reaction") in ("sessions", "all")]
    done = {"channels": 0, "sessions": 0}
    if not entries:
        return done
    client = client or _client()
    names = _Names(client, config)
    for entry in entries:
        rules = {**CHANNEL_DEFAULTS, **{k: v for k, v in entry.items() if v is not None}}
        try:
            done["sessions"] += channel_sessions.save(
                _chat_channel(client, entry, rules, names, config, store),
                channel_sessions.days(entry, 7), config, store, SESSIONS_PER_SYNC)
            done["channels"] += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("Couldn't read Slack channel %s", entry["id"])
            events.problem(store, f"Couldn't read Slack channel #{entry.get('name') or entry['id']}: {exc}",
                           via="slack", key=f"channel:{entry['id']}")
    return done
