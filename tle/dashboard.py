"""The dashboard: browse and fix items, see what happened, and change settings.

Pages are rendered on the server from tle/templates/dashboard. Every form posts back to a
route here and is redirected afterwards, so there's no JavaScript build step."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from markupsafe import Markup
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import analytics, auth, branding, events, items, settings_form
from . import settings as settings_store
from .config import SOURCE_TYPES, STATUSES, VISIBILITIES, get_config
from .emoji import EMOJI
from .models import FLAG_LABELS, Person
from .store import EventQuery, SearchQuery, get_store
from .tagger import DEFAULT_INSTRUCTIONS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", include_in_schema=False)
templates = Jinja2Templates(directory=str(resources.files("tle").joinpath("templates")))
PAGE_SIZE = 50
# Columns the Items table can show, and the ones it shows unless the Columns menu says otherwise.
ITEM_COLUMNS = {"date": "Date", "source": "Source", "themes": "Themes", "narratives": "Narratives",
                "people": "People", "visibility": "Visibility", "status": "Status", "made": "Made", "saved": "Saved"}
DEFAULT_COLUMNS = ["date", "source", "themes", "narratives", "visibility", "status", "made"]
# The theme's files; the logo can be replaced in Settings.
DEFAULT_ACCENT = "#1F5DFF"  # theme.css's accent, which the colour picker starts from
THEME_FILES = {"theme.css": "text/css", "logo-light.svg": "image/svg+xml", "logo-dark.svg": "image/svg+xml"}
ICONS = {
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "columns": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M15 4v16"/>',
    "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
    "tick": '<path d="m5 12 5 5 9-10"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
    "warn": '<path d="M12 4 2.5 20h19L12 4z"/><path d="M12 10v4M12 17h.01"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>',
    "restore": '<path d="M4 13a8 8 0 1 0 2.3-5.7M4 5v6h6"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
}


class SignInRequired(Exception):
    pass


# --- Helpers ---------------------------------------------------------------------

def _date(value: Any, with_time: bool = False) -> str:
    if not isinstance(value, datetime):
        return ""
    return value.astimezone(timezone.utc).strftime("%d %b %Y %H:%M" if with_time else "%d %b %Y")


def _heat(n: int) -> str:
    """The coverage cell's shade: none, 1-2, 3-5, 6-9, 10 or more."""
    return "h0" if n == 0 else "h1" if n <= 2 else "h2" if n <= 5 else "h3" if n <= 9 else "h4"


