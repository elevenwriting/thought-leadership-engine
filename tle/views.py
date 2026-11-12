# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Views: the positions people took in a chat session's topics.

For a channel that asks for them, the main model reads each topic once its topics are found:
the topic's messages, the rest of the session for context, and the earlier topics it continues.
It lists each view someone expressed (an opinion, a lesson, a disagreement, a prediction),
credited to them and pointing at the messages where they said it. Most topics have none.
"""

from __future__ import annotations

import logging

from . import topics
from .config import Config
from .llm import LLM
from .models import Item, Topic, View
from .store import Store

log = logging.getLogger(__name__)

STANCES = ("new", "agrees", "disagrees", "builds_on")
CONTEXT_CHARS = 300  # each message elsewhere in the session, shortened

SYSTEM = (
    "You build a thought-leadership database for {company}. Given one topic from the team's chat (its "
    "messages, and for context the rest of the session and the earlier topics it continues), pull out the "
    "VIEWS the team's people expressed IN THIS TOPIC'S MESSAGES: opinions, positions, reasoning, lessons, "
    "disagreements and predictions about their field, their clients or how the company should work. "
    "Logistics, status updates, thanks and jokes are not views. Most topics have none; return an empty list "
    "then. Write each view as one or two plain sentences stating the person's position, close to their own "
    "words, understandable on its own. Only credit people who wrote in this topic's messages, never a bot."
)

SCHEMA = {
    "type": "object",
    "properties": {"views": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "who holds the view, as named in the messages"},
            "view": {"type": "string"},
            "messages": {"type": "array", "items": {"type": "integer"}, "description": "# numbers where they say it"},
            "responds_to": {"type": ["string", "null"], "description": "whose point it answers, or null"},
            "stance": {"type": "string", "enum": list(STANCES)},
        },
        "required": ["person", "view", "messages", "responds_to", "stance"],
    }}},
    "required": ["views"],
}


def wanted(item: Item) -> bool:
    """Whether the channel this session came from asks for views."""
    return topics.is_session(item) and bool(item.source_metadata.get("views"))


def pull(item: Item, config: Config, store: Store) -> list[View]:
    """Finds the views in each of a session's topics, replacing the ones it had. Raises if a call
    fails; views already saved for other topics are kept."""
    blocks = topics.blocks(item)
    numbers = {m["id"]: m["n"] for m in item.source_metadata["messages"] if not m.get("reply_to")}
    writers = _writers(item)
    found: list[View] = []
    for topic in store.topics_for([item.row_id]):
        found += _pull_topic(item, topic, blocks, numbers, writers, config, store)
    _clear_error(item, store)
    log.info("Found %d views in %s", len(found), item.row_id)
    return found


def _pull_topic(item: Item, topic: Topic, blocks: dict[int, str], numbers: dict[str, int],
                writers: dict[int, set[str]], config: Config, store: Store) -> list[View]:
    mine = [numbers[i] for i in topic.messages if i in numbers]
    if not mine:
        store.save_views(topic.id, [])
        return []
    earlier = [t for t in topics.chain(topic.id, store) if t.date_created < topic.date_created]
    context = "\n".join(f"- {t.date_created:%Y-%m-%d}: {t.title}. {t.summary}" for t in earlier) or "(none)"
    others = [blocks[n][:CONTEXT_CHARS] for n in sorted(blocks) if n not in mine]
    prompt = (f"TOPIC: {topic.title}\n\nEARLIER TOPICS IT CONTINUES:\n{context}\n\n"
              "THIS TOPIC'S MESSAGES:\n" + "\n".join(blocks[n] for n in mine if n in blocks)
              + ("\n\nOTHER MESSAGES IN THE SAME SESSION (context only):\n" + "\n".join(others) if others else ""))
    system = SYSTEM.replace("{company}", config.company or "a company")
    result = LLM(config.llm).generate_json(system, prompt, SCHEMA, max_tokens=8000)

    ids = {n: i for i, n in numbers.items()}
    allowed = set().union(*(writers.get(n, set()) for n in mine))
    views = []
    for raw in result.get("views") or []:
        person, text = str(raw.get("person") or "").strip(), str(raw.get("view") or "").strip()
        refs = [n for n in raw.get("messages") or [] if n in mine]
        if not text or person not in allowed:
            continue  # only people who wrote in the topic, so a misread name can't be credited
        stance = raw.get("stance") if raw.get("stance") in STANCES else "new"
        views.append(View(topic_id=topic.id, row_id=item.row_id, person=person, view=text, stance=stance,
                          responds_to=str(raw.get("responds_to") or "").strip(),
                          messages=[ids[n] for n in refs]))
    store.save_views(topic.id, views)
    return views


def _writers(item: Item) -> dict[int, set[str]]:
    """Each top-level message's number -> everyone who wrote in it or its thread (known people only)."""
    out: dict[int, set[str]] = {}
    for m in item.source_metadata["messages"]:
        if m["who"] and m["who"] != "Unknown":
            out.setdefault(m["n"], set()).add(m["who"])
    return out


def _clear_error(item: Item, store: Store) -> None:
    current = store.get(item.row_id) or item
    if "views_error" in current.source_metadata:
        metadata = {k: v for k, v in current.source_metadata.items() if k != "views_error"}
        store.update(item.row_id, {"source_metadata": metadata})


def by_topic(row_ids: list[str], store: Store) -> dict[str, list[View]]:
    out: dict[str, list[View]] = {}
    for view in store.views_for(row_ids):
        out.setdefault(view.topic_id, []).append(view)
    return out


def describe(view: View, item: Item) -> dict:
    """A view as API and MCP callers get it, with the #numbers of its messages."""
    numbers = {m["id"]: m["n"] for m in item.source_metadata.get("messages") or [] if not m.get("reply_to")}
    return {"person": view.person, "view": view.view, "stance": view.stance,
            "responds_to": view.responds_to or None, "messages": [numbers[i] for i in view.messages if i in numbers]}
