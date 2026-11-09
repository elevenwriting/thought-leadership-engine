from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources

from ..models import Event, Item, Link, Person, SettingsVersion, Topic, View

UPDATABLE_FIELDS = {
    "status", "deliverables", "theme_tags", "narrative_tags", "visibility", "summary", "authors",
    "author_ids", "extracted_text", "source_metadata", "date_created", "source_type", "flags", "reading",
}
LIST_COLUMNS = {"theme_tags", "narrative_tags", "authors", "author_ids"}


@dataclass
class SearchQuery:
    """Filters combine with AND. List filters match rows that have any of the values."""

    text: str | None = None
    themes: list[str] = field(default_factory=list)
    narratives: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    author_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    flagged: bool = False  # only items with at least one flag
    source_types: list[str] = field(default_factory=list)
    vias: list[str] = field(default_factory=list)  # the ways in: slack, clickup, email...
    # The company's own items (False), reading only (True), or both (None).
    reading: bool | None = False
    visibility: str | None = None
    status: str | None = None
    statuses: list[str] = field(default_factory=list)  # any of these, alongside status
    date_from: datetime | None = None
    date_to: datetime | None = None
    ingested_after: datetime | None = None
    sort: str = "said"  # newest first by when it was said or written ("said"), or when it was saved ("added")
    limit: int = 20
    offset: int = 0


@dataclass
class TagRow:
    """The light slice of a row that coverage and trends are computed from."""

    date_created: datetime
    source_type: str
    via: str
    visibility: str
    authors: list[str]
    theme_tags: list[str]
    narrative_tags: list[str]
    deliverables: list[str]


@dataclass
class EventQuery:
    kinds: list[str] = field(default_factory=list)
    level: str | None = None
    via: str | None = None
    unread: bool = False
    row_id: str | None = None
    limit: int = 50
    offset: int = 0


def read_schema(name: str) -> str:
    return resources.files("tle.schema").joinpath(f"{name}.sql").read_text()


class Store(ABC):
    """Items, plus the dashboard's tables: events, people and versioned settings.

    Backends implement the underscored item methods; `insert` and `update` here keep each
    item's review flags in step with its metadata."""

    @abstractmethod
    def init(self) -> None:
        """Create the tables if they don't exist, and add columns that newer versions need."""

    # --- Items ---------------------------------------------------------------

    def insert(self, item: Item) -> None:
        item.flags = item.review_flags()
        self._insert(item)

    def update(self, row_id: str, fields: dict) -> Item | None:
        fields = self.check_fields(fields)
        if not fields:
            return self.get(row_id)
        if {"source_metadata", "authors", "reading"} & set(fields):
            current = self.get(row_id)
            if current is None:
                return None
            fields["flags"] = current.model_copy(update=fields).review_flags()
        return self._update(row_id, fields)

    @abstractmethod
    def _insert(self, item: Item) -> None: ...

    @abstractmethod
    def _update(self, row_id: str, fields: dict) -> Item | None: ...

    @abstractmethod
    def get(self, row_id: str) -> Item | None: ...

    @abstractmethod
    def delete(self, row_id: str) -> None: ...

    @abstractmethod
    def search(self, query: SearchQuery) -> list[Item]: ...

    @abstractmethod
    def find_by_drive_file_id(self, drive_file_id: str) -> Item | None: ...

    @abstractmethod
    def tag_rows(self, since: datetime | None = None) -> list[TagRow]:
        """The company's own items that aren't archived; reading is left out."""

    @abstractmethod
    def replace_in_lists(self, column: str, old: str, new: str | None) -> int:
        """Renames (or, with new=None, removes) a value in a list column on every item,
        e.g. a theme that was renamed. Returns how many items changed."""

    # --- Events --------------------------------------------------------------

    @abstractmethod
    def add_event(self, event: Event) -> None: ...

    @abstractmethod
    def repeat_event(self, key: str, time) -> bool:
        """Adds one to the count of the event with this key. False if there isn't one."""

    @abstractmethod
    def list_events(self, query: EventQuery) -> list[Event]: ...

    @abstractmethod
    def count_unread(self, level: str | None = None) -> int:
        """Unread events, or unread events at this level (error, warning)."""

    @abstractmethod
    def mark_read(self, ids: list[str] | None = None) -> None:
        """Marks these events read, or all of them when ids is None."""

    # --- People --------------------------------------------------------------

    @abstractmethod
    def list_people(self) -> list[Person]: ...

    @abstractmethod
    def save_person(self, person: Person) -> None:
        """Inserts or replaces by id."""

    @abstractmethod
    def delete_person(self, person_id: str) -> None: ...

    # --- Topics and links ----------------------------------------------------

    @abstractmethod
    def save_topics(self, row_id: str, topics: list[Topic]) -> None:
        """Replaces an item's topics. Links to and from the topics it had are removed."""

    @abstractmethod
    def save_topic(self, topic: Topic) -> None:
        """Saves one topic, replacing it if it exists. Its links are kept."""

    @abstractmethod
    def delete_topic(self, topic_id: str) -> None:
        """Removes one topic and the links from or to it."""

    @abstractmethod
    def topics_for(self, row_ids: list[str]) -> list[Topic]:
        """The topics of these items, in the order they were saved."""

    @abstractmethod
    def get_topics(self, ids: list[str]) -> list[Topic]: ...

    @abstractmethod
    def recent_topics(self, place: str, before, limit: int = 20) -> list[Topic]:
        """A place's latest topics from items created before `before`, newest first."""

    @abstractmethod
    def add_link(self, link: Link) -> None: ...

    @abstractmethod
    def links_for(self, ends: list[str]) -> list[Link]:
        """Links from or to any of these ends ("topic:<id>", "item:<row_id>")."""

    @abstractmethod
    def delete_link(self, link_id: str) -> None: ...

    @abstractmethod
    def save_views(self, topic_id: str, views: list[View]) -> None:
        """Replaces a topic's views."""

    @abstractmethod
    def views_for(self, row_ids: list[str]) -> list[View]:
        """The views in these items, in the order they were saved."""

    @abstractmethod
    def delete_links(self, ends: list[str]) -> None:
        """Removes every link from or to any of these ends."""

    # --- Sync state ----------------------------------------------------------

    @abstractmethod
    def get_state(self, key: str) -> dict | None:
        """Where a scheduled sync got to, e.g. "clickup-sessions:<channel id>"."""

    @abstractmethod
    def set_state(self, key: str, data: dict) -> None: ...

    # --- Settings ------------------------------------------------------------

    @abstractmethod
    def latest_settings(self) -> SettingsVersion | None: ...

    @abstractmethod
    def settings_history(self, limit: int = 50) -> list[SettingsVersion]: ...

    @abstractmethod
    def _add_settings(self, version: SettingsVersion) -> None: ...

    def save_settings(self, data: dict, saved_by: str = "", note: str = "") -> SettingsVersion:
        latest = self.latest_settings()
        version = SettingsVersion(version=(latest.version + 1) if latest else 1, data=data,
                                  saved_by=saved_by, note=note)
        self._add_settings(version)
        return version

    def get_settings_version(self, number: int) -> SettingsVersion | None:
        return next((v for v in self.settings_history(limit=1000) if v.version == number), None)

    @staticmethod
    def check_fields(fields: dict) -> dict:
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Can't update {', '.join(sorted(unknown))}")
        return {k: v for k, v in fields.items() if v is not None}

    @staticmethod
    def check_list_column(column: str) -> None:
        if column not in LIST_COLUMNS:
            raise ValueError(f"Can't replace values in {column}")