def _icon(name: str) -> Markup:
    return Markup(f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>')


def _initials(user: auth.User) -> str:
    words = [w for w in (user.name or "").split() if w[:1].isalpha()]
    return ("".join(w[0] for w in words[:2]) or user.email[:1]).upper()


def _signed_change(n: int) -> str:
    return f"+{n}" if n > 0 else f"\u2212{-n}" if n < 0 else "0"


templates.env.filters["date"] = _date
templates.env.filters["show"] = lambda v: ", ".join(map(str, v)) if isinstance(v, list) else ("—" if v in (None, "") else str(v)[:120])
templates.env.filters["flag"] = lambda f: FLAG_LABELS.get(f, f)
templates.env.filters["heat"] = _heat
templates.env.filters["initials"] = _initials
templates.env.filters["signed"] = _signed_change
templates.env.globals["quote"] = quote
templates.env.globals["icon"] = _icon


class _Ready:
    """Makes sure the tables and first settings exist once per process. The API does this when
    it starts; this covers anything that serves pages without that start-up step."""

    def __init__(self):
        self.lock, self.done = threading.Lock(), False

    def __call__(self, store) -> None:
        with self.lock:
            if not self.done:
                settings_store.prepare(store)
                self.done = True


_ready = _Ready()


class _Count:
    """A count shown in the menu, cached briefly so each page isn't an extra query."""

    def __init__(self, count):
        self.count, self.value, self.at = count, 0, 0.0

    def get(self, store) -> int:
        if time.monotonic() - self.at > 20:
            try:
                self.value = self.count(store)
            except Exception:  # noqa: BLE001
                log.exception("Couldn't count for the menu")
            self.at = time.monotonic()
        return self.value

    def clear(self) -> None:
        self.at = 0.0


class _Choices:
    """Slack channels, Drive folders or ClickUp places for the Settings pickers. They're fetched
    in the background and kept for ten minutes, so a slow or rate-limited service never holds
    the page up: it shows the last list, or says it's loading. `error` says why there's no list."""

    TTL = 600
    FIRST_WAIT = 6.0

    def __init__(self, service: str, fetch, missing):
        self.service, self.fetch, self.missing = service, fetch, missing
        self.value: list[dict] | None = None
        self.error = ""
        self.at = 0.0
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def get(self) -> list[dict] | None:
        with self.lock:
            missing = self.missing()
            if missing:
                self.value, self.error = None, missing
                return None
            if time.monotonic() - self.at > self.TTL and not (self.thread and self.thread.is_alive()):
                self.thread = threading.Thread(target=self._refresh, daemon=True)
                self.thread.start()
            thread, first = self.thread, self.at == 0.0
        if first and thread:
            thread.join(self.FIRST_WAIT)
        return self.value

    @property
    def loading(self) -> bool:
        return self.at == 0.0 and bool(self.thread and self.thread.is_alive())

    def _refresh(self) -> None:
        try:
            value, error = self.fetch(), ""
        except Exception as exc:  # noqa: BLE001
            log.warning("Couldn't list %s for Settings: %s", self.service, exc)
            value, error = None, _access_error(self.service, exc)
        with self.lock:
            self.value, self.error, self.at = value, error, time.monotonic()

    def clear(self) -> None:
        """Fetch again on the next page, still showing the current list meanwhile."""
        with self.lock:
            self.at = min(self.at, 1.0)


def _access_error(service: str, exc: Exception) -> str:
    """A short reason, e.g. "Drive error: Request had insufficient authentication scopes."."""
    response = getattr(exc, "response", None)
    if isinstance(getattr(response, "data", None), dict):  # Slack
        return f"{service} error: {response.data.get('error', exc)}"
    return f"{service} error: {getattr(exc, 'reason', None) or exc}"


def _list_slack() -> list[dict]:
    from .connectors.slack import list_channels
    return list_channels()


def _list_drive() -> list[dict]:
    from .connectors.drive_sync import list_folders
    return list_folders()


def _list_clickup(kind: str):
    def fetch() -> list[dict]:
        from .connectors import clickup
        return (clickup.list_places if kind == "places" else clickup.list_channels)(config=get_config())
    return fetch


def _clickup_missing() -> str:
    return "" if os.environ.get("CLICKUP_API_TOKEN") else \
        "No ClickUp token found. Add a personal API token as the CLICKUP_API_TOKEN secret."


def _slack_missing() -> str:
    return "" if os.environ.get("SLACK_BOT_TOKEN") else \
        "No Slack token found. Add the Slack app's bot token as the SLACK_BOT_TOKEN secret."


def _drive_missing() -> str:
    """Whether there are Google credentials to try: a key file, gcloud's own, or Cloud Run's."""
    adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("K_SERVICE") or adc.is_file():
        return ""
    return "No Google credentials found. Deployed, the service account is used; when developing, sign in with gcloud."


_choices = {
    "slack": _Choices("Slack", _list_slack, _slack_missing),
    "drive": _Choices("Drive", _list_drive, _drive_missing),
    "clickup_places": _Choices("ClickUp", _list_clickup("places"), _clickup_missing),
    "clickup_channels": _Choices("ClickUp", _list_clickup("channels"), _clickup_missing),
}


def _section_errors() -> dict[str, tuple[str, str]]:
    """Per Settings section that can't be edited yet: ("error", why Slack, Drive or ClickUp can't
    be reached) or ("loading", ...) while its list is still on its way."""
    out = {}
    for section in settings_form.SECTIONS:
        choices = [_choices[s] for s in section.sources if s in _choices]
        reason = next((c.error for c in choices if c.error), "")
        if reason:
            out[section.key] = ("error", reason)
        elif any(c.loading for c in choices):
            out[section.key] = ("loading", f"Loading the lists from {choices[0].service}. Reload the page in a moment.")
    return out


_unread = _Count(lambda store: store.count_unread())
_review = _Count(lambda store: len(store.search(SearchQuery(flagged=True, limit=1000))))


def _user(request: Request) -> auth.User:
    user = auth.current_user(request)
    if user is None:
        raise SignInRequired()
    return user


def _store():
    store = get_store()
    _ready(store)
    return store


def _page(request: Request, template: str, user: auth.User | None, **context) -> HTMLResponse:
    store = get_store()
    config = get_config()
    return templates.TemplateResponse(request, f"dashboard/{template}", {
        "user": user, "csrf": auth.csrf_token(request),
        "unread": _unread.get(store) if user else 0, "review_count": _review.get(store) if user else 0,
        "nav": template.split(".")[0], "message": request.query_params.get("msg", ""),
        "error": request.query_params.get("err", ""), "config": config,
        "company": config.company or "Eleven Content", "accent_css": branding.accent_css(config.branding.get("accent")),
        "logo_version": config.settings_version or 0, **context,
    })


def add_page(request: Request) -> HTMLResponse:
    """The web form at /ingest, in the dashboard's header when signed in."""
    return _page(request, "add.html", auth.current_user(request))


def _back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    joiner = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{joiner}{urlencode(params)}" if params else path, status_code=303)


async def _form(request: Request) -> dict[str, Any]:
    form = await request.form()
    if not auth.check_csrf(request, str(form.get("csrf", ""))):
        raise HTTPException(400, "This form has expired. Go back, reload the page and try again.")
    data: dict[str, Any] = {}
    for key, value in form.multi_items():
        if key in data:
            data[key] = [*data[key], value] if isinstance(data[key], list) else [data[key], value]
        else:
            data[key] = value
    return data


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [v.strip() for v in values if str(v).strip()]


def _names(value: str) -> list[str]:
    return [v.strip() for v in (value or "").replace("\n", ",").split(",") if v.strip()]


def _query_from(params) -> SearchQuery:
    page = max(int(params.get("page") or 1), 1)
    date = lambda k: datetime.fromisoformat(params[k]).replace(tzinfo=timezone.utc) if params.get(k) else None  # noqa: E731
    many = lambda k: _list(params.getlist(k))  # noqa: E731
    visibility = set(many("visibility"))
    return SearchQuery(
        text=params.get("q") or None,
        themes=many("theme"), narratives=many("narrative"),
        authors=many("author"), author_ids=many("person"),
        flags=many("flag"), source_types=many("source"), statuses=many("status"),
        # Both visibilities picked is the same as neither.
        visibility=visibility.pop() if len(visibility) == 1 else None,
        date_from=date("from"), date_to=date("to"), limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
    )


# --- Sign-in ---------------------------------------------------------------------

@router.get("/login")
def login(request: Request, next: str = "/dashboard") -> Response:
    """Where anyone not signed in lands. IAP signs people in before they reach the app, so
    getting here means IAP isn't in front of it, or isn't set up to tell the app who they are."""
    if auth.current_user(request):
        return RedirectResponse(next if next.startswith("/dashboard") else "/dashboard", status_code=303)
    config = get_config()
    return templates.TemplateResponse(request, "dashboard/login.html", {
        "csrf": auth.csrf_token(request), "company": config.company or "Eleven Content", "accent_css": "",
        "logo_version": config.settings_version or 0, "on_cloud_run": bool(os.environ.get("K_SERVICE")),
        "iap_header": bool(request.headers.get(auth.IAP_HEADER)),
    }, status_code=401)


@router.get("/logout")
def logout() -> Response:
    # IAP clears its own sign-in and asks again.
    return RedirectResponse("/dashboard?gcp-iap-mode=CLEAR_LOGIN_COOKIE", status_code=303)


# --- Activity --------------------------------------------------------------------

@router.get("")
def home(request: Request) -> Response:
    _user(request)
    config = get_config()
    if not config.company:
        # A new installation starts on Settings, then Taxonomy, until there's something to tag against.
        return _back("/dashboard/settings", msg="Welcome. Start with your company name and models, then add themes on Taxonomy.")
    if not config.themes:
        return _back("/dashboard/taxonomy", msg="Add the themes your company wants to be known for, so items can be tagged.")
    return RedirectResponse("/dashboard/activity", status_code=303)


@router.get("/activity")
def activity(request: Request) -> HTMLResponse:
    user, store = _user(request), _store()
    p = request.query_params
    page = max(int(p.get("page") or 1), 1)
    level = p.get("level") or None
    query = EventQuery(level=level if level in {"error", "warning"} else None, via=p.get("via") or None,
                       unread=p.get("unread") == "1", limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
                       kinds=["item_saved"] if level == "saved" else [])
    rows = store.list_events(query)
    counts = {"unread": store.count_unread(), "error": store.count_unread("error"),
              "warning": store.count_unread("warning")}
    return _page(request, "activity.html", user, events=rows, page=page, params=dict(p), counts=counts,
                 more=len(rows) == PAGE_SIZE, vias=["web", "api", "slack", "email", "drive", "clickup", "mcp", "cli", "dashboard"])


@router.post("/activity/read")
async def activity_read(request: Request) -> Response:
    _user(request)
    form = await _form(request)
    ids = _list(form.get("id"))
    _store().mark_read(ids or None)
    _unread.clear()
    return _back(str(form.get("back") or "/dashboard/activity"))


# --- Needs review ----------------------------------------------------------------

@router.get("/review")
def review(request: Request) -> HTMLResponse:
    user, store = _user(request), _store()
    flag = request.query_params.get("flag") or None
    flagged = store.search(SearchQuery(flagged=True, limit=500))
    counts = Counter(f for item in flagged for f in item.flags)
    found = [i for i in flagged if flag in i.flags] if flag else flagged
    return _page(request, "review.html", user, items=found, counts=counts, flag=flag,
                 people=[p.name for p in get_config().people], source_types=SOURCE_TYPES)


# --- Items -----------------------------------------------------------------------

@router.get("/items")
def items_page(request: Request) -> HTMLResponse:
    user, store = _user(request), _store()
    query = _query_from(request.query_params)
    found = store.search(query)
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k != "page"]
    filters = [(k, v) for k, v in pairs if k != "cols" and v]
    picked: dict[str, list[str]] = {}
    for k, v in filters:
        picked.setdefault(k, []).append(v)
    columns = [c for c in request.query_params.getlist("cols") if c in ITEM_COLUMNS] or DEFAULT_COLUMNS
    page = query.offset // PAGE_SIZE + 1
    return _page(request, "items.html", user, items=found, params=dict(filters), filters=filters, page=page,
                 picked=picked,
                 more=len(found) == PAGE_SIZE, query_string=urlencode(pairs), columns=columns,
                 all_columns=ITEM_COLUMNS, source_types=SOURCE_TYPES, statuses=STATUSES,
                 people=get_config().people, flags=FLAG_LABELS)


