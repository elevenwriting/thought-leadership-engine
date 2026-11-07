"""ClickUp: a scheduled job that saves tagged tasks, Chat messages people react to, and Docs.

Each place under `sources.clickup.places` (a space, folder or list) and each Chat channel under
`sources.clickup.channels` has its own rules:

- Tasks with the place's tag ("ingest" unless set) are saved as one item each: the title,
  description and comment thread, each commenter named. When comments are added, the item is
  rebuilt and re-tagged. Their attachments are saved as items of their own.
- Docs in the place, when turned on, are saved as one item each and updated as they change.
- Chat messages someone reacts to with the channel's emoji (or every message, in `all` mode)
  are saved like Slack's: a thread as one item, each speaker named. Reactions are checked on
  messages from the last `chat_look_back_days` days.
- Links in any of these are read and saved as items of their own (see shared.save_links).

It reads with a personal API token (CLICKUP_API_TOKEN), so it sees what that user sees. The
Chat API is marked experimental by ClickUp and may change.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .. import events
from ..attribution import attribute, parse_fields
from ..attribution import metadata as attribution_metadata
from ..config import Config
from ..emoji import EMOJI
from ..extract import extract_text, guess_mime
from ..ingest import ingest_text, refresh_text
from ..store import Store
from .shared import mostly_links, save_links

log = logging.getLogger(__name__)

API = "https://api.clickup.com/api"
MAX_FILE_BYTES = 25 * 1024 * 1024
PLACE_DEFAULTS = {"tasks": True, "tag": "ingest", "docs": False, "include_files": True, "include_links": True}
CHANNEL_DEFAULTS = {"mode": "reaction", "reaction": "round_pushpin", "include_threads": True, "include_links": True}
CLICKUP_HOSTS = ("clickup.com",)
# Doc parent types in ClickUp's API.
PARENT_TYPES = {4: "space", 5: "folder", 6: "list"}


class ClickUp:
    """A small client for the endpoints the connector uses. The sync job waits out ClickUp's rate
    limit (100 requests a minute on most plans); listing for Settings (patient=False) gives up."""

    def __init__(self, token: str | None = None, patient: bool = True):
        self.patient = patient
        token = token or os.environ.get("CLICKUP_API_TOKEN", "")
        if not token:
            raise RuntimeError("No ClickUp token found. Add a personal API token as the CLICKUP_API_TOKEN secret.")
        self.http = httpx.Client(base_url=API, headers={"Authorization": token}, timeout=30)

    def get(self, path: str, **params: Any) -> dict:
        for attempt in range(4):
            response = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
            if response.status_code == 429 and not self.patient:
                raise RuntimeError("ClickUp's rate limit was reached. Reload Settings in a minute.")
            if response.status_code == 429 and attempt < 3:
                reset = float(response.headers.get("X-RateLimit-Reset", 0)) - time.time()
                time.sleep(min(max(reset, 2), 60))
                continue
            response.raise_for_status()
            return response.json()
        return {}

    def pages(self, path: str, key: str = "data", **params: Any):
        """Follows ClickUp's v3 next_cursor pagination."""
        cursor = None
        while True:
            body = self.get(path, cursor=cursor, **params)
            yield from body.get(key) or []
            cursor = body.get("next_cursor")
            if not cursor or not body.get(key):
                return


def workspaces(client: ClickUp) -> list[dict]:
    return client.get("/v2/team").get("teams", [])


def workspace(client: ClickUp, config: Config) -> dict:
    """The workspace named in settings, or the token's first one."""
    teams = workspaces(client)
    wanted = str(config.sources.get("clickup", {}).get("workspace_id") or "")
    for team in teams:
        if not wanted or str(team["id"]) == wanted:
            return team
    raise RuntimeError(f"The ClickUp token can't see workspace {wanted}.")


