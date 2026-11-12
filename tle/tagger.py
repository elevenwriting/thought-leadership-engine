# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""The "sorting hat": classifies an item against the company's themes and narratives."""

from __future__ import annotations

from .config import Config
from .llm import LLM


def _tag_list(names: list[str]) -> dict:
    return {"type": "string", "enum": names} if names else {"type": "string"}


def _schema(config: Config) -> dict:
    return {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Two to four sentences: the main points, opinions or claims, in plain language.",
            },
            "theme_tags": {
                "type": "array",
                "items": _tag_list([t.name for t in config.themes]),
                "description": "Themes this item says something substantive about. Can be empty.",
            },
            "narrative_tags": {
                "type": "array",
                "items": _tag_list([n.name for n in config.narratives]),
                "description": "Narratives this item could support. Can be empty.",
            },
            "suggested_themes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recurring topics in this item that none of the existing themes cover. Usually empty.",
            },
        },
        "required": ["summary", "theme_tags", "narrative_tags", "suggested_themes"],
    }


DEFAULT_INSTRUCTIONS = """You organise thought leadership for {company}.
Each item is something a person at the company said or wrote: a chat message, a meeting
transcript, a voice note, a document, an interview or a published post. Tag it so a content
team can find it later.

Rules:
- Only tag a theme when the item says something substantive about it, not a passing mention.
- Only tag a narrative when the item contains material for it: an actual opinion that goes
  against the grain for Counter-narrative, an actual forecast for Predictions, and so on.
- It's fine to return no tags. Small talk and logistics should get none.
- The summary should keep the speaker's actual position, including strong or unusual views.
  Don't soften them."""


def _system(config: Config) -> str:
    """The instructions can be replaced in Settings (`prompts.tagging`). The themes,
    narratives and people are always added after them, so edits can't drop the taxonomy."""
    instructions = (config.prompts.get("tagging") or DEFAULT_INSTRUCTIONS).strip()
    instructions = instructions.replace("{company}", config.company or "a company")
    themes = "\n".join(f"- {t.name}: {t.description}" for t in config.themes)
    narratives = "\n".join(f"- {n.name}: {n.description}" for n in config.narratives)
    people = "\n".join(f"- {p.name}, {p.role}" for p in config.people)
    return f"""{instructions}

Themes (what it's about):
{themes or '- (none defined yet)'}

Narratives (the angle it could support):
{narratives or '- (none defined yet)'}

People:
{people or '- (none listed)'}"""


def tag(text: str, config: Config, source_type: str, authors: list[str], note: str = "") -> dict:
    if len(text) > config.max_input_chars:
        text = text[: config.max_input_chars] + "\n\n[cut for length]"
    # A note from whoever submitted it ("this is for the pricing theme", "Jane's best bit is
    # the second half") guides the tagging, but the tags still have to be earned by the item.
    note_block = (
        f"<submitter_note>\n{note.strip()[:4000]}\n</submitter_note>\n"
        "Use the note as guidance on what this is and what matters in it. Only tag what the item itself supports.\n\n"
        if note and note.strip() else ""
    )
    prompt = (
        f"Source type: {source_type}\nAuthors: {', '.join(authors) or 'unknown'}\n\n{note_block}"
        f"<item>\n{text}\n</item>\n\nTag this item."
    )
    result = LLM(config.llm).generate_json(_system(config), prompt, _schema(config))

    # Providers without strict enum support can drift, so keep only known tags.
    themes = {t.name for t in config.themes}
    narratives = {n.name for n in config.narratives}
    return {
        "summary": result.get("summary", "").strip(),
        "theme_tags": [t for t in result.get("theme_tags", []) if t in themes],
        "narrative_tags": [n for n in result.get("narrative_tags", []) if n in narratives],
        "suggested_themes": [s for s in result.get("suggested_themes", []) if s],
    }
