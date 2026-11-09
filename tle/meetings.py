"""Meeting transcripts and meeting notes.

A transcript records what people said, so a view in it can be quoted and credited. Notes from
Gemini, Granola or a note-taker are a summary: "Jane argued for value pricing" is the notetaker's
sentence, not Jane's. So each item records what it holds, in `source_metadata.record`:
- `transcript`: quoted and credited as usual;
- `notes`: kept, but marked as paraphrased everywhere it's returned or cited;
- `both`: one doc with notes and a transcript (Meet can do this). The transcript becomes the
  item's text and the notes are kept beside it, in `source_metadata.notes`.

The text's shape usually says which it is (a speaker on every line, or headings like "Summary"
and "Next steps"). When it doesn't, the author check's answer is used.
"""

from __future__ import annotations

import re

RECORDS = ("transcript", "notes", "both")

SPEAKER = re.compile(
    r"^\s*(?:[\[(]?\d{1,2}:\d{2}(?::\d{2})?[\])]?\s*[-–]?\s*)?"           # an optional time first
    r"(?:Me|Them|Speaker \d+|[A-Z][\w.'’-]*(?: [A-Z][\w.'’-]*){0,3})"     # the speaker
    r"\s*(?:[\[(]\d{1,2}:\d{2}(?::\d{2})?[\])])?\s*:\s+\S")                # an optional time after, then the words
TIME_ONLY = re.compile(r"^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?[\])]?\s*$")
TRANSCRIPT_HEADING = re.compile(r"^\s*(?:#+\s*)?(?:\W\s*)?Transcript\s*:?\s*$", re.IGNORECASE)
NOTES_MARKERS = re.compile(
    r"^\s*(?:#+\s*)?(?:\W\s*)?(?:Notes by Gemini|Meeting notes|Summary|Next steps|Suggested next steps|Action items"
    r"|Key takeaways|Decisions|Details)\s*:?\s*$", re.IGNORECASE | re.MULTILINE)

TRANSCRIPT_SHARE = 0.35  # of non-empty lines that are a speaker's words or a time
MIN_NOTES_LINES = 3


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def transcript_share(text: str) -> float:
    lines = _lines(text)
    if not lines:
        return 0.0
    return sum(1 for line in lines if SPEAKER.match(line) or TIME_ONLY.match(line)) / len(lines)


def _looks_like_notes(text: str) -> bool:
    """Two or more of the headings notetakers use, or one that only they use."""
    found = [m.group(0).strip().lower() for m in NOTES_MARKERS.finditer(text)]
    return len(found) >= 2 or any("gemini" in f or "meeting notes" in f for f in found)


def split(text: str) -> tuple[str, str] | None:
    """(notes, transcript) when the text is notes followed by a transcript under its own heading."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if TRANSCRIPT_HEADING.match(line):
            notes, rest = "\n".join(lines[:i]).strip(), "\n".join(lines[i + 1:]).strip()
            if len(_lines(notes)) >= MIN_NOTES_LINES and transcript_share(rest) >= TRANSCRIPT_SHARE:
                return notes, rest
    return None


def classify(text: str, suggested: str = "") -> str:
    """What the text holds: transcript, notes, both, or "" for neither. Its shape wins when it's
    clear; otherwise `suggested` (the author check's answer) is used."""
    if split(text):
        return "both"
    if transcript_share(text) >= TRANSCRIPT_SHARE:
        return "transcript"
    if suggested in RECORDS:
        return suggested
    return "notes" if _looks_like_notes(text) else ""


def prepare(text: str, metadata: dict, source_type: str) -> str:
    """Records what a meeting item holds, and for notes plus a transcript in one doc, keeps the
    transcript as the text and the notes in the metadata. Returns the text to save. Other kinds of
    item are only checked when the author check says they're meeting notes."""
    suggested = str(metadata.get("record") or "")
    if metadata.get("notes"):  # already split, e.g. a preview being saved
        metadata["record"] = "both"
        return text
    if source_type not in ("meeting", "interview") and suggested not in ("notes", "both"):
        metadata.pop("record", None)
        return text
    record = classify(text, suggested)
    if not record:
        metadata.pop("record", None)
        return text
    metadata["record"] = record
    parts = split(text) if record == "both" else None
    if parts:
        metadata["notes"], text = parts
    return text


def paraphrased(metadata: dict) -> bool:
    return metadata.get("record") == "notes"


TAGGING_NOTE = ("These are meeting notes: a summary by a notetaker or an AI, not the speakers' own words. Tag what "
                "was discussed, and in the summary say what each person argued without presenting it as a quote.")