def list_places(client: ClickUp | None = None, config: Config | None = None) -> list[dict]:
    """Spaces, folders and lists, for picking in Settings. IDs read as space:1, folder:2, list:3."""
    client = client or ClickUp(patient=False)
    teams = [workspace(client, config)] if config else workspaces(client)[:1]
    places = []
    for team in teams:
        for space in client.get(f"/v2/team/{team['id']}/space", archived="false").get("spaces", []):
            places.append({"id": f"space:{space['id']}", "name": space["name"]})
            for folder in client.get(f"/v2/space/{space['id']}/folder", archived="false").get("folders", []):
                places.append({"id": f"folder:{folder['id']}", "name": f"{space['name']} / {folder['name']}"})
                for lst in folder.get("lists", []):
                    places.append({"id": f"list:{lst['id']}", "name": f"{space['name']} / {folder['name']} / {lst['name']}"})
            for lst in client.get(f"/v2/space/{space['id']}/list", archived="false").get("lists", []):
                places.append({"id": f"list:{lst['id']}", "name": f"{space['name']} / {lst['name']}"})
    return places


def list_channels(client: ClickUp | None = None, config: Config | None = None) -> list[dict]:
    """Chat channels (not direct messages), for picking in Settings."""
    client = client or ClickUp(patient=False)
    team = workspace(client, config) if config else workspaces(client)[0]
    channels = [
        {"id": c["id"], "name": c.get("name") or c["id"], "private": c.get("visibility") == "PRIVATE"}
        for c in client.pages(f"/v3/workspaces/{team['id']}/chat/channels", limit=100)
        if c.get("type") == "CHANNEL" and not c.get("archived")
    ]
    return sorted(channels, key=lambda c: c["name"].lower())


