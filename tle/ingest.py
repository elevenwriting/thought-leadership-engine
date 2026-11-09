"""The shared pipeline every way in uses: extract, tag, apply governance, store."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import events, meetings
from .config import SOURCE_TYPES, VISIBILITIES, Config
from .extract import extract_text, guess_mime
from .models import Item, Link
from .store import Store
from .tagger import tag

log = logging.getLogger(__name__)


def resolve_people(values: list[str], config: Config) -> tuple[list[str], list[str]]:
    """Maps emails, Slack IDs and other spellings to each person's name, so one person is
    always one name. Returns the names and the IDs of those who are in the people list."""
    names, ids = [], []
    for value in values:
        value = value.strip()
        if not value:
            continue
        person = config.find_person(value)
        names.append(person.name if person else value)
        if person:
            ids.append(person.id)
    return list(dict.fromkeys(names)), list(dict.fromkeys(ids))


def resolve_authors(values: list[str], config: Config) -> list[str]:
    return resolve_people(values, config)[0]


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def ingest_text(text: str, *, config: Config, store: Store, via: str = "", actor: str = "", **kwargs: Any) -> Item:
    """Tags and saves an item, and records it for the dashboard. `via` says which way it came
    in (slack, email, drive...). See prepare_item for the other arguments."""
    item = prepare_item(text, config=config, via=via, **kwargs)
    store.insert(item)
    events.item_saved(store, item, via=via, actor=actor)
    link_shared(item, store)
    return item


def link_shared(item: Item, store: Store) -> None:
    """Pages and docs an item shares that are saved as items of their own get a "related" link
    from it, once each."""
    ends = {f"item:{link['saved_as']}" for link in item.source_metadata.get("links") or [] if link.get("saved_as")}
    ends.discard(f"item:{item.row_id}")
    if not ends:
        return
    me = f"item:{item.row_id}"
    have = {lk.to_id for lk in store.links_for([me]) if lk.from_id == me and lk.kind == "related"}
    where = item.source_metadata.get("label") or item.source_type
    for end in sorted(ends - have):
        store.add_link(Link(from_id=me, to_id=end, kind="related", reason=f"Shared in {where}"))


def prepare_item(
    text: str,
    *,
    config: Config,
    source_type: str,
    authors: list[str],
    via: str = "",
    reading: bool = False,
    date_created: datetime | None = None,
    visibility: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    drive_file_id: str | None = None,
    row_id: str | None = None,
    note: str = "",
    tags: dict[str, Any] | None = None,
) -> Item:
    """Validates, applies governance and tags an item, without saving it (the web form
    shows this as an editable preview). `note` is guidance from the submitter for the
    tagger, kept in the metadata. `tags` (summary, theme_tags, narrative_tags), when given,
    are used as they are instead of calling the LLM, e.g. after someone reviewed a preview."""
    text = text.strip()
    if not text:
        raise ValueError("Nothing to ingest: the text is empty.")
    source_metadata = dict(source_metadata or {})
    given = (source_type or "").strip()
    source_type = given.lower()
    if source_type not in SOURCE_TYPES:
        # A type we don't know (or none) is saved as "other", keeping what was given for review.
        if given:
            source_metadata["source_type_given"] = given
        source_type = "other"
    visibility = visibility or config.default_visibility(source_type)
    if visibility not in VISIBILITIES:
        raise ValueError("visibility must be public or private")
    if not authors:
        raise ValueError("Every item needs at least one author.")

    text = meetings.prepare(text, source_metadata, source_type)
    authors, author_ids = resolve_people(authors, config)
    item = Item(
        **({"row_id": row_id} if row_id else {}),
        date_created=_utc(date_created),
        source_type=source_type,
        via=via,
        reading=reading,
        source_metadata=source_metadata,
        authors=authors,
        author_ids=author_ids,
        drive_file_id=drive_file_id,
        extracted_text=text,
        visibility=visibility,
    )

    if note.strip():
        item.source_metadata["note"] = note.strip()
    if tags is not None:
        item.summary = (tags.get("summary") or "").strip()
        item.theme_tags = _known(tags.get("theme_tags"), {t.name for t in config.themes})
        item.narrative_tags = _known(tags.get("narrative_tags"), {n.name for n in config.narratives})
    else:
        _apply_tags(item, config)
    return item


def _known(values: list[str] | None, allowed: set[str]) -> list[str]:
    return [v for v in dict.fromkeys(values or []) if v in allowed]


def _tagging_note(item: Item) -> str:
    """The submitter's note, plus what the tagger needs to know about shared links and reading."""
    parts = [item.source_metadata.get("note", "")]
    if meetings.paraphrased(item.source_metadata):
        parts.append(meetings.TAGGING_NOTE)
    elif item.source_metadata.get("notes"):
        parts.append("This is a meeting transcript. The meeting's notes, for context:\n"
                     + str(item.source_metadata["notes"])[:4000])
    if item.reading:
        parts.append("This is someone else's content, which the team shared as reading. Tag what it's about, "
                     "and say in the summary whose view it is.")
    links = item.source_metadata.get("links") or []
    if links:
        pages = "\n".join(
            f"- {link.get('title') or link.get('url')}" + (f" by {link['author']}" if link.get("author") else "")
            + (f" ({link['site']})" if link.get("site") else "") + (f": {link['excerpt']}" if link.get("excerpt") else "")
            for link in links)
        parts.append("The people in this item are responding to what they shared, which someone else may have "
                     "written. Tag and summarise their response and their view, not the shared content:\n" + pages)
    return "\n\n".join(p for p in parts if p.strip())


