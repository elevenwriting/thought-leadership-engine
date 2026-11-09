"""The source settings as form fields: what the Settings page shows, and how a save is read back."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .config import SOURCE_TYPES


@dataclass
class Field:
    path: str  # dotted, inside the section: "gmail.inbox"
    label: str
    kind: str = "text"  # text | number | bool | choice | select | multi | emoji | list | pick | date
    hint: str = ""
    default: Any = None
    options: list[tuple[str, str]] = field(default_factory=list)  # for choice: (value, label)
    show_if: tuple[str, str] | None = None  # only relevant when another field alongside has this value
    on: Any = True  # what a bool stores when switched on, and off
    off: Any = False
    source: str = ""  # for pick: where the choices come from (slack | drive), typed in when that's unavailable
    inline: bool = False  # a short number drawn like a switch: the box first, its label and hint beside it

    @property
    def name(self) -> str:
        return self.path.replace(".", "__")

    @property
    def on_value(self) -> str:
        """The checkbox's value attribute, which show_if compares against."""
        return self.on if isinstance(self.on, str) else "on"


@dataclass
class Rows:
    """A list of things each with its own rules (Slack channels, Drive folders), stored under `key`."""
    key: str
    noun: str
    fields: list[Field]
    empty: str
    name_prefix: str = ""  # shown before a row's name in the list, e.g. "#"
    title: str = ""  # a heading, when a section has more than one list
    shown: Any = None  # reads the rows from settings, for older layouts; defaults to the list under `key`

    @property
    def source(self) -> str:
        return next((f.source for f in self.fields if f.kind == "pick"), "")


@dataclass
class Section:
    key: str
    title: str
    lead: str
    fields: list[Field]
    rows: list[Rows] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        """Where its pickers' choices come from; if any can't be reached, the section can't be edited."""
        return [s for s in [*(r.source for r in self.rows), *(f.source for f in self.fields if f.kind == "pick")] if s]


CHANNEL_FIELDS = [
    Field("id", "Channel", "pick", source="slack"),
    Field("mode", "Save", "select", default="reaction",
          options=[("all", "Every message"), ("reaction", "Reacted messages and threads")]),
    Field("reaction", "Reaction", "emoji", default="round_pushpin", show_if=("mode", "reaction")),
    Field("include_threads", "Save whole threads", "bool",
          "The parent and every reply as one item, rebuilt as the thread grows", True),
    Field("include_files", "Save shared files", "bool",
          "Documents, PDFs, images and voice clips, as items of their own. Their author is worked out from the content.", True),
    Field("include_links", "Save shared links", "bool",
          "Each linked page, such as a LinkedIn post or an article, is read and saved as its own item, "
          "credited to whoever wrote it, with the message as a note.", True),
]
CHANNEL_DEFAULTS = {f.path: f.default for f in CHANNEL_FIELDS if f.path not in ("id", "name")}

CLICKUP_PLACE_FIELDS = [
    Field("id", "Space, folder or list", "pick", source="clickup_places"),
    Field("tasks", "Save tagged tasks", "bool", "Each with its comments, rebuilt as comments are added", True),
    Field("tag", "Tag", hint="Tasks with this tag are saved", default="ingest", show_if=("tasks", "on")),
    Field("docs", "Save Docs", "bool", "Every Doc in it, updated as it changes", False),
    Field("include_files", "Save attached files", "bool", "As items of their own", True),
    Field("include_links", "Save shared links", "bool", "Each linked page is read and saved as its own item", True),
    Field("visibility", "Visibility", "select", default="",
          options=[("", "Default for ClickUp"), ("public", "Public"), ("private", "Private")]),
]
CLICKUP_CHANNEL_FIELDS = [
    Field("id", "Channel", "pick", source="clickup_channels"),
    Field("mode", "Save", "select", default="reaction",
          options=[("sessions", "Every message, as sessions"), ("reaction", "Reacted messages and threads")]),
    Field("reaction", "Reaction", "emoji", default="round_pushpin", show_if=("mode", "reaction")),
    Field("from", "Starting from", "date",
          "Nothing posted before this is saved. An earlier date reads the channel's history, which uses more "
          "model calls. Changing this or the mode later never re-reads: to start over, remove the channel and add "
          "it again.", ""),
    Field("gap_hours", "A session ends after", "number", "hours without a new message", 4,
          show_if=("mode", "sessions")),
    Field("views", "Pull out views", "bool",
          "The main model lists each person's positions in every topic. About 4 times the cost of finding topics.",
          False, show_if=("mode", "sessions")),
    Field("include_threads", "Save whole threads", "bool", "The message and every reply as one item", True,
          show_if=("mode", "reaction")),
    Field("look_back_days", "Days to keep watching", "number",
          "New reactions, and new replies that update a session, are picked up for this many days after a message",
          7, inline=True),
    Field("include_links", "Save shared links", "bool", "Each linked page is read and saved as its own item", True),
    Field("visibility", "Visibility", "select", default="",
          options=[("", "Default for ClickUp"), ("public", "Public"), ("private", "Private")]),
]

