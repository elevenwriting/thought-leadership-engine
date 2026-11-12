# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Topics: the pieces of work and questions in a chat session, and the chains they form.

Once a session is saved, one call to the quick model reads the whole session, alongside the
place's recent topics, and lists its topics: each one piece of work or one question, with the
top-level messages it covers (their thread replies go with them). For each topic it says
whether it continues one of the recent topics, and a "continues" link records that. Linked
topics form a chain: the same work picked up across days.

A topic only points into its session, so a message filed under the wrong topic is still in
the session. Links are stored rather than merged, so a wrong one is harmless and can be removed.
"""

from __future__ import annotations

import logging

from .config import Config
from .llm import LLM
from .models import Item, Link, Topic
from .store import Store

log = logging.getLogger(__name__)

RECENT_TOPICS = 20
UNASSIGNED = "Unassigned"

SYSTEM = (
    "You organise a team's chat channel. You get one SESSION (a burst of activity, ended by a few quiet "
    "hours) and summaries of RECENT TOPICS from earlier sessions. Split the session into the topics it "
    "covers. Busy sessions come in waves: people start on one thing and other subjects come in while "
    "everyone is talking, interleaved. A topic is one piece of work or one question (a particular blog "
    "post, page, video, report, idea or decision); don't split a topic into its small steps, and don't "
    "lump different pieces of work together because the same people discussed them. Many sessions are "
    "a single topic.\n\n"
    "Every top-level message (#number) goes in exactly one topic; thread replies (↳) go with their "
    "message. Short replies ('Yep', 'Will do', 'haha') belong to whatever they answer.\n\n"
    "For each topic, say whether it continues or returns to one of the RECENT TOPICS: the same piece "
    "of work or the same question picked up again, even days later. Give that topic's id, or null if "
    "it's new. Only link when it really is the same piece of work or question, not merely a related "
    "theme."
)

SCHEMA = {
    "type": "object",
    "properties": {"topics": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "a short name for the piece of work or question"},
            "summary": {"type": "string", "description": "one sentence: what was said or decided"},
            "messages": {"type": "array", "items": {"type": "integer"}, "description": "top-level # numbers"},
            "continues": {"type": ["string", "null"], "description": "id of the recent topic it picks up, or null"},
            "why": {"type": "string", "description": "one short sentence on the link, or why it's new"},
        },
        "required": ["title", "summary", "messages", "continues", "why"],
    }}},
    "required": ["topics"],
}


def place(item: Item) -> str:
    """Where a session was said, e.g. clickup:<channel id>. Empty for items that aren't sessions."""
    return str(item.source_metadata.get("place") or "")


def is_session(item: Item) -> bool:
    return bool(item.source_metadata.get("session")) and bool(item.source_metadata.get("messages"))


def corrected(row_id: str, store: Store) -> bool:
    """Whether a person has changed this item's topics or their links, so the model mustn't redo them."""
    found = store.topics_for([row_id])
    if any(t.made_by != "model" for t in found):
        return True
    return any(link.made_by != "model" for link in store.links_for([f"topic:{t.id}" for t in found]))


