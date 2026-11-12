# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Records what happened, for the dashboard's Activity page and each item's history.

Recording never gets in the way: if the events table can't be written, the failure is
logged and whatever was being done carries on."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .models import Event, Item, now

log = logging.getLogger(__name__)


def record(store, kind: str, message: str, *, level: str = "info", via: str = "", row_id: str | None = None,
           actor: str = "", key: str = "", **details: Any) -> None:
    """With a key, an event already recorded under it gets its count raised instead of a new row.
    Secrets are blanked out of the message, which often quotes an error."""
    from .secrets import redact

    message = redact(message)
    try:
        if key and store.repeat_event(key, now()):
            return
        store.add_event(Event(kind=kind, level=level, via=via, row_id=row_id, actor=actor,
                              message=message[:1000], details=details, key=key))
    except Exception:  # noqa: BLE001
        log.exception("Couldn't record event %s: %s", kind, message)


def problem(store, message: str, *, via: str, key: str = "", row_id: str | None = None, actor: str = "",
            **details: Any) -> None:
    """Something that went wrong and needs a person: an unreadable file, a refused link...

    `key` names the thing that failed (a Gmail message ID, a Drive file ID), so a job that
    retries it every few minutes adds to one problem rather than filling the Activity page.
    Without one, the same message from the same source counts as the same problem."""
    from .secrets import redact

    log.warning("%s: %s", via, redact(message))
    key = key or hashlib.sha1(f"{via}\n{message}".encode()).hexdigest()
    record(store, "error", message, level="error", via=via, row_id=row_id, actor=actor,
           key=f"{via}:{key}", **details)


def item_saved(store, item: Item, *, via: str, actor: str = "") -> None:
    label = item.source_metadata.get("label") or item.source_metadata.get("subject") or item.source_type
    record(store, "item_saved", f"Saved {item.source_type}: {label} by {', '.join(item.authors) or 'unknown'}",
           via=via, row_id=item.row_id, actor=actor, flags=item.flags)
    if "tagging_failed" in item.flags:
        record(store, "warning", f"Tagging failed for {label}: {item.source_metadata.get('tagging_error', '')}",
               level="warning", via=via, row_id=item.row_id, actor=actor)


def changes(before: Item, after: Item, fields) -> dict[str, list]:
    """{field: [old, new]} for the fields that actually changed."""
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    return {f: [old.get(f), new.get(f)] for f in fields if old.get(f) != new.get(f)}