@router.get("/items/{row_id}")
def item_page(request: Request, row_id: str) -> HTMLResponse:
    user, store = _user(request), _store()
    item = store.get(row_id)
    if item is None:
        raise HTTPException(404, "No item with that ID.")
    history = store.list_events(EventQuery(row_id=row_id, limit=100))
    config = get_config()
    hidden = {"suggested_themes", "tagging_error", "note"}
    return _page(request, "item.html", user, item=item, history=history, source_types=SOURCE_TYPES,
                 statuses=STATUSES, visibilities=VISIBILITIES, themes=config.themes, narratives=config.narratives,
                 formats=config.formats, people=[p.name for p in config.people],
                 metadata={k: v for k, v in item.source_metadata.items() if k not in hidden})


@router.post("/items/{row_id}")
async def item_save(request: Request, row_id: str) -> Response:
    user, store = _user(request), _store()
    form = await _form(request)
    back = str(form.get("back") or f"/dashboard/items/{row_id}")
    fields: dict[str, Any] = {}
    if "authors" in form:
        fields["authors"] = _names(str(form["authors"]))
    for key in ("summary", "extracted_text", "status", "visibility", "source_type"):
        if key in form:
            fields[key] = str(form[key]).strip() if key != "extracted_text" else str(form[key])
    if form.get("has_tags"):
        fields["theme_tags"] = _list(form.get("theme_tags"))
        fields["narrative_tags"] = _list(form.get("narrative_tags"))
    current = store.get(row_id)
    if current is None:
        raise HTTPException(404, "No item with that ID.")
    if form.get("date_created"):
        date = datetime.fromisoformat(str(form["date_created"])).replace(tzinfo=timezone.utc)
        if date.date() != current.date_created.date():  # the form only has the day, so keep the time otherwise
            fields["date_created"] = date
    if "label" in form:
        metadata = dict(current.source_metadata)
        metadata["label"] = str(form["label"]).strip()
        fields["source_metadata"] = metadata
    try:
        item = await run_in_threadpool(items.update, store, get_config(), row_id, fields,
                                       via="dashboard", actor=user.actor)
    except ValueError as exc:
        return _back(back, err=str(exc))
    if item is None:
        raise HTTPException(404, "No item with that ID.")
    return _back(back, msg="Saved.")