FOLDER_FIELDS = [
    Field("id", "Folder", "pick", source="drive"),
    Field("source_types", "Holds", "multi", default=["document"],
          hint="Pick several and each file's type is worked out from its content",
          options=[(t, t.capitalize()) for t in SOURCE_TYPES]),
    Field("visibility", "Visibility", "select", default="",
          options=[("", "Default for the source type"), ("public", "Public"), ("private", "Private")]),
    Field("include_subfolders", "Include subfolders", "bool", "Files in folders inside this one are read too", False),
]

SECTIONS = [
    Section("slack", "Slack", "Only the channels added here are saved from, each with its own rules.", [],
            [Rows("channels", "channel", CHANNEL_FIELDS, "No channels yet, so nothing is saved from Slack.", "#",
                  shown=lambda data: _channels(data))]),
    Section("drive", "Google Drive", "Folders the scheduled sync reads, each with its own rules. Share each one with "
            "the service account. Authors are worked out from the content, falling back to the file owner.", [],
            [Rows("folders", "folder", FOLDER_FIELDS, "No folders yet, so nothing is read from Drive.",
                  shown=lambda data: _folders_shown(data))]),
    Section("clickup", "ClickUp", "Read on a schedule with the ClickUp API token, so it sees what that user sees. "
            "Authors are worked out from the content, falling back to whoever created it.", [
    ], [
        Rows("places", "place", CLICKUP_PLACE_FIELDS, "No spaces, folders or lists yet, so no tasks or Docs are saved.",
             title="Tasks and Docs"),
        Rows("channels", "channel", CLICKUP_CHANNEL_FIELDS, "No Chat channels yet, so no Chat messages are saved.", "#",
             title="Chat"),
    ]),
    Section("email", "Email", "A dedicated Google Workspace inbox. Whatever is sent to it is saved, and the author is worked out "
            "from the content. Items where that isn't clear show up under Needs review.", [
        Field("only_people", "Only allow registered people", "bool",
              "Mail from anyone not on People is filed as rejected. Off, anyone can contribute.", False),
        Field("gmail.inbox", "Inbox address", default=""),
        Field("gmail.service_account", "Service account", hint="Only needed when running locally"),
        Field("gmail.processed_label", "Label for saved mail", default="tle-saved"),
        Field("gmail.rejected_label", "Label for rejected mail", default="tle-rejected"),
    ]),
    Section("files", "Files", "PDFs are read from their text layer, which is free and exact.", [
        Field("pdf_fallback", "Send PDFs without a text layer to {model} to read", "bool",
              "Scanned PDFs and slides saved as pictures. This uses more tokens. Off, they're refused.",
              "llm", on="llm", off="off"),
        Field("min_chars_per_page", "Count a PDF as scanned below", "number", "characters per page", 100,
              show_if=("pdf_fallback", "llm")),
        Field("pages_per_call", "Pages per call", "number", default=8, show_if=("pdf_fallback", "llm")),
        Field("max_pdf_pages", "Read up to", "number", "pages", 50, show_if=("pdf_fallback", "llm")),
    ]),
]
SECTION_KEYS = tuple(s.key for s in SECTIONS)
# Older settings the form replaces, dropped when a section is saved.
RETIRED = {"slack": ["allowed_channels", "author_detection", *CHANNEL_DEFAULTS], "email": ["allow_people", "allowed_domains", "author_detection", "provider", "imap"],
           "drive": ["author_detection"]}