def _apply_tags(item: Item, config: Config) -> None:
    """A tagging failure shouldn't lose the capture: the item is kept untagged with the reason."""
    item.source_metadata.pop("tagging_error", None)
    item.source_metadata.pop("suggested_themes", None)
    try:
        tags = tag(item.extracted_text, config, item.source_type, item.authors, note=_tagging_note(item))
        item.summary = tags["summary"]
        item.theme_tags = tags["theme_tags"]
        item.narrative_tags = tags["narrative_tags"]
        if tags["suggested_themes"]:
            item.source_metadata["suggested_themes"] = tags["suggested_themes"]
    except Exception as exc:  # noqa: BLE001
        log.exception("Tagging failed for %s", item.row_id)
        item.source_metadata["tagging_error"] = str(exc)[:500]


def refresh_text(
    existing: Item,
    text: str,
    *,
    config: Config,
    store: Store,
    authors: list[str],
    source_metadata: dict[str, Any] | None = None,
    via: str = "",
) -> Item:
    """Replaces an item's text when its source has grown (e.g. new replies in a Slack thread)
    and tags it again. Status, visibility and deliverables are kept."""
    text = text.strip()
    authors, author_ids = resolve_people(authors, config)
    if text == existing.extracted_text and authors == existing.authors:
        return existing
    item = existing.model_copy(deep=True)
    item.extracted_text = text
    item.authors, item.author_ids = authors, author_ids
    item.source_metadata.update(source_metadata or {})
    _apply_tags(item, config)
    events.record(store, "item_updated", f"Updated and re-tagged {item.source_metadata.get('label') or item.source_type}"
                  " because its source changed", via=via, row_id=item.row_id)
    link_shared(item, store)
    return store.update(item.row_id, {
        "extracted_text": item.extracted_text,
        "authors": item.authors,
        "author_ids": item.author_ids,
        "source_metadata": item.source_metadata,
        "summary": item.summary,
        "theme_tags": item.theme_tags,
        "narrative_tags": item.narrative_tags,
    })


def ingest_file(
    data: bytes,
    filename: str,
    *,
    config: Config,
    store: Store,
    source_type: str | None = None,
    mime_type: str | None = None,
    **kwargs: Any,
) -> Item:
    mime = guess_mime(filename, mime_type)
    if source_type is None:
        source_type = "voicenote" if mime.startswith("audio/") else "document"
    text = extract_text(data, filename, config, mime)
    metadata = {"file_name": filename, "mime_type": mime, **(kwargs.pop("source_metadata", None) or {})}
    metadata.setdefault("label", filename.rsplit(".", 1)[0])
    return ingest_text(text, config=config, store=store, source_type=source_type, source_metadata=metadata, **kwargs)
