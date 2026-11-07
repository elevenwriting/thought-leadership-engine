"""Slack bot: saves messages from allowed channels, either every message or the ones
someone reacts to with the configured emoji.

With include_threads on, a thread is saved as one item: the parent message and all its
replies, in order, with each speaker named. Reacting to any message in a thread saves the
whole thread. When the thread grows (a new reply in `all` mode, or another reaction), the
item's text is rebuilt and tagged again.

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
from ..extract import extract_text, guess_mime
from ..ingest import ingest_text, refresh_text
from ..settings_form import CHANNEL_DEFAULTS
from .shared import mostly_links, save_links
from ..store import Store

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 25 * 1024 * 1024
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|[^>]*)?>")


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
    """Slack user ID -> the person's name, from People first, then Slack."""

    def __init__(self, client: WebClient, config: Config):
        self.client, self.config, self.cache = client, config, {}

    def __call__(self, user_id: str) -> str:
        if user_id not in self.cache:
            person = self.config.person_by_slack_id(user_id)
            if person:
                self.cache[user_id] = person.name
            else:
                profile = self.client.users_info(user=user_id)["user"]
                self.cache[user_id] = profile.get("real_name") or profile.get("name") or user_id
        return self.cache[user_id]

    def text(self, text: str) -> str:
        return SLACK_LINK.sub(lambda m: m.group(1), MENTION.sub(lambda m: "@" + self(m.group(1)), text))


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
    elif mode == "all" and event.get("type") == "message":
        # Edits, deletions, joins and bot posts all arrive as subtypes.
        if event.get("subtype") not in (None, "file_share", "thread_broadcast") or event.get("bot_id"):
            return
        if _is_reply(event) and not include_threads:
            return
        channel, ts, message = event.get("channel"), event.get("ts"), event
    else:
        return

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
    if settings["include_links"]:
        shared = [(names.text(m["text"]), names(m["user"]), _when(m["ts"])) for m in messages]
        saved = save_links(shared, via="slack", place=f"Slack #{channel_name}", row_prefix=f"slack-link-{channel}",
                           metadata={"channel_id": channel, "channel_name": channel_name}, config=config, store=store)
        # One message that's mostly the link is a note on the link's item, not saved again.
        if saved and len(messages) == 1 and mostly_links(SLACK_LINK.sub("", messages[0]["text"])):
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