@router.post("/items/{row_id}/retag")
async def item_retag(request: Request, row_id: str) -> Response:
    user, store = _user(request), _store()
    form = await _form(request)
    item = await run_in_threadpool(items.retag, store, get_config(), row_id, via="dashboard", actor=user.actor)
    back = str(form.get("back") or f"/dashboard/items/{row_id}")
    if item is None:
        raise HTTPException(404, "No item with that ID.")
    if "tagging_failed" in item.flags:
        return _back(back, err=f"Tagging failed again: {item.source_metadata.get('tagging_error', '')}")
    return _back(back, msg="Tagged again.")


@router.post("/items/{row_id}/deliverables")
async def item_deliverables(request: Request, row_id: str) -> Response:
    user, store = _user(request), _store()
    form = await _form(request)
    item = store.get(row_id)
    if item is None:
        raise HTTPException(404, "No item with that ID.")
    if form.get("remove") is not None:
        index = int(form["remove"])
        kept = [d for i, d in enumerate(item.deliverables) if i != index]
        items.update(store, get_config(), row_id, {"deliverables": kept}, via="dashboard", actor=user.actor)
        return _back(f"/dashboard/items/{row_id}", msg="Removed.")
    title = str(form.get("title", "")).strip()
    if not title:
        return _back(f"/dashboard/items/{row_id}", err="Give a title or link for the deliverable.")
    items.add_deliverable(store, get_config(), row_id, f"{form.get('format') or 'Other'}: {title}",
                          via="dashboard", actor=user.actor)
    return _back(f"/dashboard/items/{row_id}", msg="Deliverable added.")