def find(item: Item, config: Config, store: Store) -> list[Topic]:
    """Finds a session's topics and links them to the place's recent topics, replacing any it had.
    Raises if the model call fails; the session is kept either way. Topics a person has corrected
    are kept as they are."""
    if corrected(item.row_id, store):
        log.info("Kept %s's corrected topics", item.row_id)
        return store.topics_for([item.row_id])
    messages = item.source_metadata["messages"]
    top = {m["n"]: m["id"] for m in messages if not m.get("reply_to")}
    recent = [t for t in store.recent_topics(place(item), item.date_created, RECENT_TOPICS) if t.row_id != item.row_id]
    listed = "\n".join(f"- {t.id} ({t.date_created:%Y-%m-%d}, {', '.join(t.people)}): {t.title}. {t.summary}"
                       for t in recent) or "(none yet)"
    prompt = f"RECENT TOPICS:\n{listed}\n\nSESSION ({item.date_created:%Y-%m-%d %H:%M}):\n{item.extracted_text}"
    result = LLM(config.quick).generate_json(SYSTEM, prompt, SCHEMA, max_tokens=4000)

    known = {t.id for t in recent}
    who = _people(messages)
    topics, links, seen = [], [], set()
    for raw in result.get("topics") or []:
        ids = [top[n] for n in raw.get("messages") or [] if n in top and top[n] not in seen]
        if not ids or not str(raw.get("title") or "").strip():
            continue
        seen.update(ids)
        topic = _topic(item, raw["title"], raw.get("summary") or "", ids, who, len(topics))
        topics.append(topic)
        if raw.get("continues") in known:
            links.append(Link(from_id=f"topic:{topic.id}", to_id=f"topic:{raw['continues']}", kind="continues",
                              reason=str(raw.get("why") or "").strip()))
    missing = [i for i in top.values() if i not in seen]
    if missing:  # anything the model left out is still the session's, so nothing goes unfiled
        topics.append(_topic(item, UNASSIGNED, "Messages the topic search didn't place.", missing, who, len(topics)))

    store.save_topics(item.row_id, topics)
    for link in links:
        store.add_link(link)
    _set_title(item, topics, store)
    log.info("Found %d topics in %s, %d linked to earlier ones", len(topics), item.row_id, len(links))
    return topics


def _people(messages: list[dict]) -> dict[str, list[str]]:
    """Each top-level message's writers: its own, then its replies'."""
    who: dict[str, list[str]] = {}
    for m in messages:
        key = m.get("reply_to") or m["id"]
        who.setdefault(key, [])
        if m["who"] not in who[key]:
            who[key].append(m["who"])
    return who


def _topic(item: Item, title: str, summary: str, ids: list[str], who: dict, position: int) -> Topic:
    people = list(dict.fromkeys(name for i in ids for name in who.get(i, []) if name != "Unknown"))
    return Topic(row_id=item.row_id, place=place(item), title=title.strip(), summary=summary.strip(), messages=ids,
                 people=people, date_created=item.date_created, position=position)


def _set_title(item: Item, topics: list[Topic], store: Store) -> None:
    """A session is called after its biggest topic, "and N more" when it has others."""
    named = [t for t in topics if t.title != UNASSIGNED] or topics
    if not named:
        return
    main = max(named, key=lambda t: len(t.messages))
    others = len(named) - 1
    title = main.title + (f", and {others} more topic{'s' if others != 1 else ''}" if others else "")
    current = store.get(item.row_id) or item
    metadata = {**current.source_metadata, "title": title}
    metadata.pop("topics_error", None)
    store.update(item.row_id, {"source_metadata": metadata})


# --- Corrections ---------------------------------------------------------------
# Each one is credited to the person who made it (made_by), which also stops the model from
# redoing the session's topics later.

def _owned(topic_id: str, row_id: str, store: Store) -> Topic:
    found = store.get_topics([topic_id])
    if not found or found[0].row_id != row_id:
        raise ValueError("That topic isn't part of this item.")
    return found[0]


def rename(item: Item, topic_id: str, title: str, summary: str, actor: str, store: Store) -> Topic:
    topic = _owned(topic_id, item.row_id, store)
    if not title.strip():
        raise ValueError("Give the topic a title.")
    topic = topic.model_copy(update={"title": title.strip(), "summary": summary.strip(), "made_by": actor})
    store.save_topic(topic)
    _set_title(item, store.topics_for([item.row_id]), store)
    return topic


def move(item: Item, message_id: str, to: str, actor: str, store: Store, title: str = "") -> Topic:
    """Moves a top-level message (and its replies) to another of the session's topics, or to a new
    one when `to` is "new". A topic left with no messages is removed, with its links."""
    top = {m["id"] for m in item.source_metadata.get("messages") or [] if not m.get("reply_to")}
    if message_id not in top:
        raise ValueError("That message isn't in this session.")
    current = store.topics_for([item.row_id])
    who = _people(item.source_metadata["messages"])
    if to == "new":
        if not title.strip():
            raise ValueError("Give the new topic a title.")
        target = _topic(item, title, "", [], who, max((t.position for t in current), default=-1) + 1)
    else:
        target = _owned(to, item.row_id, store)
    for topic in current:
        if message_id in topic.messages and topic.id != target.id:
            rest = [m for m in topic.messages if m != message_id]
            if rest:
                store.save_topic(_regathered(topic, rest, who, actor))
            else:
                store.delete_topic(topic.id)
    order = [m["id"] for m in item.source_metadata["messages"] if not m.get("reply_to")]
    ids = sorted({*target.messages, message_id}, key=order.index)
    target = _regathered(target, ids, who, actor)
    store.save_topic(target)
    _set_title(item, store.topics_for([item.row_id]), store)
    return target