def _get(data: dict, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _set(data: dict, path: str, value: Any) -> None:
    *parents, last = path.split(".")
    for part in parents:
        data = data.setdefault(part, {})
    if value is None:
        data.pop(last, None)
    else:
        data[last] = value


def _channels(data: dict) -> list[dict]:
    """Slack channels with their rules, including ones from the older shared allowed_channels list."""
    if data.get("channels"):
        return [{**CHANNEL_DEFAULTS, **c} for c in data["channels"]]
    shared = {k: data.get(k, v) for k, v in CHANNEL_DEFAULTS.items()}
    return [{"id": c, **shared} for c in data.get("allowed_channels") or []]


def _folders_shown(data: dict) -> list[dict]:
    """Drive folders, with the older single source_type read as a list of one."""
    return [{**f, "source_types": f.get("source_types") or [f.get("source_type") or "document"]}
            for f in data.get("folders") or []]


def values(sources: dict) -> dict[str, dict[str, Any]]:
    """Each section's field values for the form, with defaults where nothing is set."""
    out = {}
    for section in SECTIONS:
        data = sources.get(section.key) or {}
        current = {}
        for f in section.fields:
            value = _get(data, f.path)
            current[f.name] = f.default if value is None else value
        current["rows"] = {r.key: (r.shown or (lambda d, k=r.key: list(d.get(k) or [])))(data) for r in section.rows}
        out[section.key] = current
    return out


def _number(raw: str, label: str) -> int | float | None:
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        raise ValueError(f"{label} should be a number.") from None
    return int(number) if number.is_integer() else number


def _value(f: Field, raw: Any, label: str) -> Any:
    if f.kind == "multi":
        values = raw if isinstance(raw, list) else [raw]
        picked = [v for v in (str(v).strip() for v in values) if v in dict(f.options)]
        return picked or None
    raw = _last(raw)
    if f.kind == "bool":
        return f.on if raw else f.off
    if f.kind == "list":
        return [v.strip() for v in raw.replace("\n", ",").split(",") if v.strip()]
    if f.kind == "number":
        return _number(raw, label)
    if f.kind == "date" and raw:
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            raise ValueError(f"{label} should be a date.") from None
    return raw or None


def _last(value: Any) -> str:
    return str(value[-1] if isinstance(value, list) else value).strip()


def read(form: dict, sources: dict, names: dict[str, dict[str, str]] | None = None) -> dict:
    """The sources settings with the form's values applied. Keys the form doesn't show are kept,
    and so are sections the form didn't include (it names the ones it has in `sections`).
    `names` maps each pick source's IDs to names (from Slack or Drive), saved with each row."""
    sources = dict(sources)
    posted = form.get("sections", [])
    posted = set(posted if isinstance(posted, list) else [posted])
    for section in (s for s in SECTIONS if s.key in posted):
        data = {k: v for k, v in (sources.get(section.key) or {}).items() if k not in RETIRED.get(section.key, [])}
        for nested in {f.path.split(".")[0] for f in section.fields if "." in f.path}:
            data[nested] = dict(data.get(nested) or {})
        for f in section.fields:
            raw = form.get(f"{section.key}__{f.name}", "")
            _set(data, f.path, _value(f, raw, f"{section.title}: {f.label}"))
        for spec in section.rows:
            before = {r.get("id"): r for r in (sources.get(section.key) or {}).get(spec.key) or []}
            data[spec.key] = _row_values(form, section, spec, names or {}, before)
        sources[section.key] = {k: v for k, v in data.items() if v != {}}
    return sources


def _rows(form: dict, prefix: str) -> list[dict[str, Any]]:
    """Repeated rows named <prefix>__<n>__<field>, in order, with each field's raw value."""
    rows: dict[int, dict[str, Any]] = {}
    for key, value in form.items():
        if not key.startswith(prefix + "__"):
            continue
        n, _, name = key[len(prefix) + 2:].partition("__")
        if n.isdigit() and name:
            rows.setdefault(int(n), {})[name] = value
    return [row for _, row in sorted(rows.items())]


def _row_values(form: dict, section: Section, spec: Rows, names: dict[str, dict[str, str]], before: dict) -> list[dict]:
    """A section's rows; ones without an ID are dropped, and an ID given twice keeps the first.
    Each keeps a name, from Slack or Drive when they answered, otherwise the one it had, and any
    keys the form doesn't show (such as `since`).

    Rows with a mode and a Starting from date (ClickUp channels): a new row with no date starts
    from today and records when it was `added`, and when an existing row's mode changes, `since`
    records when, so the new mode starts from then rather than re-reading what came before."""
    rows, seen = [], set()
    for row in _rows(form, f"{section.key}__{spec.noun}"):
        row_id = _last(row.get("id", ""))
        if not row_id or row_id in seen:
            continue
        seen.add(row_id)
        values = {}
        for f in spec.fields:
            value = _value(f, row.get(f.name, ""), f"{section.title} {spec.noun} {row_id}: {f.label}")
            if value is not None:
                values[f.path] = value
        old = before.get(row_id) or {}
        shown = {f.path for f in spec.fields} | {"id", "name"}
        values.update({k: v for k, v in old.items() if k not in shown})
        dated = {f.path for f in spec.fields if f.kind == "date"}
        if "from" in dated and "mode" in values:
            if not old:
                values.setdefault("from", date.today().isoformat())
                # Its sync starts over from this: a channel removed and added again re-reads its history.
                values["added"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            elif old.get("mode", "reaction") != values["mode"]:
                values["since"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        name = names.get(spec.source, {}).get(row_id) or old.get("name")
        if name:
            values = {"id": row_id, "name": name, **{k: v for k, v in values.items() if k != "id"}}
        rows.append(values)
    return rows


def form_rows(form: dict, prefix: str) -> list[dict[str, str]]:
    """Rows named <prefix>__<n>__<field>, in order, with each field's last value as text."""
    return [{k: _last(v) for k, v in row.items()} for row in _rows(form, prefix)]