def _when(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


class _People:
    """ClickUp user IDs to names, from the workspace's members."""

    def __init__(self, team: dict):
        self.names = {str(m["user"]["id"]): m["user"].get("username") or m["user"].get("email") or str(m["user"]["id"])
                      for m in team.get("members", []) if m.get("user")}

    def __call__(self, user: Any) -> str:
        if isinstance(user, dict):
            return user.get("username") or user.get("email") or self.names.get(str(user.get("id")), "Unknown")
        return self.names.get(str(user), "Unknown")


def _rules(entry: dict, defaults: dict) -> dict:
    return {**defaults, **{k: v for k, v in entry.items() if v is not None}}


# --- Tasks -----------------------------------------------------------------------

def _tagged_tasks(client: ClickUp, team_id: str, place: str, tag: str):
    kind, _, place_id = place.partition(":")
    scope = {"space": "space_ids[]", "folder": "project_ids[]", "list": "list_ids[]"}[kind]
    page = 0
    while True:
        body = client.get(f"/v2/team/{team_id}/task", page=page, include_closed="true", subtasks="true",
                          **{"tags[]": tag, scope: place_id})
        yield from body.get("tasks", [])
        if body.get("last_page", True) or not body.get("tasks"):
            return
        page += 1


def _comments(client: ClickUp, task_id: str) -> list[dict]:
    """Every comment on a task, oldest first, with replies after the comment they answer."""
    comments, start, start_id = [], None, None
    while True:
        batch = client.get(f"/v2/task/{task_id}/comment", start=start, start_id=start_id).get("comments", [])
        comments += batch
        if len(batch) < 25:
            break
        start, start_id = batch[-1]["date"], batch[-1]["id"]
    ordered = []
    for comment in sorted(comments, key=lambda c: int(c["date"])):
        ordered.append(comment)
        if comment.get("reply_count"):
            replies = client.get(f"/v2/comment/{comment['id']}/reply").get("comments", [])
            ordered += sorted(replies, key=lambda c: int(c["date"]))
    return ordered


def _save_task(client: ClickUp, task: dict, rules: dict, people: _People, config: Config, store: Store) -> None:
    task = client.get(f"/v2/task/{task['id']}", include_markdown_description="true")
    creator = people(task.get("creator") or {})
    comments = _comments(client, task["id"])
    description = (task.get("markdown_description") or task.get("text_content") or "").strip()
    lines = [task["name"], description]
    lines += [f"{people(c.get('user') or {})} ({_when(c['date']).strftime('%d.%m.%Y %H:%M')}): {c.get('comment_text', '').strip()}"
              for c in comments if c.get("comment_text", "").strip()]
    text = "\n\n".join(line for line in lines if line)
    place = f"ClickUp task \"{task['name']}\""
    metadata = {"url": task.get("url"), "label": task["name"], "clickup_task_id": task["id"],
                "list": (task.get("list") or {}).get("name"), "comment_count": len(comments)}

    if rules["include_files"]:
        for attachment in task.get("attachments") or []:
            _save_attachment(attachment, task, creator, config, store)
    if rules["include_links"]:
        shared = [(description, creator, _when(task["date_created"]))]
        shared += [(c.get("comment_text", ""), people(c.get("user") or {}), _when(c["date"])) for c in comments]
        save_links(shared, via="clickup", place=place, row_prefix="clickup-link", skip_hosts=CLICKUP_HOSTS,
                   metadata={"clickup_task_id": task["id"]}, config=config, store=store)

    row_id = f"clickup-task-{task['id']}"
    existing = store.get(row_id)
    if existing and existing.extracted_text == text:
        return
    fields, body = parse_fields(text)
    who = attribute(body, config=config, submitter=creator, fields=fields,
                    context=f"{place}, created by {creator}, with {len(comments)} comments")
    metadata.update(attribution_metadata(who, creator))
    if existing:
        refresh_text(existing, body, config=config, store=store, authors=who.authors, source_metadata=metadata,
                     via="clickup")
        log.info("Updated ClickUp task %s", task["id"])
        return
    ingest_text(body, config=config, store=store, row_id=row_id, source_type="clickup", via="clickup",
                authors=who.authors, visibility=rules.get("visibility") or None,
                date_created=who.date or _when(task["date_created"]), source_metadata=metadata)
    log.info("Saved ClickUp task %s", task["id"])


def _save_attachment(attachment: dict, task: dict, creator: str, config: Config, store: Store) -> None:
    row_id = f"clickup-file-{attachment['id']}"
    if store.get(row_id):
        return
    filename = attachment.get("title") or attachment["id"]
    try:
        # Attachment links are signed URLs, so no token is sent with them.
        response = httpx.get(attachment["url"], timeout=60, follow_redirects=True)
        response.raise_for_status()
        if len(response.content) > MAX_FILE_BYTES:
            raise ValueError("File is larger than 25 MB.")
        mime = guess_mime(filename, response.headers.get("content-type"))
        text = extract_text(response.content, filename, config, mime)
    except Exception as exc:  # noqa: BLE001
        events.problem(store, f"Couldn't read {filename}, attached to ClickUp task \"{task['name']}\": {exc}",
                       via="clickup", key=row_id, url=task.get("url"))
        return
    fields, text = parse_fields(text)
    uploader = attachment.get("user", {}).get("username") or creator
    who = attribute(text, config=config, submitter=uploader, fields=fields,
                    context=f"Attached to ClickUp task \"{task['name']}\" by {uploader}\nFile: {filename}")
    ingest_text(text, config=config, store=store, row_id=row_id, via="clickup",
                source_type="voicenote" if mime.startswith("audio/") else "document", authors=who.authors,
                date_created=who.date or _when(attachment.get("date") or task["date_created"]),
                note=f"Attached to the ClickUp task \"{task['name']}\"",
                source_metadata={"url": task.get("url"), "label": filename, "file_name": filename, "mime_type": mime,
                                 "clickup_task_id": task["id"], **attribution_metadata(who, uploader)})


# --- Docs ------------------------------------------------------------------------

def _place_ids(client: ClickUp, place: str) -> set[str]:
    """The place and everything inside it, as kind:id, to match Docs' parents against."""
    kind, _, place_id = place.partition(":")
    found = {place}
    if kind == "space":
        for folder in client.get(f"/v2/space/{place_id}/folder", archived="false").get("folders", []):
            found.add(f"folder:{folder['id']}")
            found |= {f"list:{lst['id']}" for lst in folder.get("lists", [])}
        found |= {f"list:{lst['id']}" for lst in client.get(f"/v2/space/{place_id}/list").get("lists", [])}
    elif kind == "folder":
        found |= {f"list:{lst['id']}" for lst in client.get(f"/v2/folder/{place_id}/list").get("lists", [])}
    return found


def _page_text(pages: list[dict], depth: int = 0) -> list[str]:
    parts = []
    for page in pages:
        title = (page.get("name") or "").strip()
        content = (page.get("content") or "").strip()
        if title and depth:
            parts.append(f"{'#' * min(depth + 1, 6)} {title}")
        if content:
            parts.append(content)
        parts += _page_text(page.get("pages") or [], depth + 1)
    return parts


def _save_doc(client: ClickUp, team_id: str, doc: dict, rules: dict, people: _People, config: Config, store: Store) -> None:
    row_id = f"clickup-doc-{doc['id']}"
    existing = store.get(row_id)
    updated = int(doc.get("date_updated") or doc.get("date_created") or 0)
    if existing and existing.source_metadata.get("clickup_updated") == updated:
        return
    pages = client.get(f"/v3/workspaces/{team_id}/docs/{doc['id']}/pages", max_page_depth=-1, content_format="text/md")
    pages = pages if isinstance(pages, list) else pages.get("pages") or pages.get("data") or []
    text = "\n\n".join(_page_text(pages)).strip()
    if not text:
        return
    creator = people(doc.get("creator"))
    url = f"https://app.clickup.com/{team_id}/v/dc/{doc['id']}"
    metadata = {"url": url, "label": doc.get("name"), "clickup_doc_id": doc["id"], "clickup_updated": updated}
    if rules["include_links"]:
        save_links([(text, creator, _when(updated or doc["date_created"]))], via="clickup",
                   place=f"ClickUp Doc \"{doc.get('name')}\"", row_prefix="clickup-link", skip_hosts=CLICKUP_HOSTS,
                   metadata={"clickup_doc_id": doc["id"]}, config=config, store=store)
    fields, body = parse_fields(text)
    who = attribute(body, config=config, submitter=creator, fields=fields,
                    context=f"ClickUp Doc \"{doc.get('name')}\", created by {creator}")
    metadata.update(attribution_metadata(who, creator))
    if existing:
        if existing.extracted_text != body:
            refresh_text(existing, body, config=config, store=store, authors=who.authors, source_metadata=metadata,
                         via="clickup")
        else:
            store.update(existing.row_id, {"source_metadata": {**existing.source_metadata, **metadata}})
        return
    ingest_text(body, config=config, store=store, row_id=row_id, source_type="clickup", via="clickup",
                authors=who.authors, visibility=rules.get("visibility") or None,
                date_created=who.date or _when(doc["date_created"]), source_metadata=metadata)
    log.info("Saved ClickUp Doc %s", doc["id"])


# --- Chat ------------------------------------------------------------------------

def _reacted(client: ClickUp, team_id: str, message_id: str, reaction: str) -> bool:
    want = reaction.strip(":").lower()
    for r in client.pages(f"/v3/workspaces/{team_id}/chat/messages/{message_id}/reactions", limit=100):
        if r.get("reaction", "").strip(":").lower() == want or EMOJI.get(want) == r.get("reaction"):
            return True
    return False


def _replies(client: ClickUp, team_id: str, message_id: str) -> list[dict]:
    return sorted(client.pages(f"/v3/workspaces/{team_id}/chat/messages/{message_id}/replies", limit=100),
                  key=lambda m: int(m["date"]))


def _save_channel(client: ClickUp, team_id: str, channel: dict, rules: dict, people: _People, days: int,
                  config: Config, store: Store) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    name = channel.get("name") or channel["id"]
    for message in client.pages(f"/v3/workspaces/{team_id}/chat/channels/{channel['id']}/messages", limit=100):
        if _when(message["date"]) < since:
            break  # newest first
        replies = _replies(client, team_id, message["id"]) if rules["include_threads"] and message.get("replies_count") else []
        if rules["mode"] == "reaction" and not any(
            _reacted(client, team_id, m["id"], rules["reaction"]) for m in [message, *replies]
        ):
            continue
        thread = [m for m in [message, *replies] if (m.get("content") or "").strip()]
        if not thread:
            continue
        place = f"ClickUp #{name}"
        if rules["include_links"]:
            shared = [(m["content"], people(m["user_id"]), _when(m["date"])) for m in thread]
            saved = save_links(shared, via="clickup", place=place, row_prefix="clickup-link", skip_hosts=CLICKUP_HOSTS,
                               metadata={"clickup_channel_id": channel["id"]}, config=config, store=store)
            if saved and len(thread) == 1 and mostly_links(thread[0]["content"]):
                continue
        if len(thread) == 1:
            text = thread[0]["content"].strip()
        else:
            text = "\n\n".join(f"{people(m['user_id'])} ({_when(m['date']).strftime('%d.%m.%Y %H:%M')}): {m['content'].strip()}"
                               for m in thread)
        authors = list(dict.fromkeys(people(m["user_id"]) for m in thread))
        metadata = {"label": place, "clickup_channel_id": channel["id"], "channel_name": name,
                    "url": f"https://app.clickup.com/{team_id}/chat/r/{channel['id']}/t/{message['id']}",
                    "thread": len(thread) > 1, "message_count": len(thread)}
        row_id = f"clickup-chat-{message['id']}"
        existing = store.get(row_id)
        if existing:
            if existing.extracted_text != text:
                refresh_text(existing, text, config=config, store=store, authors=authors, source_metadata=metadata,
                             via="clickup")
            continue
        ingest_text(text, config=config, store=store, row_id=row_id, source_type="clickup", via="clickup",
                    authors=authors, visibility=rules.get("visibility") or None, date_created=_when(message["date"]),
                    source_metadata=metadata)
        log.info("Saved ClickUp chat message %s", message["id"])


# --- The job ---------------------------------------------------------------------

def sync(config: Config, store: Store, client: ClickUp | None = None) -> dict[str, int]:
    settings = config.sources.get("clickup", {})
    places = [_rules(p, PLACE_DEFAULTS) for p in settings.get("places") or [] if p.get("id")]
    channels = [_rules(c, CHANNEL_DEFAULTS) for c in settings.get("channels") or [] if c.get("id")]
    if not places and not channels:
        log.info("No ClickUp places or channels configured.")
        return {"tasks": 0, "docs": 0, "chat": 0}

    client = client or ClickUp()
    team = workspace(client, config)
    team_id, people = str(team["id"]), _People(team)
    done = {"tasks": 0, "docs": 0, "chat": 0}

    docs_wanted = [(p, _place_ids(client, p["id"])) for p in places if p["docs"]]
    for place in places:
        if not place["tasks"]:
            continue
        for task in _tagged_tasks(client, team_id, place["id"], place["tag"]):
            try:
                _save_task(client, task, place, people, config, store)
                done["tasks"] += 1
            except Exception as exc:  # noqa: BLE001
                events.problem(store, f"Couldn't save ClickUp task \"{task.get('name')}\": {exc}", via="clickup",
                               key=f"task:{task['id']}", url=task.get("url"))
    if docs_wanted:
        for doc in client.pages(f"/v3/workspaces/{team_id}/docs", key="docs", limit=100):
            parent = doc.get("parent") or {}
            parent_id = f"{PARENT_TYPES.get(parent.get('type'), '')}:{parent.get('id')}"
            rules = next((p for p, ids in docs_wanted if parent_id in ids), None)
            if not rules:
                continue
            try:
                _save_doc(client, team_id, doc, rules, people, config, store)
                done["docs"] += 1
            except Exception as exc:  # noqa: BLE001
                events.problem(store, f"Couldn't save ClickUp Doc \"{doc.get('name')}\": {exc}", via="clickup",
                               key=f"doc:{doc['id']}")
    days = int(settings.get("chat_look_back_days") or 7)
    for channel in channels:
        try:
            _save_channel(client, team_id, channel, channel, people, days, config, store)
            done["chat"] += 1
        except Exception as exc:  # noqa: BLE001
            events.problem(store, f"Couldn't read ClickUp channel {channel.get('name') or channel['id']}: {exc}",
                           via="clickup", key=f"channel:{channel['id']}")
    log.info("ClickUp: %s", done)
    return done