@router.post("/items/{row_id}/dismiss-themes")
async def item_dismiss_themes(request: Request, row_id: str) -> Response:
    user, store = _user(request), _store()
    form = await _form(request)
    item = store.get(row_id)
    if item is None:
        raise HTTPException(404, "No item with that ID.")
    metadata = {k: v for k, v in item.source_metadata.items() if k != "suggested_themes"}
    items.update(store, get_config(), row_id, {"source_metadata": metadata}, via="dashboard", actor=user.actor)
    return _back(str(form.get("back") or f"/dashboard/items/{row_id}"), msg="Theme ideas dismissed.")


@router.post("/items/{row_id}/delete")
async def item_delete(request: Request, row_id: str) -> Response:
    user, store = _user(request), _store()
    form = await _form(request)
    if form.get("confirm") != "yes":
        return _back(f"/dashboard/items/{row_id}", err="Tick the box to confirm.")
    items.delete(store, row_id, via="dashboard", actor=user.actor)
    return _back("/dashboard/items", msg="Deleted.")


# --- People ----------------------------------------------------------------------

def _author_counts(store) -> Counter:
    return Counter(a for row in store.tag_rows() for a in row.authors)


def _person_names(person: Person) -> list[str]:
    return [person.name, *person.aliases]


@router.get("/people")
def people_page(request: Request) -> HTMLResponse:
    user, store = _user(request), _store()
    people = store.list_people()
    counts = _author_counts(store)
    known = {n.lower() for p in people for n in _person_names(p)}
    unmatched = sorted(((name, n) for name, n in counts.items() if name.lower() not in known and name != "Unknown"),
                       key=lambda x: -x[1])
    return _page(request, "people.html", user, people=people, counts=counts, unmatched=unmatched)


def _person_from_form(form: dict, existing: Person | None = None) -> Person:
    name = str(form.get("name", "")).strip()
    if not name:
        raise ValueError("A person needs a name.")
    data = existing.model_dump() if existing else {}
    data.update(name=name, role=str(form.get("role", "")).strip(), emails=_names(str(form.get("emails", ""))),
                slack_id=str(form.get("slack_id", "")).strip(), aliases=_names(str(form.get("aliases", ""))),
                responsible_for=_names(str(form.get("responsible_for", ""))),
                active=form.get("active") == "on" if existing else True)
    return Person(**data)


def _credit(store, person: Person, names: list[str], actor: str) -> int:
    """Credits every item by any of these names to the person: their name in `authors`,
    their ID in `author_ids`."""
    changed = 0
    for name in names:
        if name != person.name:
            changed += store.replace_in_lists("authors", name, person.name)
    for item in store.search(SearchQuery(authors=[person.name], limit=5000)):
        if person.id not in item.author_ids:
            store.update(item.row_id, {"author_ids": [*item.author_ids, person.id]})
    if changed:
        events.record(store, "person_changed", f"Credited {changed} items to {person.name}", via="dashboard",
                      actor=actor, person_id=person.id, names=names)
    return changed


@router.post("/people")
async def people_add(request: Request) -> Response:
    user, store = _user(request), _store()
    form = await _form(request)
    try:
        person = _person_from_form(form)
    except ValueError as exc:
        return _back("/dashboard/people", err=str(exc))
    settings_store.save_person(store, person, actor=user.actor)
    await run_in_threadpool(_credit, store, person, _person_names(person), user.actor)
    return _back(f"/dashboard/people/{person.id}", msg=f"Added {person.name}.")