def _regathered(topic: Topic, ids: list[str], who: dict, actor: str) -> Topic:
    people = list(dict.fromkeys(name for i in ids for name in who.get(i, []) if name != "Unknown"))
    return topic.model_copy(update={"messages": ids, "people": people, "made_by": actor})


def candidates(item: Item, store: Store, limit: int = 30) -> list[Topic]:
    """Earlier topics from the same place that one of this session's topics could continue."""
    return store.recent_topics(place(item), item.date_created, limit)


def link(item: Item, topic_id: str, to_topic_id: str, actor: str, store: Store) -> Link:
    """Records that a topic continues an earlier one."""
    topic = _owned(topic_id, item.row_id, store)
    earlier = store.get_topics([to_topic_id])
    if not earlier or earlier[0].id == topic.id:
        raise ValueError("Pick an earlier topic.")
    ends = {f"topic:{topic.id}", f"topic:{to_topic_id}"}
    for existing in store.links_for([f"topic:{topic.id}"]):
        if {existing.from_id, existing.to_id} == ends:
            raise ValueError("Those topics are already linked.")
    new = Link(from_id=f"topic:{topic.id}", to_id=f"topic:{to_topic_id}", kind="continues", made_by=actor,
               reason=f"Linked by {actor}")
    store.add_link(new)
    return new


def unlink(item: Item, link_id: str, actor: str, store: Store) -> None:
    """Removes a link from or to one of this session's topics. The topic is marked as corrected,
    so the model doesn't make the link again."""
    own = {f"topic:{t.id}": t for t in store.topics_for([item.row_id])}
    found = [lk for lk in store.links_for(list(own)) if lk.id == link_id]
    if not found:
        raise ValueError("That link isn't on this item's topics.")
    store.delete_link(link_id)
    for end in (found[0].from_id, found[0].to_id):
        if end in own:
            store.save_topic(own[end].model_copy(update={"made_by": actor}))


def direct_links(topic: Topic, store: Store) -> list[tuple[Link, Topic]]:
    """A topic's own links, each with the topic at the other end."""
    me = f"topic:{topic.id}"
    links = store.links_for([me])
    others = {t.id: t for t in store.get_topics(
        [end.partition(":")[2] for lk in links for end in (lk.from_id, lk.to_id) if end != me and end.startswith("topic:")])}
    out = []
    for lk in links:
        other = lk.to_id if lk.from_id == me else lk.from_id
        tid = other.partition(":")[2]
        if tid in others:
            out.append((lk, others[tid]))
    return out


def chain(topic_id: str, store: Store) -> list[Topic]:
    """Every topic linked to this one by "continues" links, either way, oldest first."""
    seen, todo = {topic_id}, [topic_id]
    while todo:
        ends = [f"topic:{t}" for t in todo]
        todo = []
        for link in store.links_for(ends):
            if link.kind != "continues":
                continue
            for end in (link.from_id, link.to_id):
                kind, _, tid = end.partition(":")
                if kind == "topic" and tid not in seen:
                    seen.add(tid)
                    todo.append(tid)
    return store.get_topics(sorted(seen))


def outline(items: list[Item], store: Store) -> dict[str, list[dict]]:
    """Search results' sessions -> their topics, briefly (no chains), so a caller can go
    straight to the messages it needs."""
    sessions = {i.row_id: i for i in items if is_session(i)}
    out: dict[str, list[dict]] = {}
    for topic in store.topics_for(list(sessions)):
        out.setdefault(topic.row_id, []).append({"id": topic.id, "title": topic.title, "summary": topic.summary,
                                                 "messages": _numbers(sessions[topic.row_id], topic)})
    return out


