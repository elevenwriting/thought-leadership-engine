# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""MCP server for Claude Desktop, Claude Code, Cursor or any MCP client.

The service serves it at /mcp, behind the API token (see api.py), which is how people connect.
For development it also runs over stdio against whatever database the environment points at:

    tle-mcp

See README for the client config.
"""

from __future__ import annotations

from datetime import datetime

from mcp.server.mcpserver import MCPServer

from . import analytics, items, playbooks, topics
from .config import STATUSES, VISIBILITIES, get_config
from .ingest import ingest_text
from .store import SearchQuery, get_store

mcp = MCPServer(
    "thought-leadership",
    instructions=(
        "The company's captured thought leadership: what its people have said in Slack, meetings, "
        "voice notes, interviews, documents and posts, tagged by theme and narrative. Start with "
        "get_taxonomy, then get_playbook for the job at hand. Cite every item you use. Items with a "
        "private_flag are internal: use them to inform content, but don't quote or publish them "
        "without the author's approval, and tell the user which parts rest on private items. Items marked "
        "paraphrased are meeting notes, a summary by a notetaker or an AI: report what they say, but never quote "
        "them as someone's own words. "
        "Chat is saved as sessions, each split into topics; when a topic continues work from other "
        "sessions, get_chain returns the whole thread of it in order. A topic's views are a summary of "
        "each person's position: use them to find what matters, but quote the messages they point to, "
        "never the view's own wording."
    ),
)


@mcp.tool()
def get_taxonomy() -> dict:
    """The company's themes, narratives, formats and people (with roles and what they're responsible for)."""
    config = get_config()
    return {
        "company": config.company,
        "themes": [t.__dict__ for t in config.themes],
        "narratives": [n.__dict__ for n in config.narratives],
        "formats": config.formats,
        "people": [{"name": p.name, "role": p.role, "responsible_for": p.responsible_for} for p in config.people],
        "playbooks": playbooks.names(),
    }


@mcp.tool()
def get_playbook(name: str) -> str:
    """Step-by-step instructions for a job: query, coverage, updates, trends, atomise, aggregate,
    case-studies, mining, debate, foresight, post-mortems, briefs, media, after-publishing."""
    return playbooks.get(name)


@mcp.tool()
def search_items(
    text: str | None = None,
    themes: list[str] | None = None,
    narratives: list[str] | None = None,
    authors: list[str] | None = None,
    source_types: list[str] | None = None,
    sources: list[str] | None = None,
    include_reading: bool = False,
    visibility: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_text: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Find items. List filters match any of the values; filters combine with AND.
    `text` is a case-insensitive phrase, so keep it short (one to three words).
    Dates are ISO, e.g. 2026-09-01. Leave visibility empty to get public and private items.
    `source_types` are kinds of content (linkedin, meeting, document...); `sources` are the ways in
    (slack, clickup, email, drive, web, api, mcp). Reading (other people's content the team shared
    without comment) is left out unless include_reading is set; never cite it as the company's view.
    Results include summaries and citations; set include_text or call get_item for the full text.
    Chat sessions also list their topics, with the #numbers of the messages each covers."""
    store = get_store()
    items = store.search(SearchQuery(
        text=text, themes=themes or [], narratives=narratives or [], authors=authors or [],
        source_types=source_types or [], vias=sources or [], reading=None if include_reading else False,
        visibility=visibility, status=status,
        date_from=datetime.fromisoformat(date_from) if date_from else None,
        date_to=datetime.fromisoformat(date_to) if date_to else None,
        limit=min(limit, 100), offset=offset,
    ))
    return {"count": len(items),
            "items": topics.results(items, store, include_text=include_text, max_text_chars=20_000)}


@mcp.tool()
def get_item(row_id: str) -> dict:
    """One item with its full text, tags, citation and visibility. A chat session also lists its
    topics, each with its messages (#numbers in the text), the chain it's on and, where the channel
    pulls them out, the views people took in it (summaries: quote the messages, not the view)."""
    store = get_store()
    item = store.get(row_id)
    if not item:
        raise ValueError("No item with that ID.")
    return topics.result(item, store)


@mcp.tool()
def get_chain(topic_id: str) -> dict:
    """A chat topic and every topic linked to it: the same piece of work or question picked up
    in other sessions, oldest first, each with its messages and citation."""
    try:
        return {"chain": topics.chain_result(topic_id, get_store())}
    except KeyError:
        raise ValueError("No topic with that ID.")


@mcp.tool()
def get_coverage(visibility: str | None = None) -> dict:
    """Theme x narrative grid: how many items each cell has, which formats have been made from them,
    and the gaps (cells with no material, and cells with material but no content yet)."""
    return analytics.coverage(get_store(), get_config(), visibility)


@mcp.tool()
def get_trends(days: int = 30) -> dict:
    """Activity per theme, narrative, author and source type in the last `days` days, against the period before."""
    return analytics.trends(get_store(), days)


@mcp.tool()
def add_item(
    text: str,
    authors: list[str],
    source_type: str = "braindump",
    visibility: str | None = None,
    date_created: str | None = None,
    label: str | None = None,
) -> dict:
    """Save something a person said or wrote. Only use this when the user asks you to save something.
    source_type: slack, clickup, meeting, document, voicenote, braindump, interview, linkedin, marketing, web, email, other."""
    item = ingest_text(
        text, config=get_config(), store=get_store(), source_type=source_type, authors=authors, via="mcp",
        visibility=visibility, date_created=datetime.fromisoformat(date_created) if date_created else None,
        source_metadata={"label": label} if label else None,
    )
    return item.to_result(include_text=False)


@mcp.tool()
def update_item(
    row_id: str,
    status: str | None = None,
    visibility: str | None = None,
    theme_tags: list[str] | None = None,
    narrative_tags: list[str] | None = None,
) -> dict:
    """Correct an item's tags, or change its status (new, reviewed, used, archived) or visibility.
    Only make an item public when the user confirms the author has approved it."""
    if status and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if visibility and visibility not in VISIBILITIES:
        raise ValueError("visibility must be public or private")
    item = items.update(get_store(), get_config(), row_id, {"status": status, "visibility": visibility,
                        "theme_tags": theme_tags, "narrative_tags": narrative_tags}, via="mcp")
    if not item:
        raise ValueError("No item with that ID.")
    return item.to_result(include_text=False)


@mcp.tool()
def add_deliverable(row_id: str, format: str, title_or_url: str) -> dict:
    """Record a piece of content made from an item (e.g. format "LinkedIn post"), and mark the item used."""
    item = items.add_deliverable(get_store(), get_config(), row_id, f"{format}: {title_or_url}", via="mcp")
    if not item:
        raise ValueError("No item with that ID.")
    return item.to_result(include_text=False)


def _playbook_prompt(name: str):
    def prompt() -> str:
        return playbooks.get(name)
    return prompt


for _name in playbooks.names():
    mcp.prompt(name=_name, description=f"Run the {_name} playbook")(_playbook_prompt(_name))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
