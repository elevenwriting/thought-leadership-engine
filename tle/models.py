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


def now() -> datetime:
    return datetime.now(timezone.utc)


class Item(BaseModel):
    row_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    date_created: datetime = Field(default_factory=now)
    date_ingested: datetime = Field(default_factory=now)
    source_type: str
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
        if meta.get("author_needs_review") or not self.authors or self.authors == ["Unknown"]:
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
        label = self.source_metadata.get("label")
        if label:
            where = f"{where} {str(label).upper()}"
        return f"{who} - {where} - {self.date_created.strftime('%d.%m.%Y')}"

    def to_result(self, include_text: bool = True, max_text_chars: int | None = None) -> dict[str, Any]:
        """Shape returned to API, MCP and skill callers. Private rows carry a flag."""
        data = self.model_dump(mode="json")
        if not include_text:
            data.pop("extracted_text")
        elif max_text_chars and len(self.extracted_text) > max_text_chars:
            data["extracted_text"] = self.extracted_text[:max_text_chars]
            data["text_truncated"] = True
        data["citation"] = self.citation
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


class SettingsVersion(BaseModel):
    version: int
    saved_at: datetime = Field(default_factory=now)
    saved_by: str = ""
    note: str = ""
    data: dict[str, Any]
