"""Coverage and trends: the counting questions that retrieval alone can't answer."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from .config import Config
from .models import now
from .store import Store, TagRow


def _formats_made(row: TagRow, formats: list[str]) -> set[str]:
    """Deliverables are recorded as 'Format: title or URL'."""
    made = set()
    for d in row.deliverables:
        prefix = d.split(":", 1)[0].strip().lower()
        made.update(f for f in formats if f.lower() == prefix)
    return made


def coverage(store: Store, config: Config, visibility: str | None = None) -> dict:
    """Theme x narrative grid: how much source material each cell has, and which formats
    have already been made from it. Empty cells and cells with material but no content
    are the gaps worth planning around."""
    rows = [r for r in store.tag_rows() if not visibility or r.visibility == visibility]
    themes = [t.name for t in config.themes]
    narratives = [n.name for n in config.narratives]

    cells = []
    for theme in themes:
        for narrative in narratives:
            matching = [r for r in rows if theme in r.theme_tags and narrative in r.narrative_tags]
            made = Counter(f for r in matching for f in _formats_made(r, config.formats))
            cells.append({
                "theme": theme,
                "narrative": narrative,
                "items": len(matching),
                "public_items": sum(r.visibility == "public" for r in matching),
                "formats_made": dict(made),
                "formats_missing": [f for f in config.formats if f not in made],
            })

    return {
        "total_items": len(rows),
        "cells": cells,
        "no_material": [f"{c['theme']} x {c['narrative']}" for c in cells if c["items"] == 0],
        "material_but_no_content": [
            f"{c['theme']} x {c['narrative']} ({c['items']} items)"
            for c in cells if c["items"] and not c["formats_made"]
        ],
        "untagged_items": sum(not r.theme_tags for r in rows),
        "theme_totals": {t: sum(t in r.theme_tags for r in rows) for t in themes},
    }


def trends(store: Store, days: int = 30) -> dict:
    """Activity per theme, narrative, author and source in the last `days` days,
    compared with the `days` before that."""
    current_start = now() - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    rows = store.tag_rows(since=previous_start)

    def split(key) -> list[dict]:
        current, previous = Counter(), Counter()
        for r in rows:
            target = current if r.date_created >= current_start else previous
            values = key(r)
            target.update(values if isinstance(values, list) else [values])
        names = set(current) | set(previous)
        out = [
            {"name": n, "current": current[n], "previous": previous[n], "change": current[n] - previous[n]}
            for n in names
        ]
        return sorted(out, key=lambda x: (-x["current"], -x["change"]))

    return {
        "window_days": days,
        "items_current": sum(r.date_created >= current_start for r in rows),
        "items_previous": sum(r.date_created < current_start for r in rows),
        "themes": split(lambda r: r.theme_tags),
        "narratives": split(lambda r: r.narrative_tags),
        "authors": split(lambda r: r.authors),
        "source_types": split(lambda r: r.source_type),
    }
