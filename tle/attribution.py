# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Works out who said or wrote something, and when, when the person submitting it may
not be the author (e.g. someone emailing in a colleague's notes or a forwarded thread).

Two ways, in order:
1. Fields given at the top of the message, one per line:
       Author: Jane Smith, Mo Jones
       Date: 2026-09-12
       Type: interview
       Visibility: public
       Label: Podcast with Acme
2. Otherwise, an LLM reads the content and its context and names the author(s) and,
   if the content shows it, the date.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .llm import LLM

log = logging.getLogger(__name__)

FIELD_NAMES = {
    "author": "authors", "authors": "authors", "by": "authors", "speaker": "authors", "speakers": "authors",
    "date": "date", "when": "date",
    "type": "source_type", "source": "source_type", "source type": "source_type",
    "visibility": "visibility",
    "label": "label", "title": "label",
}
FIELD_LINE = re.compile(r"^\s*([A-Za-z ]{2,12})\s*:\s*(.+?)\s*$")
DATE_FORMATS = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"]
CONTEXT_CHARS = 12_000


@dataclass
class Attribution:
    authors: list[str]
    date: datetime | None = None
    source: str = "submitter"  # provided | llm | submitter
    reason: str = ""
    needs_review: bool = False
    fields: dict[str, str] = field(default_factory=dict)
    source_type: str | None = None  # picked from the choices given, when there were several
    record: str = ""  # for a meeting: transcript, notes or both (see meetings.py)


def parse_date(value: str) -> datetime | None:
    value = value.strip()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_fields(text: str) -> tuple[dict[str, str], str]:
    """Reads `Key: value` lines at the very top of the text. Returns the fields found and
    the text without them. Stops at the first line that isn't a known field."""
    fields: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            if fields:
                i += 1
                break
            i += 1
            continue
        match = FIELD_LINE.match(line)
        name = FIELD_NAMES.get(match.group(1).strip().lower()) if match else None
        if not name:
            break
        fields[name] = match.group(2)
        i += 1
    if not fields:
        return {}, text
    return fields, "\n".join(lines[i:]).strip()


def split_names(value: str) -> list[str]:
    return [n.strip() for n in re.split(r",|;|\band\b|&", value) if n.strip()]


SCHEMA = {
    "type": "object",
    "properties": {
        "authors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Whose ideas and words these are. Use names exactly as in the people list when they match. Empty if you can't tell.",
        },
        "date": {
            "type": "string",
            "description": "When it was originally said or written, as YYYY-MM-DD, only if the content shows it. Otherwise empty.",
        },
        "reason": {"type": "string", "description": "One sentence on how you decided."},
        "record": {
            "type": "string", "enum": ["transcript", "notes", "both", "neither"],
            "description": "For a meeting, call or interview: transcript (word for word, speaker by speaker), notes "
                           "(a summary by a notetaker or an AI such as Gemini or Granola), or both in one document. "
                           "neither for anything else.",
        },
    },
    "required": ["authors", "date", "reason", "record"],
}


def detect(text: str, *, config: Config, submitter: str, context: str = "",
           source_types: list[str] | None = None) -> Attribution:
    """Asks the LLM who the author is. Falls back to the submitter, flagged for review.
    With several source types to choose from, it also picks the one that fits. With no
    submitter (a page someone linked to), only the page itself can name its author."""
    last_rule = ("- If nothing points to anyone else, it's the submitter's own writing: return the submitter."
                 if submitter else
                 "- Whoever shared it is not its author unless the page itself shows they wrote it (a byline, "
                 "their own post).")
    people = "\n".join(f"- {p.name} ({p.role}){', ' + p.email if p.email else ''}" for p in config.people)
    system = f"""You work out who the author of a piece of content is, for {config.company or 'a company'}'s
thought leadership database. The author is the person whose ideas, opinions or words these are,
which is not always the person who submitted it.

People at the company:
{people or '- (none listed)'}

How to decide:
- A forwarded email: the person who wrote the original message, from its From line or signature.
- A transcript or interview: the people whose views it records (usually the main speakers),
  not the interviewer or note-taker, unless they're the ones giving the opinions.
- Notes about someone ("Jane's take on pricing", "from my call with Mo"): that person.
- A post, article or document with a byline or signature: the person named.
- A message sharing a link with comments on it: whoever wrote the comments, not the linked page's author.
{last_rule}
- If you genuinely can't tell, return no authors."""
    prompt = (
        (f"Submitted by: {submitter}\n\n" if submitter else "")
        + (f"<context>\n{context[:CONTEXT_CHARS]}\n</context>\n\n" if context else "")
        + f"<content>\n{text[:CONTEXT_CHARS]}\n</content>\n\nWho is the author, and when was it said or written? "
        "If it's a meeting, call or interview, is it a transcript, notes, or both?"
    )
    schema = SCHEMA
    if source_types and len(source_types) > 1:
        schema = {**SCHEMA, "properties": {**SCHEMA["properties"], "source_type": {
            "type": "string", "enum": source_types,
            "description": "Which of these kinds of content it is, e.g. a meeting transcript, an interview, a document."}},
            "required": [*SCHEMA["required"], "source_type"]}
        prompt += f" And which of these is it: {', '.join(source_types)}?"
    try:
        result = LLM(config.llm).generate_json(system, prompt, schema)
    except Exception as exc:  # noqa: BLE001
        log.warning("Author detection failed: %s", exc)
        return Attribution([submitter] if submitter else [], source="submitter",
                           reason=f"Detection failed: {exc}"[:300], needs_review=True)

    authors = [a.strip() for a in result.get("authors", []) if a and a.strip()]
    date = parse_date(result.get("date") or "") if result.get("date") else None
    picked = result.get("source_type") if result.get("source_type") in (source_types or []) else None
    record = result.get("record") if result.get("record") in ("transcript", "notes", "both") else ""
    if not authors:
        return Attribution([submitter] if submitter else [], date=date, source="submitter", reason=result.get("reason", ""),
                           needs_review=True, source_type=picked, record=record)
    known = {p.name for p in config.people} | {p.email.lower() for p in config.people if p.email}
    unknown = any(a not in known and a.lower() not in known for a in authors)
    return Attribution(authors, date=date, source="llm", reason=result.get("reason", ""), needs_review=unknown,
                       source_type=picked, record=record)


def attribute(
    text: str,
    *,
    config: Config,
    submitter: str,
    context: str = "",
    fields: dict[str, str] | None = None,
    source_types: list[str] | None = None,
) -> Attribution:
    """Fields win; then the LLM works it out from the content; then the submitter, flagged for review."""
    fields = fields or {}
    if fields.get("authors"):
        return Attribution(split_names(fields["authors"]), source="provided", fields=fields)
    result = detect(text, config=config, submitter=submitter, context=context, source_types=source_types)
    result.fields = fields
    return result


def metadata(who: Attribution, submitter: str) -> dict:
    """What every attributed item records about how its author was decided."""
    return {
        "submitted_by": submitter,
        "author_source": who.source,
        **({"author_reason": who.reason} if who.reason else {}),
        **({"author_needs_review": True} if who.needs_review else {}),
        **({"record": who.record} if who.record else {}),
    }