def result(item: Item, store: Store, **kwargs) -> dict:
    """An item as API and MCP callers get it; a session also lists its topics and their chains, and
    any item lists the items it's related to."""
    data = item.to_result(**kwargs)
    if is_session(item):
        data["topics"] = describe(item, store)
    related = related_items(item, store)
    if related:
        data["related"] = related
    return data


def related_items(item: Item, store: Store) -> list[dict]:
    """Items linked to this one as related: a doc and the chat it was shared in, say."""
    me = f"item:{item.row_id}"
    out = []
    for link in store.links_for([me]):
        other_end = link.to_id if link.from_id == me else link.from_id
        kind, _, other_id = other_end.partition(":")
        other = store.get(other_id) if link.kind == "related" and kind == "item" else None
        if other:
            out.append({"row_id": other.row_id, "citation": other.citation, "reason": link.reason,
                        "label": other.source_metadata.get("title") or other.source_metadata.get("label") or ""})
    return out


def results(items: list[Item], store: Store, **kwargs) -> list[dict]:
    """Search results; sessions list their topics in brief."""
    brief = outline(items, store)
    out = []
    for item in items:
        data = item.to_result(**kwargs)
        if item.row_id in brief:
            data["topics"] = brief[item.row_id]
        out.append(data)
    return out


def chain_result(topic_id: str, store: Store) -> list[dict]:
    """A whole chain, oldest first: each topic with its session and the messages it covers."""
    found = chain(topic_id, store)
    if not found:
        raise KeyError(topic_id)
    items = {i.row_id: i for i in (store.get(t.row_id) for t in found) if i}
    out = []
    for topic in found:
        item = items.get(topic.row_id)
        if item is None:
            continue
        text = blocks(item)
        numbers = _numbers(item, topic)
        out.append({"topic_id": topic.id, "title": topic.title, "summary": topic.summary, "people": topic.people,
                    "date": topic.date_created.date().isoformat(), "row_id": item.row_id, "citation": item.citation,
                    "private_flag": item.to_result(include_text=False).get("private_flag"),
                    "messages": "\n".join(text[n] for n in numbers if n in text)})
    return out


def describe(item: Item, store: Store) -> list[dict]:
    """A session's topics for API and MCP callers, each with the chain it's on (the other topics,
    with the session each is in) and, where they were pulled out, the views people took in it."""
    out = []
    numbers = {m["id"]: m["n"] for m in item.source_metadata.get("messages") or [] if not m.get("reply_to")}
    views: dict[str, list[dict]] = {}
    for v in store.views_for([item.row_id]):
        views.setdefault(v.topic_id, []).append({
            "person": v.person, "view": v.view, "stance": v.stance, "responds_to": v.responds_to or None,
            "messages": [numbers[i] for i in v.messages if i in numbers]})
    for topic in store.topics_for([item.row_id]):
        others = [t for t in chain(topic.id, store) if t.id != topic.id]
        out.append({
            "id": topic.id, "title": topic.title, "summary": topic.summary, "people": topic.people,
            "messages": _numbers(item, topic), **({"views": views[topic.id]} if topic.id in views else {}),
            "chain": [{"topic_id": t.id, "row_id": t.row_id, "date": t.date_created.date().isoformat(),
                       "title": t.title, "summary": t.summary,
                       "when": "before" if t.date_created < topic.date_created else "after"} for t in others],
        })
    return out


def _numbers(item: Item, topic: Topic) -> list[int]:
    """The topic's messages as the #numbers in the session's text."""
    numbers = {m["id"]: m["n"] for m in item.source_metadata.get("messages") or [] if not m.get("reply_to")}
    return [numbers[i] for i in topic.messages if i in numbers]


def blocks(item: Item) -> dict[int, str]:
    """The session's text split by top-level message: #number -> that message and its replies."""
    out: dict[int, str] = {}
    current = None
    for line in item.extracted_text.splitlines():
        if line.startswith("#") and line[1:].split(" ", 1)[0].isdigit():
            current = int(line[1:].split(" ", 1)[0])
            out[current] = line
        elif current is not None:
            out[current] += "\n" + line
    return out
