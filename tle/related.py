# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Related links across sources: "this meeting, this chat session and this document are about the
same thing."

After each scheduled sync, the items saved since the last run are each compared with the items
around them: within a few weeks, sharing a person, and sharing themes or words. The best few
candidates go to the quick model, which says which are really about the same piece of work,
client, project or decision. Those get a "related" link, shown on both items' pages and
returned by `get_item`.

The model can also say two items are the same meeting recorded twice: a transcript from the
Drive folder and the notes emailed in, say. Then the notes are kept on the transcript too, as
if they'd come in one doc, so the transcript carries its summary.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from . import meetings
from .config import Config
from .llm import LLM
from .models import Item, Link
from .store import SearchQuery, Store

log = logging.getLogger(__name__)

STATE = "related"
WINDOW = timedelta(days=21)
CANDIDATES = 8
PER_RUN = 40
FIRST_RUN = timedelta(days=2)  # a database that never ran this starts with the last two days
SUMMARY_CHARS = 400

SYSTEM = (
    "You connect a company's knowledge base. You get one ITEM (a chat session, meeting, document, email or post) "
    "and some CANDIDATES saved around the same time by the same people. Say which candidates are about the same "
    "specific thing as the item: the same piece of work, client, project, deliverable, event or decision. "
    "Sharing a broad theme (AI, SEO, pricing in general) is not enough. Most candidates are unrelated; return "
    "an empty list then. If a candidate is the very same meeting or call as the item, recorded another way (a "
    "transcript and its notes), say same_meeting."
)

SCHEMA = {
    "type": "object",
    "properties": {"related": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "the candidate's id"},
            "relation": {"type": "string", "enum": ["related", "same_meeting"]},
            "reason": {"type": "string", "description": "one short sentence: what they share"},
        },
        "required": ["id", "relation", "reason"],
    }}},
    "required": ["related"],
}

WORD = re.compile(r"[a-z][a-z0-9'-]{3,}")
STOP = set("this that with from have been were they their there what when which will would could should about "
           "into your just also more than then them some like only very much over such here does done make made "
           "need want think thanks thank going good great really".split())


def catch_up(config: Config, store: Store, limit: int = PER_RUN) -> int:
    """Relates the items saved since the last run, oldest first. Returns how many were looked at."""
    state = store.get_state(STATE) or {}
    since = datetime.fromisoformat(state["cursor"]) if state.get("cursor") else datetime.now(timezone.utc) - FIRST_RUN
    new = store.search(SearchQuery(ingested_after=since, limit=500))
    new.sort(key=lambda i: i.date_ingested)
    done = 0
    for item in new[:limit]:
        try:
            relate(item, config, store)
        except Exception:  # noqa: BLE001
            log.exception("Couldn't relate %s", item.row_id)  # the next item still goes ahead
        done += 1
        store.set_state(STATE, {"cursor": item.date_ingested.isoformat()})
    return done


def candidates(item: Item, store: Store) -> list[Item]:
    """Items around this one, by the same people, ranked by themes and words in common."""
    nearby = store.search(SearchQuery(authors=item.authors, date_from=item.date_created - WINDOW,
                                      date_to=item.date_created + WINDOW, limit=200))
    mine = _words(item)
    linked = {lk.to_id for lk in store.links_for([f"item:{item.row_id}"])} | \
             {lk.from_id for lk in store.links_for([f"item:{item.row_id}"])}
    scored = []
    for other in nearby:
        if other.row_id == item.row_id or f"item:{other.row_id}" in linked:
            continue
        if _same_channel(item, other):
            continue  # a channel's own sessions are linked by their topics' chains
        themes = len(set(item.theme_tags) & set(other.theme_tags))
        words = len(mine & _words(other))
        if themes or words >= 3:
            scored.append((themes * 3 + words, other))
    scored.sort(key=lambda pair: -pair[0])
    return [other for _, other in scored[:CANDIDATES]]


def relate(item: Item, config: Config, store: Store) -> list[Link]:
    """Asks the quick model which candidates are about the same thing, and links them."""
    found = candidates(item, store)
    if not found:
        return []
    listed = "\n".join(f"- id {i}: {_describe(other)}" for i, other in enumerate(found))
    prompt = f"ITEM:\n{_describe(item)}\n\nCANDIDATES:\n{listed}"
    result = LLM(config.quick).generate_json(SYSTEM, prompt, SCHEMA, max_tokens=2000)
    links = []
    for raw in result.get("related") or []:
        try:
            other = found[int(str(raw.get("id")).strip())]
        except (ValueError, IndexError):
            continue
        ends = {f"item:{item.row_id}", f"item:{other.row_id}"}
        if any({lk.from_id, lk.to_id} == ends for lk in store.links_for([f"item:{other.row_id}"])):
            continue  # linked meanwhile, e.g. by a sync running at the same time
        relation = raw.get("relation") if raw.get("relation") in ("related", "same_meeting") else "related"
        if relation == "same_meeting" and not (_meeting(item) and _meeting(other)):
            relation = "related"  # only meetings can be the same meeting
        link = Link(from_id=f"item:{item.row_id}", to_id=f"item:{other.row_id}", kind="related",
                    reason=str(raw.get("reason") or "").strip()[:300] or "About the same thing")
        if relation == "same_meeting":
            link.reason = "The same meeting. " + link.reason
            _join(item, other, store)
        store.add_link(link)
        links.append(link)
    log.info("Related %s to %d of %d candidates", item.row_id, len(links), len(found))
    return links


def _join(one: Item, two: Item, store: Store) -> None:
    """A meeting's notes, kept on its transcript, as if both had come in one doc."""
    notes = next((i for i in (one, two) if meetings.paraphrased(i.source_metadata)), None)
    transcript = next((i for i in (one, two) if i.source_metadata.get("record") == "transcript"), None)
    if not notes or not transcript or transcript.source_metadata.get("notes"):
        return
    current = store.get(transcript.row_id) or transcript
    store.update(transcript.row_id, {"source_metadata": {
        **current.source_metadata, "record": "both", "notes": notes.extracted_text, "notes_from": notes.row_id}})


def _describe(item: Item) -> str:
    label = item.source_metadata.get("title") or item.source_metadata.get("label") or item.source_type
    kind = item.source_type + (" notes" if meetings.paraphrased(item.source_metadata) else "")
    summary = (item.summary or " ".join(item.extracted_text.split()))[:SUMMARY_CHARS]
    return f"{label} ({kind}, {item.date_created:%Y-%m-%d}, {', '.join(item.authors)}): {summary}"


def _words(item: Item) -> set[str]:
    text = " ".join([str(item.source_metadata.get("title") or item.source_metadata.get("label") or ""),
                     item.summary, item.extracted_text[:3000]]).lower()
    return {w for w in WORD.findall(text) if w not in STOP}


def _channel(item: Item) -> str:
    """The chat channel an item came from, if any."""
    md = item.source_metadata
    if md.get("clickup_channel_id"):
        return f"clickup:{md['clickup_channel_id']}"
    if md.get("channel_id"):
        return f"slack:{md['channel_id']}"
    return str(md.get("place") or "")


def _same_channel(one: Item, two: Item) -> bool:
    """Chat from one channel is never compared with itself: its sessions are chained by their
    topics, and older per-message items overlap the sessions that cover the same messages."""
    channel = _channel(one)
    return bool(channel) and channel == _channel(two)


def _meeting(item: Item) -> bool:
    return item.source_type in ("meeting", "interview") or bool(item.source_metadata.get("record"))
