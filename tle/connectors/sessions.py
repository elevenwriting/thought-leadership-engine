"""Chat sessions: a channel's messages, split wherever it went quiet, saved as one item each.

A session is a burst of activity: it ends when no top-level message has been posted for the
channel's gap (4 hours unless set). Thread replies stay with their message wherever they fall,
so a late reply never holds a session open. A session is stored like a meeting transcript: one
item, each top-level message numbered (#1, #2...) with its replies under it, and the messages
listed in the metadata so later steps can point at them.

This module knows nothing about ClickUp or Slack; connectors hand it plain messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

DEFAULT_GAP_HOURS = 4


@dataclass
class Message:
    id: str
    who: str
    when: datetime
    text: str
    replies: list["Message"] = field(default_factory=list)
    reply_count: int = 0  # as the source reports it, to notice new replies without fetching them


def gap(hours: float | None) -> timedelta:
    return timedelta(hours=float(hours or DEFAULT_GAP_HOURS))


def split(messages: list[Message], quiet: timedelta) -> list[list[Message]]:
    """Top-level messages, oldest first, split wherever the channel was quiet for longer than `quiet`."""
    sessions: list[list[Message]] = []
    for message in sorted(messages, key=lambda m: m.when):
        if sessions and message.when - sessions[-1][-1].when > quiet:
            sessions.append([])
        if not sessions:
            sessions.append([])
        sessions[-1].append(message)
    return sessions


def closed(session: list[Message], quiet: timedelta, now: datetime) -> bool:
    """Whether a session has ended: nothing new at the top level for longer than the gap."""
    return now - session[-1].when > quiet


def _stamp(when: datetime) -> str:
    return when.strftime("%d.%m.%Y %H:%M")


def transcript(session: list[Message]) -> tuple[str, list[dict]]:
    """The session's text, and its messages as metadata ({id, n, who, when, reply_to}).
    Messages with no text are left out of the text but numbered, so numbers match the source."""
    lines, listed = [], []
    for n, message in enumerate(session, 1):
        listed.append({"id": message.id, "n": n, "who": message.who, "when": message.when.isoformat(), "reply_to": None})
        if message.text.strip():
            lines.append(f"#{n} {message.who} ({_stamp(message.when)}): {message.text.strip()}")
        for reply in sorted(message.replies, key=lambda r: r.when):
            listed.append({"id": reply.id, "n": n, "who": reply.who, "when": reply.when.isoformat(), "reply_to": message.id})
            if reply.text.strip():
                lines.append(f"    ↳ {reply.who} ({_stamp(reply.when)}): {reply.text.strip()}")
    return "\n".join(lines), listed


def authors(session: list[Message]) -> list[str]:
    """Everyone who wrote in it, in order of first message. Unknown senders (bots, people who
    have left) are dropped when anyone else is known."""
    names = list(dict.fromkeys(m.who for message in session for m in [message, *message.replies] if m.text.strip()))
    known = [n for n in names if n and n != "Unknown"]
    return known or names


def title(channel: str, session: list[Message]) -> str:
    """What a session is called until its topics are known: the channel and the day."""
    return f"#{channel}, {session[0].when.strftime('%-d %b %Y')}"