@router.post("/people/unmatched")
async def people_unmatched(request: Request) -> Response:
    """An author name that matches no one: add them as a person, or say who they are."""
    user, store = _user(request), _store()
    form = await _form(request)
    name = str(form.get("name", "")).strip()
    target = str(form.get("person_id", ""))
    if not name:
        return _back("/dashboard/people")
    if target:
        person = next((p for p in store.list_people() if p.id == target), None)
        if person is None:
            return _back("/dashboard/people", err="That person no longer exists.")
        if name.lower() not in {a.lower() for a in _person_names(person)}:
            person = person.model_copy(update={"aliases": [*person.aliases, name]})
            settings_store.save_person(store, person, actor=user.actor, before=person)
        count = await run_in_threadpool(_credit, store, person, [name], user.actor)
        return _back("/dashboard/people", msg=f"{name} is now {person.name} ({count} items).")
    person = Person(name=name)
    settings_store.save_person(store, person, actor=user.actor)
    await run_in_threadpool(_credit, store, person, [name], user.actor)
    return _back(f"/dashboard/people/{person.id}", msg=f"Added {name}. Fill in their details.")


@router.get("/people/{person_id}")
def person_page(request: Request, person_id: str) -> HTMLResponse:
    user, store = _user(request), _store()
    person = next((p for p in store.list_people() if p.id == person_id), None)
    if person is None:
        raise HTTPException(404, "No such person.")
    by_id = store.search(SearchQuery(author_ids=[person.id], limit=500))
    by_name = store.search(SearchQuery(authors=_person_names(person), limit=500))
    found = list({i.row_id: i for i in [*by_id, *by_name]}.values())
    found.sort(key=lambda i: i.date_created, reverse=True)
    themes = Counter(t for i in found for t in i.theme_tags)
    narratives = Counter(n for i in found for n in i.narrative_tags)
    return _page(request, "person.html", user, person=person, items=found, themes=themes, narratives=narratives)


@router.post("/people/{person_id}")
async def person_save(request: Request, person_id: str) -> Response:
    user, store = _user(request), _store()
    form = await _form(request)
    before = next((p for p in store.list_people() if p.id == person_id), None)
    if before is None:
        raise HTTPException(404, "No such person.")
    if form.get("delete") == "yes":
        store.delete_person(person_id)
        events.record(store, "person_changed", f"Removed person {before.name}", via="dashboard", actor=user.actor)
        get_config.cache_clear()
        return _back("/dashboard/people", msg=f"Removed {before.name}. Their items keep their name.")
    try:
        person = _person_from_form(form, before)
    except ValueError as exc:
        return _back(f"/dashboard/people/{person_id}", err=str(exc))
    settings_store.save_person(store, person, actor=user.actor, before=before)
    if person.name != before.name:
        await run_in_threadpool(_credit, store, person, [before.name], user.actor)
    return _back(f"/dashboard/people/{person_id}", msg="Saved.")


# --- Taxonomy --------------------------------------------------------------------

@router.get("/taxonomy")
def taxonomy_page(request: Request) -> HTMLResponse:
    user, store = _user(request), _store()
    rows = store.tag_rows()
    theme_counts = Counter(t for r in rows for t in r.theme_tags)
    narrative_counts = Counter(n for r in rows for n in r.narrative_tags)
    ideas = Counter()
    for item in store.search(SearchQuery(flags=["theme_ideas"], limit=1000)):
        ideas.update(item.source_metadata.get("suggested_themes", []))
    current = settings_store.current(store)
    return _page(request, "taxonomy.html", user, current=current, theme_counts=theme_counts,
                 narrative_counts=narrative_counts, ideas=ideas.most_common(30))


def _save_settings(store, data: dict, user: auth.User, note: str) -> None:
    settings_store.save(store, data, actor=user.actor, note=note)


