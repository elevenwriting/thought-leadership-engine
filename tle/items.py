"""Changing saved items, with each change recorded in the item's history."""

from __future__ import annotations

from typing import Any

from . import events
from .config import SOURCE_TYPES, STATUSES, VISIBILITIES, Config
from .ingest import _apply_tags, resolve_people
from .models import Item

INTERNAL = {"flags", "author_ids"}


def label(item: Item) -> str:
    meta = item.source_metadata
    return str(meta.get("label") or meta.get("subject") or meta.get("file_name") or item.source_type)


def update(store, config: Config, row_id: str, fields: dict[str, Any], *, via: str, actor: str = "") -> Item | None:
    before = store.get(row_id)
    if before is None:
        return None
    fields = {k: v for k, v in fields.items() if v is not None}
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if "visibility" in fields and fields["visibility"] not in VISIBILITIES:
        raise ValueError("visibility must be public or private")
    metadata = dict(fields.get("source_metadata", before.source_metadata))

    if "authors" in fields:
        names, ids = resolve_people(fields["authors"], config)
        if not names:
            raise ValueError("Every item needs at least one author.")
        fields["authors"], fields["author_ids"] = names, ids
        if names != before.authors:
            # Someone has decided, so it no longer needs review.
            metadata.pop("author_needs_review", None)
            metadata["author_source"] = "provided"
    if "source_type" in fields and fields["source_type"] != before.source_type:
        if fields["source_type"] not in SOURCE_TYPES:
            raise ValueError(f"Unknown source type {fields['source_type']}")
        metadata.pop("source_type_given", None)
    if metadata != before.source_metadata:
        fields["source_metadata"] = metadata

    after = store.update(row_id, fields)
    diff = events.changes(before, after, [f for f in fields if f not in INTERNAL])
    if diff:
        if "extracted_text" in diff:
            diff["extracted_text"] = [f"{len(before.extracted_text)} characters", f"{len(after.extracted_text)} characters"]
        events.record(store, "item_updated", f"Changed {', '.join(_names(diff))} on {label(after)}",
                      via=via, row_id=row_id, actor=actor, changes=diff)
    return after


def _names(diff: dict) -> list[str]:
    return [f.replace("_tags", "s").replace("_", " ") for f in diff]


def add_deliverable(store, config: Config, row_id: str, deliverable: str, *, via: str, actor: str = "") -> Item | None:
    item = store.get(row_id)
    if item is None:
        return None
    return update(store, config, row_id, {"deliverables": [*item.deliverables, deliverable], "status": "used"},
                  via=via, actor=actor)


def retag(store, config: Config, row_id: str, *, via: str, actor: str = "") -> Item | None:
    """Runs the tagger again, e.g. after the themes changed or tagging failed."""
    item = store.get(row_id)
    if item is None:
        return None
    _apply_tags(item, config)
    return update(store, config, row_id, {"summary": item.summary, "theme_tags": item.theme_tags,
                                          "narrative_tags": item.narrative_tags,
                                          "source_metadata": item.source_metadata}, via=via, actor=actor)


def delete(store, row_id: str, *, via: str, actor: str = "") -> bool:
    item = store.get(row_id)
    if item is None:
        return False
    store.delete(row_id)
    events.record(store, "item_deleted", f"Deleted {label(item)} ({item.citation})", via=via, row_id=row_id,
                  actor=actor, citation=item.citation)
    return True
