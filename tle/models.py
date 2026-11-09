"""The row: one captured source item (a Slack message, a transcript, a voice note...)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

PRIVATE_FLAG = (
    "PRIVATE: internal source. Use it to inform content, but don't quote it, "
    "name it or publish it without the author's approval."
)

PARAPHRASED_FLAG = (
    "PARAPHRASED: meeting notes, a summary written by a notetaker or an AI. The wording isn't the "
    "speakers' own: report it as \"according to the meeting notes\", never in quotation marks."
)

READING_FLAG = (
    "READING: someone else's content that the team shared without comment. It is not the company's "
    "view: don't cite or quote it as theirs."
)


def now() -> datetime:
    return datetime.now(timezone.utc)


class Item(BaseModel):
    row_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    date_created: datetime = Field(default_factory=now)
    date_ingested: datetime = Field(default_factory=now)
    source_type: str  # the kind of content: a LinkedIn post, a meeting, a document...
    via: str = ""     # the way in: slack, clickup, email, drive, web, api, mcp, cli
    reading: bool = False  # someone else's content shared by the team, not the company's own thinking
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    authors: list[str] = Field(default_factory=list)
    author_ids: list[str] = Field(default_factory=list)  # people table IDs, for authors who are in it
    drive_file_id: str | None = None
    extracted_text: str
    theme_tags: list[str] = Field(default_factory=list)
    narrative_tags: list[str] = Field(default_factory=list)
    summary: str = ""
    deliverables: list[str] = Field(default_factory=list)
    status: str = "new"
    visibility: str = "private"
    flags: list[str] = Field(default_factory=list)  # what needs a person to look at it; see review_flags

    def review_flags(self) -> list[str]:
        """Worked out from the metadata whenever an item is saved, so they can be filtered on."""
        meta = self.source_metadata
        flags = []
        if meta.get("tagging_error"):
            flags.append("tagging_failed")
        if not self.reading and (meta.get("author_needs_review") or not self.authors or self.authors == ["Unknown"]):
            flags.append("author_unclear")
        if meta.get("source_type_given"):
            flags.append("unknown_source")
        if meta.get("suggested_themes"):
            flags.append("theme_ideas")
        return flags

    @property
    def citation(self) -> str:
        """Source citation in the form 'J SMITH - SLACK #NEWS - 19.09.2026'."""
        who = ", ".join(self.authors).upper() or "UNKNOWN"
        where = self.source_type.upper()
        if self.source_metadata.get("record") == "notes":
            where += " NOTES"
        label = self.source_metadata.get("label")
        if label:
            where = f"{where} {str(label).upper()}"
        return f"{who} - {where} - {self.date_created.strftime('%d.%m.%Y')}"

    def to_result(self, include_text: bool = True, max_text_chars: int | None = None) -> dict[str, Any]:
        """Shape returned to API, MCP and skill callers. Private rows carry a flag."""
        data = self.model_dump(mode="json")
        # A chat session's message list is for the pipeline; its text already numbers them.
        data["source_metadata"].pop("messages", None)
        if not include_text:
            data.pop("extracted_text")
        elif max_text_chars and len(self.extracted_text) > max_text_chars:
            data["extracted_text"] = self.extracted_text[:max_text_chars]
            data["text_truncated"] = True
        data["citation"] = self.citation
        if self.reading:
            data["reading_flag"] = READING_FLAG
        if self.source_metadata.get("record") == "notes":
            data["paraphrased"] = True
            data["paraphrased_flag"] = PARAPHRASED_FLAG
        if self.visibility == "private":
            data["private_flag"] = PRIVATE_FLAG
        return data


FLAG_LABELS = {
    "tagging_failed": "Tagging failed",
    "author_unclear": "Author unclear",
    "unknown_source": "Unknown source type",
    "theme_ideas": "New theme ideas",
}


class Person(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    role: str = ""
    emails: list[str] = Field(default_factory=list)
    slack_id: str = ""
    aliases: list[str] = Field(default_factory=list)  # other spellings: "Jen", a maiden name
    responsible_for: list[str] = Field(default_factory=list)
    active: bool = True

    @classmethod
    def from_config(cls, raw: dict) -> "Person":
        """Settings and older data list one `email`; the table holds several."""
        raw = dict(raw)
        email = raw.pop("email", "")
        raw.setdefault("emails", [email] if email else [])
        return cls(**raw)

    @property
    def email(self) -> str:
        return self.emails[0] if self.emails else ""

    def matches(self, value: str) -> bool:
        value = value.strip().lower()
        return bool(value) and (
            value in {e.lower() for e in self.emails}
            or value == self.slack_id.lower()
            or value == self.name.lower()
            or value in {a.lower() for a in self.aliases}
        )


class Event(BaseModel):
    """Something that happened: an item saved or changed, a setting changed, or a problem."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    time: datetime = Field(default_factory=now)
    kind: str            # item_saved, item_updated, item_deleted, error, settings_changed, ...
    level: str = "info"  # info | warning | error
    via: str = ""        # web, api, slack, email, drive, mcp, cli, dashboard
    row_id: str | None = None
    actor: str = ""      # who did it, when known
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    read: bool = False
    key: str = ""        # the same problem again (a retried email, say) adds to this event's count
    count: int = 1
    last_time: datetime | None = None


class Topic(BaseModel):
    """A part of an item: one piece of work or one question in a chat session."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    row_id: str                  # the item it's part of
    place: str = ""              # where it was said, e.g. clickup:<channel id>, to find a place's recent topics
    title: str
    summary: str = ""
    messages: list[str] = Field(default_factory=list)  # top-level message IDs; their replies go with them
    people: list[str] = Field(default_factory=list)
    date_created: datetime       # the item's, so a place's topics sort by when they were said
    position: int = 0            # order within the item
    made_by: str = "model"       # model, or the person who corrected it


class Link(BaseModel):
    """"This is related to that", between topics and items. Ends are "topic:<id>" or "item:<row_id>"."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    from_id: str
    to_id: str
    kind: str = "continues"      # continues: the same piece of work picked up again | related
    reason: str = ""
    made_by: str = "model"
    created: datetime = Field(default_factory=now)


class View(BaseModel):
    """A position someone took in one topic of a chat session, credited to them and pointing at
    the messages where they said it."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    topic_id: str
    row_id: str                  # the session it's in
    person: str
    view: str                    # one or two sentences, close to their own words
    stance: str = "new"          # new | agrees | disagrees | builds_on
    responds_to: str = ""        # whose point it answers, if anyone's
    messages: list[str] = Field(default_factory=list)  # top-level message IDs
    made_by: str = "model"
    created: datetime = Field(default_factory=now)


class SettingsVersion(BaseModel):
    version: int
    saved_at: datetime = Field(default_factory=now)
    saved_by: str = ""
    note: str = ""
    data: dict[str, Any]