@router.post("/taxonomy/{kind}")
async def taxonomy_save(request: Request, kind: str) -> Response:
    """Add, edit, rename or remove a theme or narrative, or replace the formats."""
    user, store = _user(request), _store()
    form = await _form(request)
    data = settings_store.current(store)
    if kind == "formats":
        data["formats"] = [f.strip() for f in str(form.get("formats", "")).splitlines() if f.strip()]
        _save_settings(store, data, user, "Changed the formats")
        return _back("/dashboard/taxonomy", msg="Formats saved.")
    if kind not in {"themes", "narratives"}:
        raise HTTPException(404)
    column = "theme_tags" if kind == "themes" else "narrative_tags"
    singular = kind[:-1]
    tags = [dict(t) for t in data.get(kind, [])]
    old = str(form.get("old", "")).strip()
    name = str(form.get("name", "")).strip()
    description = str(form.get("description", "")).strip()
    existing = next((t for t in tags if t["name"] == old), None) if old else None

    if form.get("action") == "remove" and existing:
        tags.remove(existing)
        data[kind] = tags
        _save_settings(store, data, user, f"Removed {singular} {old}")
        removed = 0
        if form.get("from_items") == "on":
            removed = await run_in_threadpool(store.replace_in_lists, column, old, None)
        return _back("/dashboard/taxonomy", msg=f"Removed {old}" + (f" and took it off {removed} items." if removed else "."))
    if not name:
        return _back("/dashboard/taxonomy", err=f"A {singular} needs a name.")
    if name != old and any(t["name"] == name for t in tags):
        return _back("/dashboard/taxonomy", err=f"There's already a {singular} called {name}.")
    if existing:
        existing.update(name=name, description=description)
        data[kind] = tags
        note = f"Renamed {singular} {old} to {name}" if name != old else f"Edited {singular} {name}"
        _save_settings(store, data, user, note)
        renamed = await run_in_threadpool(store.replace_in_lists, column, old, name) if name != old else 0
        return _back("/dashboard/taxonomy", msg=note + (f", on {renamed} items." if renamed else "."))
    tags.append({"name": name, "description": description})
    data[kind] = tags
    _save_settings(store, data, user, f"Added {singular} {name}")
    if form.get("from_idea") == "on":
        # The tagger suggested it: the items that suggested it can be re-tagged to pick it up.
        return _back("/dashboard/review?flag=theme_ideas", msg=f"Added {name}. Re-tag the items that suggested it.")
    return _back("/dashboard/taxonomy", msg=f"Added {name}.")


# --- Coverage --------------------------------------------------------------------

@router.get("/coverage")
def coverage_page(request: Request) -> HTMLResponse:
    user, store = _user(request), _store()
    config = get_config()
    visibility = request.query_params.get("visibility") or None
    days = int(request.query_params.get("days") or 30)
    grid = analytics.coverage(store, config, visibility)
    cells = {(c["theme"], c["narrative"]): c for c in grid["cells"]}
    stats = [(grid["total_items"], "Items"), (grid["untagged_items"], "Without a theme"),
             (len(grid["material_but_no_content"]), "Pairs with material, nothing made"),
             (len(grid["no_material"]), "Pairs with no material")]
    return _page(request, "coverage.html", user, grid=grid, cells=cells, stats=stats, themes=[t.name for t in config.themes],
                 narratives=[n.name for n in config.narratives], visibility=visibility,
                 trends=analytics.trends(store, days), days=days)


# --- Settings --------------------------------------------------------------------

@router.get("/settings")
def settings_page(request: Request) -> HTMLResponse:
    user, store = _user(request), _store()
    current = settings_store.current(store)
    sources = current.get("sources", {})
    focus = request.query_params.get("focus", "")
    saved = {"saved_section": focus.split("|")[0], "saved_message": request.query_params.get("msg", ""), "message": ""} \
        if focus and request.query_params.get("msg") else {}
    config = get_config()
    model = lambda m: {"provider": m.provider, "model": m.model, "env": m.api_key_env,  # noqa: E731
                       "key": bool(os.environ.get(m.api_key_env))} if m else {"provider": "", "model": ""}
    models = {"llm": model(config.llm), "transcription": model(config.transcription)}
    from .defaults import AUDIO_MODELS, MODELS

    return _page(request, "settings.html", user, current=current, models=models, model_defaults=MODELS,
                 audio_defaults=AUDIO_MODELS, **saved,
                 sections=settings_form.SECTIONS, values=settings_form.values(sources),
                 visibility_defaults=sources.get("default_visibility", {}), source_types=SOURCE_TYPES,
                 default_prompt=DEFAULT_INSTRUCTIONS, default_accent=DEFAULT_ACCENT, pick={k: c.get() for k, c in _choices.items()},
                 access_errors=_section_errors(), emoji=EMOJI,
                 reader_model=get_config().reader.model,
                 settings_version=get_config().settings_version)


@router.post("/settings")
async def settings_save(request: Request) -> Response:
    user, store = _user(request), _store()
    form = await _form(request)
    data = settings_store.current(store)
    data["company"] = str(form.get("company", "")).strip()
    if "llm_provider" in form:
        data["llm"] = {**(data.get("llm") or {}), "provider": str(form["llm_provider"]),
                       "model": str(form.get("llm_model", "")).strip()}
        audio = str(form.get("transcription_provider", "")).strip()
        data["transcription"] = {"provider": audio, "model": str(form.get("transcription_model", "")).strip()} \
            if audio else None
    prompt = str(form.get("prompt", "")).strip()
    data["prompts"] = {**data.get("prompts", {}), "tagging": prompt} if prompt else \
        {k: v for k, v in data.get("prompts", {}).items() if k != "tagging"}
    picked = {k: c.get() for k, c in _choices.items()}
    names = {k: {c["id"]: c["name"] for c in v} for k, v in picked.items() if v}
    # Sections that Slack or Drive can't be reached for are shown greyed out and left as they are.
    posted = form.get("sections", [])
    blocked = _section_errors()
    form["sections"] = [s for s in (posted if isinstance(posted, list) else [posted]) if s not in blocked]
    try:
        sources = settings_form.read(form, data.get("sources", {}), names)
    except ValueError as exc:
        return _back("/dashboard/settings", err=str(exc))
    sources["default_visibility"] = {t: form.get(f"visibility_{t}", "private") for t in SOURCE_TYPES}
    data["sources"] = sources
    try:
        data["branding"] = await _branding(form, dict(data.get("branding") or {}))
    except ValueError as exc:
        return _back("/dashboard/settings", err=str(exc))
    try:
        settings_store.save(store, data, actor=user.actor)
    except ValueError as exc:
        return _back("/dashboard/settings", err=str(exc))
    notes = _join_channels(sources.get("slack", {}).get("channels") or [], picked["slack"], store)
    message = " ".join(["Saved. Running services pick it up within a minute.", *notes])
    focus = form.get("focus")
    focus = (focus[-1] if isinstance(focus, list) else focus or "").strip()
    if focus:
        # Saved from one channel, folder or place: back to it, with the message in its section.
        query = urlencode({"focus": focus, "msg": message})
        return RedirectResponse(f"/dashboard/settings?{query}#section-{focus.split('|')[0]}", status_code=303)
    return _back("/dashboard/settings", msg=message)


async def _branding(form: dict, current: dict) -> dict:
    """The logo and accent colour after this save: a new upload, a reset, and the colour picked."""
    upload = form.get("logo")
    if form.get("logo_reset"):
        current.pop("logo", None)
    elif getattr(upload, "filename", ""):
        current["logo"] = branding.read_logo(await upload.read(), upload.filename)
    # The theme's own colour picked again is the same as no colour of your own.
    colour = branding.valid_colour(str(form["accent"])) if "accent" in form else DEFAULT_ACCENT
    if colour == DEFAULT_ACCENT:
        current.pop("accent", None)
    else:
        current["accent"] = colour
    return current


def _join_channels(channels: list[dict], visible: list[dict] | None, store) -> list[str]:
    """Adds the bot to the chosen public channels it isn't in yet. (Private channels are only
    listed once it's been invited.)"""
    if visible is None:
        return []
    from .connectors.slack import join_channel

    seen = {c["id"]: c for c in visible}
    joined = []
    for channel in channels:
        found = seen.get(channel["id"])
        if not found or found["member"] or found["private"]:
            continue
        try:
            join_channel(found["id"])
            joined.append(f"#{found['name']}")
        except Exception as exc:  # noqa: BLE001
            events.problem(store, f"The Slack bot couldn't join #{found['name']}: {exc}", via="dashboard",
                           key=f"slack-join:{found['id']}")
    if joined:
        _choices["slack"].clear()
    return [f"The bot joined {', '.join(joined)}."] if joined else []


# --- Static ----------------------------------------------------------------------

def _static(name: str, media_type: str) -> Response:
    text = resources.files("tle.static").joinpath(name).read_text()
    return Response(text, media_type=media_type, headers={"Cache-Control": "max-age=300"})


@router.get("/dashboard.css")
def stylesheet() -> Response:
    return _static("dashboard.css", "text/css")


@router.get("/select.js")
def select_script() -> Response:
    return _static("select.js", "text/javascript")


@router.get("/emoji.js")
def emoji_script() -> Response:
    return _static("emoji.js", "text/javascript")


def _theme_file(name: str) -> Response:
    """The logo uploaded in Settings, or the packaged default."""
    if name.startswith("logo-"):
        uploaded = branding.logo(get_config().branding)
        if uploaded:
            return Response(uploaded[0], media_type=uploaded[1],
                            headers={"Cache-Control": "max-age=300", **branding.LOGO_HEADERS})
    data = resources.files("tle.static").joinpath(name).read_bytes()
    return Response(data, media_type=THEME_FILES[name], headers={"Cache-Control": "max-age=300"})


for _name in THEME_FILES:
    router.add_api_route(f"/{_name}", lambda name=_name: _theme_file(name), methods=["GET"])
