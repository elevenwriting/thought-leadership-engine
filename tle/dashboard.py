# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""The dashboard: browse and fix items, see what happened, and change settings.

Pages are rendered on the server from tle/templates/dashboard. Every form posts back to a
route here and is redirected afterwards, so there's no JavaScript build step."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from markupsafe import Markup
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import analytics, auth, events, items, settings_form, syncs, topics, views
from . import settings as settings_store
from .config import SOURCE_TYPES, SOURCES, STATUSES, TYPE_LABELS, VISIBILITIES, get_config, on_cloud_run
from .emoji import EMOJI
from .models import FLAG_LABELS, Person
from .store import EventQuery, SearchQuery, get_store
from .tagger import DEFAULT_INSTRUCTIONS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", include_in_schema=False)
templates = Jinja2Templates(directory=str(resources.files("tle").joinpath("templates")))
PAGE_SIZES = (10, 20, 50)  # Items per page, picked on the Items toolbar
PAGE_SIZE = 20
# Columns the Items table can show, and the ones it shows unless the Columns menu says otherwise.
ITEM_COLUMNS = {"date": "Date", "source": "Source", "type": "Type", "themes": "Themes", "narratives": "Narratives",
                "people": "People", "visibility": "Visibility", "status": "Status", "made": "Made", "saved": "Saved"}
DEFAULT_COLUMNS = ["date", "source", "type", "themes", "narratives", "visibility", "status", "made"]
# The theme's files; the logo can be replaced in Settings.
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
    "edit": '<path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3z"/><path d="m14 8 3 3"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
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


def _ago(value: datetime | None) -> str:
    """"just now", "4 minutes ago", "3 hours ago", or the date for anything older than a day."""
    if not value:
        return ""
    seconds = (datetime.now(timezone.utc) - value).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return _date(value, True)


templates.env.filters["ago"] = _ago
templates.env.filters["show"] = lambda v: ", ".join(map(str, v)) if isinstance(v, list) else ("—" if v in (None, "") else str(v)[:120])
templates.env.filters["flag"] = lambda f: FLAG_LABELS.get(f, f)
templates.env.filters["via"] = lambda v: SOURCES.get(v, (v or "Unknown").capitalize())
templates.env.filters["kind"] = lambda t: TYPE_LABELS.get(t, (t or "").capitalize())
templates.env.filters["heat"] = _heat
templates.env.filters["initials"] = _initials
templates.env.filters["signed"] = _signed_change
templates.env.globals["quote"] = quote
templates.env.globals["icon"] = _icon




# time.monotonic() counts from when the machine started, and a new Cloud Run instance has only
# just started, so "long ago" must not be 0: that would read as "a moment ago" for its first minutes.
NEVER = float("-inf")


class _Count:
    """A count shown in the menu, cached briefly so each page isn't an extra query."""

    def __init__(self, count):
        self.count, self.value, self.at = count, 0, NEVER

    def get(self, store) -> int:
        if time.monotonic() - self.at > 20:
            try:
                self.value = self.count(store)
            except Exception:  # noqa: BLE001
                log.exception("Couldn't count for the menu")
            self.at = time.monotonic()
        return self.value

    def clear(self) -> None:
        self.at = NEVER


class _Choices:
    """Slack channels, Drive folders or ClickUp places for the Settings pickers. They're fetched
    in the background and kept for ten minutes, so a slow or rate-limited service never holds
    the page up: it shows the last list, or says it's loading, and the page's script swaps the
    section in once the list is there. `error` says why there's no list."""

    TTL = 600

    def __init__(self, service: str, fetch, missing):
        self.service, self.fetch, self.missing = service, fetch, missing
        self.value: list[dict] | None = None
        self.error = ""
        self.busy = False
        self.at: float | None = None  # when the list was last fetched; None: never
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def get(self) -> list[dict] | None:
        with self.lock:
            missing = self.missing()
            if missing:
                self.value, self.error = None, missing
                return None
            if self.thread and self.thread.is_alive() and time.monotonic() - self.started > self.STUCK:
                self._give_up()
            if (self.at is None or time.monotonic() - self.at > self.TTL) and not (self.thread and self.thread.is_alive()):
                self.thread = threading.Thread(target=self._refresh, daemon=True)
                self.started = time.monotonic()
                self.thread.start()
            return self.value

    @property
    def loading(self) -> bool:
        return self.at is None and bool(self.thread and self.thread.is_alive())

    RETRY_AFTER_ERROR = 60
    STUCK = 60  # a fetch running longer than this is abandoned, and tried again after RETRY_AFTER_ERROR
    started = 0.0

    def _give_up(self) -> None:
        """Called with the lock held. Logs where the fetch is stuck, so the cause can be found, and
        shows an error instead of loading forever. The stuck thread is left to finish or not."""
        import traceback

        frame = sys._current_frames().get(self.thread.ident) if self.thread else None
        where = "".join(traceback.format_stack(frame)) if frame else "(no stack)"
        log.warning("Listing %s for Settings has taken over %ds; giving up. It was at:\n%s",
                    self.service, self.STUCK, where)
        self.thread = None
        self.value, self.busy = None, False
        self.error = f"{self.service} took over a minute to answer. Refresh to try again."
        self.at = time.monotonic() - (self.TTL - self.RETRY_AFTER_ERROR)

    def _refresh(self) -> None:
        busy = False
        try:
            value, error = self.fetch(), ""
        except Exception as exc:  # noqa: BLE001
            log.warning("Couldn't list %s for Settings: %s", self.service, exc)
            value, error = None, _access_error(self.service, exc)
            # Asked to slow down (a rate limit): not a fault, just try again shortly.
            busy = "rate limit" in str(exc).lower() or "429" in str(exc)
        with self.lock:
            self.value, self.error, self.busy = value, ("" if busy else error), busy
            # A failure is tried again after a minute rather than kept for the full TTL.
            self.at = time.monotonic() - (self.TTL - self.RETRY_AFTER_ERROR if (error or busy) else 0)

    def clear(self) -> None:
        """Fetch again on the next page, still showing the current list meanwhile."""
        with self.lock:
            if self.at is not None:
                self.at = time.monotonic() - self.TTL - 1

    def reset(self) -> None:
        """Forget the list and fetch it afresh, showing it as loading meanwhile."""
        with self.lock:
            self.value, self.error, self.busy, self.at = None, "", False, None


def _access_error(service: str, exc: Exception) -> str:
    """A short reason, e.g. "Drive error: Request had insufficient authentication scopes."."""
    from .secrets import redact

    response = getattr(exc, "response", None)
    if isinstance(getattr(response, "data", None), dict):  # Slack
        return redact(f"{service} error: {response.data.get('error', exc)}")
    return redact(f"{service} error: {getattr(exc, 'reason', None) or exc}")


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


_service_account_email: list[str] = []


def service_account() -> str:
    """The Google account the engine runs as, which Drive folders are shared with. On Cloud Run
    it's the service's own account; when developing, the one gcloud acts as, if any."""
    if not _service_account_email:
        email = ""
        try:
            if on_cloud_run():
                email = httpx.get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                                  headers={"Metadata-Flavor": "Google"}, timeout=2).text.strip()
            elif not _drive_missing():
                import google.auth

                credentials, _ = google.auth.default()
                email = getattr(credentials, "service_account_email", "") or ""
        except Exception as exc:  # noqa: BLE001
            log.info("Couldn't tell which account the engine runs as: %s", exc)
        if "@" not in email:
            return ""
        _service_account_email.append(email)
    return _service_account_email[0]


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


def _section_errors(pick: dict[str, list | None] | None = None) -> dict[str, tuple[str, str]]:
    """Per Settings section that can't be edited yet: ("error", why Slack, Drive or ClickUp can't
    be reached) or ("loading", ...) while its list is still on its way. Given the lists the page
    is drawn with, a section whose list wasn't there yet counts as loading, even if it arrived
    a moment later, so the page never draws it without its list."""
    out = {}
    for section in settings_form.SECTIONS:
        choices = [_choices[s] for s in section.sources if s in _choices]
        not_drawn = pick is not None and any(pick.get(s) is None for s in section.sources if s in _choices)
        reason = next((c.error for c in choices if c.error), "")
        if reason:
            out[section.key] = ("error", reason)
        elif any(c.busy for c in choices):
            out[section.key] = ("loading", f"{choices[0].service} asked us to slow down, so this takes a little longer. "
                                           "It appears here by itself.")
        elif any(c.loading for c in choices) or not_drawn:
            out[section.key] = ("loading", f"Loading lists from {choices[0].service}…")
    return out


# The menu counts unread problems only, so the number means something needs attention.
_unread = _Count(lambda store: store.count_unread(level="error"))
_review = _Count(lambda store: len(store.search(SearchQuery(flagged=True, limit=1000))))


def _user(request: Request) -> auth.User:
    user = auth.current_user(request)
    if user is None:
        raise SignInRequired()
    return user


def _store():
    return get_store()


def _page(request: Request, template: str, user: auth.User | None, **context) -> HTMLResponse:
    store = get_store()
    config = get_config()
    return templates.TemplateResponse(request, f"dashboard/{template}", {
        "user": user, "csrf": auth.csrf_token(request),
        "unread": _unread.get(store) if user else 0, "review_count": _review.get(store) if user else 0,
        "nav": template.split(".")[0], "message": request.query_params.get("msg", ""),
        "error": request.query_params.get("err", ""), "config": config,
        "company": config.company or "", **context,
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


def _per_page(params) -> int:
    per = params.get("per") or ""
    return int(per) if per.isdigit() and int(per) in PAGE_SIZES else PAGE_SIZE


def _query_from(params) -> SearchQuery:
    page = max(int(params.get("page") or 1), 1)
    per = _per_page(params)
    date = lambda k: datetime.fromisoformat(params[k]).replace(tzinfo=timezone.utc) if params.get(k) else None  # noqa: E731
    many = lambda k: _list(params.getlist(k))  # noqa: E731
    visibility = set(many("visibility"))
    return SearchQuery(
        text=params.get("q") or None,
        themes=many("theme"), narratives=many("narrative"),
        authors=many("author"), author_ids=many("person"),
        flags=many("flag"), source_types=many("type"), vias=many("source"), statuses=many("status"),
        reading=params.get("view") == "reading",
        # Both visibilities picked is the same as neither.
        visibility=visibility.pop() if len(visibility) == 1 else None,
        date_from=date("from"), date_to=date("to"), limit=per, offset=(page - 1) * per,
        sort="added" if params.get("sort") == "added" else "said",
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
        "csrf": auth.csrf_token(request), "company": config.company or "",
        "on_cloud_run": bool(os.environ.get("K_SERVICE")),
        "iap_header": bool(request.headers.get(auth.IAP_HEADER)),
    }, status_code=401)


def unavailable_page(request: Request, reason: str) -> HTMLResponse:
    """Shown when the database can't be reached. Uses nothing that needs the database."""
    return templates.TemplateResponse(request, "dashboard/unavailable.html", {
        "csrf": "", "company": "Expertise Database", "reason": reason,
    }, status_code=503)


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
    return RedirectResponse("/dashboard/items", status_code=303)


# Activity and Needs review used to be pages of their own; they're views on Items now.

@router.get("/activity")
def activity() -> Response:
    return RedirectResponse("/dashboard/items?view=problems", status_code=303)


@router.get("/review")
def review(request: Request) -> Response:
    flag = request.query_params.get("flag")
    return RedirectResponse("/dashboard/items?view=review" + (f"&flag={quote(flag)}" if flag else ""), status_code=303)


@router.post("/problems/dismiss")
async def problems_dismiss(request: Request) -> Response:
    """Dismiss one problem, or all of them without an id."""
    _user(request)
    form = await _form(request)
    ids = _list(form.get("id"))
    _store().mark_read(ids or None)
    _unread.clear()
    return _back("/dashboard/items?view=problems", msg="Dismissed." if ids else "All dismissed.")


# --- Items -----------------------------------------------------------------------

@router.get("/items")
def items_page(request: Request) -> HTMLResponse:
    user, store = _user(request), _store()
    view = request.query_params.get("view")
    view = view if view in ("review", "problems", "reading") else "all"
    flagged = store.search(SearchQuery(flagged=True, limit=500))
    totals = {"review_total": len(flagged), "problem_total": store.count_unread(level="error"), "view": view,
              "reading_total": len(store.search(SearchQuery(reading=True, limit=1000)))}
    if view == "review":
        flag = request.query_params.get("flag") or None
        return _page(request, "items.html", user, **totals, flag=flag,
                     flag_counts=Counter(f for item in flagged for f in item.flags),
                     review_items=[i for i in flagged if flag in i.flags] if flag else flagged,
                     people_names=[p.name for p in get_config().people], source_types=SOURCE_TYPES)
    if view == "problems":
        problems = store.list_events(EventQuery(level="error", unread=True, limit=200))
        return _page(request, "items.html", user, **totals, problems=problems)
    query = _query_from(request.query_params)
    found = store.search(query)
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k != "page"]
    filters = [(k, v) for k, v in pairs if k not in ("cols", "sort", "per") and v]
    picked: dict[str, list[str]] = {}
    for k, v in filters:
        picked.setdefault(k, []).append(v)
    columns = [c for c in request.query_params.getlist("cols") if c in ITEM_COLUMNS] or DEFAULT_COLUMNS
    if query.sort == "added" and "saved" not in columns:
        columns = [*columns, "saved"]  # the order the list is in, beside when it was said
    # The same list sorted the other way: everything but the page and the sort kept.
    unsorted = [(k, v) for k, v in pairs if k != "sort"]
    sort_links = {"said": urlencode(unsorted), "added": urlencode([*unsorted, ("sort", "added")])}
    per = query.limit
    page = query.offset // per + 1
    # Changing how many show starts again from the first page, keeping everything else.
    keep = [(k, v) for k, v in pairs if k != "per"]
    return _page(request, "items.html", user, **totals, items=found, params=dict(filters), filters=filters, page=page,
                 picked=picked,
                 more=len(found) == per, query_string=urlencode(pairs), columns=columns,
                 sort=query.sort, sort_links=sort_links, per=per, page_sizes=PAGE_SIZES, keep=keep,
                 all_columns=ITEM_COLUMNS, source_types=SOURCE_TYPES, statuses=STATUSES, sources=SOURCES,
                 people=get_config().people, flags=FLAG_LABELS)


@router.get("/items/{row_id}")
def item_page(request: Request, row_id: str) -> HTMLResponse:
    user, store = _user(request), _store()
    item = store.get(row_id)
    if item is None:
        raise HTTPException(404, "No item with that ID.")
    history = store.list_events(EventQuery(row_id=row_id, limit=100))
    config = get_config()
    hidden = {"suggested_themes", "tagging_error", "note", "links", "messages", "topics_error", "place", "views", "views_error", "notes", "record", "notes_from", "drive_modified"}
    return _page(request, "item.html", user, item=item, history=history, source_types=SOURCE_TYPES,
                 session_topics=_session_topics(item, store) if topics.is_session(item) else None,
                 topic_candidates=topics.candidates(item, store) if topics.is_session(item) else [],
                 related=_related(item, store),
                 statuses=STATUSES, visibilities=VISIBILITIES, themes=config.themes, narratives=config.narratives,
                 formats=config.formats, people=[p.name for p in config.people],
                 metadata={k: v for k, v in item.source_metadata.items() if k not in hidden})


def _related(item, store) -> list[dict]:
    """Items linked to this one as related (a doc and the chat it was shared in, say)."""
    me = f"item:{item.row_id}"
    out = []
    for link in store.links_for([me]):
        if link.kind != "related":
            continue
        other_end = link.to_id if link.from_id == me else link.from_id
        kind, _, other_id = other_end.partition(":")
        other = store.get(other_id) if kind == "item" else None
        if other:
            out.append({"link": link, "item": other, "outgoing": link.from_id == me})
    return out


def _preview(block: str, chars: int = 60) -> str:
    """A message's first words, for picking it from a list: "Jane Smith: Should the playbook…"."""
    first = block.splitlines()[0] if block else ""
    match = re.match(r"#\d+ (.+?) \([^)]*\): (.*)", first)
    who, said = (match.group(1), match.group(2)) if match else ("", first)
    said = said if len(said) <= chars else said[:chars].rsplit(" ", 1)[0] + "…"
    return f"{who}: {said}" if who else said


def _session_topics(item, store) -> list[dict]:
    """A chat session's topics for its page: each with its messages and the chain it's on."""
    text = topics.blocks(item)
    numbers = {m["id"]: m["n"] for m in item.source_metadata.get("messages") or [] if not m.get("reply_to")}
    out = []
    pulled = views.by_topic([item.row_id], store)
    for topic in store.topics_for([item.row_id]):
        others = [t for t in topics.chain(topic.id, store) if t.id != topic.id]
        ns = [numbers[i] for i in topic.messages if i in numbers]
        out.append({"topic": topic, "numbers": ns, "text": "\n".join(text[n] for n in ns if n in text),
                    "before": [t for t in others if t.date_created < topic.date_created],
                    "after": [t for t in others if t.date_created >= topic.date_created],
                    "messages": [(i, numbers[i], _preview(text.get(numbers[i], ""))) for i in topic.messages if i in numbers],
                    "links": topics.direct_links(topic, store),
                    "views": [(v, [numbers[i] for i in v.messages if i in numbers]) for v in pulled.get(topic.id, [])]})
    return out


@router.post("/items/{row_id}/topics")
async def item_topics(request: Request, row_id: str) -> Response:
    """Corrections to a chat session's topics: rename one, move a message, link or unlink."""
    user, store = _user(request), _store()
    form = await _form(request)
    item = store.get(row_id)
    if item is None or not topics.is_session(item):
        raise HTTPException(404, "No chat session with that ID.")
    back = f"/dashboard/items/{row_id}"
    action = str(form.get("action") or "")
    topic_id = str(form.get("topic") or "")
    try:
        if action == "rename":
            topics.rename(item, topic_id, str(form.get("title") or ""), str(form.get("summary") or ""), user.actor, store)
            msg = "Topic saved."
        elif action == "move":
            topics.move(item, str(form.get("message") or ""), str(form.get("to") or ""), user.actor, store,
                        title=str(form.get("new_title") or ""))
            msg = "Message moved."
        elif action == "link":
            topics.link(item, topic_id, str(form.get("to") or ""), user.actor, store)
            msg = "Linked."
        elif action == "unlink":
            topics.unlink(item, str(form.get("link") or ""), user.actor, store)
            msg = "Link removed."
        else:
            raise ValueError("Unknown change.")
    except ValueError as exc:
        return _back(back, err=str(exc))
    events.record(store, "item_updated", f"Topics: {msg[:-1].lower()} on {items.label(item)}", via="dashboard",
                  row_id=row_id, actor=user.actor)
    return _back(back, msg=msg)


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
    if form.get("has_reading"):
        fields["reading"] = bool(form.get("reading"))
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
    if "label" in form or "title" in form:
        metadata = dict(current.source_metadata)
        if "label" in form:
            metadata["label"] = str(form["label"]).strip()
        if "title" in form and str(form["title"]).strip():
            metadata["title"] = str(form["title"]).strip()
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
    if topics.is_session(item) and (item.source_metadata.get("topics_error") or not store.topics_for([row_id])):
        try:
            await run_in_threadpool(topics.find, item, get_config(), store)
        except Exception as exc:  # noqa: BLE001
            return _back(back, err=f"Finding the topics failed again: {exc}")
    if views.wanted(item):  # after corrections, this brings the views in line with the topics
        try:
            await run_in_threadpool(views.pull, store.get(row_id) or item, get_config(), store)
        except Exception as exc:  # noqa: BLE001
            return _back(back, err=f"Pulling out the views failed: {exc}")
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
    person = Person(**data)
    if not person.slack_id and person.emails and os.environ.get("SLACK_BOT_TOKEN"):
        # No need to look it up by hand: Slack finds it from their email.
        from .connectors.slack import member_id

        person.slack_id = next((found for email in person.emails if (found := member_id(email))), "") or ""
    return person


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


@router.post("/taxonomy")
async def taxonomy_save_all(request: Request) -> Response:
    """Everything on the Taxonomy page at once: edits, renames (carried onto items), removals
    (taken off items when asked), additions, and the formats. Nothing is saved if any of it is wrong."""
    user, store = _user(request), _store()
    form = await _form(request)
    data = settings_store.current(store)
    renames: list[tuple[str, str, str]] = []
    removals: list[tuple[str, str]] = []
    for kind in ("themes", "narratives"):
        column = "theme_tags" if kind == "themes" else "narrative_tags"
        rows = settings_form.form_rows(form, kind)
        if not rows:
            continue
        tags, seen = [], set()
        for row in rows:
            old, name = row.get("old", "").strip(), row.get("name", "").strip()
            if row.get("remove") == "on":
                if old and row.get("from_items") == "on":
                    removals.append((column, old))
                continue
            if not name:
                if old:
                    return _back("/dashboard/taxonomy", err=f"{old} needs a name. Nothing was saved.")
                continue  # an empty new row
            if name.lower() in seen:
                return _back("/dashboard/taxonomy", err=f"There are two {kind} called {name}. Nothing was saved.")
            seen.add(name.lower())
            tags.append({"name": name, "description": row.get("description", "").strip()})
            if old and name != old:
                renames.append((column, old, name))
        data[kind] = tags
    if "formats" in form:
        data["formats"] = [f.strip() for f in str(form["formats"]).splitlines() if f.strip()]
    _save_settings(store, data, user, "Taxonomy")
    touched = 0
    for column, old, new in renames:
        touched += await run_in_threadpool(store.replace_in_lists, column, old, new)
    for column, old in removals:
        touched += await run_in_threadpool(store.replace_in_lists, column, old, None)
    return _back("/dashboard/taxonomy", msg="Saved." + (f" Updated {touched} items." if touched else ""))


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
        return _back("/dashboard/items?view=review&flag=theme_ideas", msg=f"Added {name}. Re-tag the items that suggested it.")
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

@router.get("/sources")
def sources_page(request: Request) -> HTMLResponse:
    """Where the database reads from: Slack, Drive, ClickUp, email, files, and default visibility."""
    return _settings_page(request, "sources")


@router.get("/settings")
def settings_page(request: Request) -> HTMLResponse:
    """Company, models and tagging instructions."""
    return _settings_page(request, "settings")


def _settings_page(request: Request, page: str) -> HTMLResponse:
    user, store = _user(request), _store()
    current = settings_store.current(store)
    sources = current.get("sources", {})
    focus = request.query_params.get("focus", "")
    saved = {"saved_section": focus.split("|")[0], "saved_message": request.query_params.get("msg", ""), "message": ""} \
        if focus and request.query_params.get("msg") else {}
    config = get_config()
    model = lambda m: {"provider": m.provider, "model": m.model, "env": m.api_key_env,  # noqa: E731
                       "key": bool(os.environ.get(m.api_key_env))} if m else {"provider": "", "model": ""}
    models = {"llm": model(config.llm), "fast": model(config.fast), "transcription": model(config.transcription)}
    from .defaults import AUDIO_MODELS, FAST_MODELS, MODELS

    pick = {k: c.get() for k, c in _choices.items()} if page == "sources" else {k: None for k in _choices}
    return _page(request, "settings.html", user, page=page, nav=page, current=current, models=models, model_defaults=MODELS,
                 fast_defaults=FAST_MODELS, audio_defaults=AUDIO_MODELS, **saved,
                 sections=settings_form.SECTIONS, values=settings_form.values(sources),
                 visibility_defaults=sources.get("default_visibility", {}), source_types=SOURCE_TYPES,
                 default_prompt=DEFAULT_INSTRUCTIONS, service_account=service_account(), pick=pick,
                 access_errors=_section_errors(pick) if page == "sources" else {}, emoji=EMOJI,
                 sync_status={source: (lambda run: dict(run, line=_sync_line(run)))(syncs.status(store, source))
                              for source in syncs.SOURCES},
                 reader_model=get_config().reader.model,
                 settings_version=get_config().settings_version)


def _sync_line(run: dict) -> str:
    """What Settings says beside a source: when it was last read and what that found."""
    if run["running"]:
        return "Reading now…"
    if not run["finished"]:
        return "Not read yet"
    line = f"Last read {_ago(run['finished'])}"
    if run["error"]:
        return f"{line}, and it failed: {run['error'][:120]}"
    return f"{line}: {run['summary']}" if run["summary"] else line


def _sync_json(source: str, store) -> dict:
    run = syncs.status(store, source)
    return {"running": run["running"], "line": _sync_line(run), "failed": bool(run["error"]) and not run["running"],
            "finished": run["finished"].isoformat() if run["finished"] else None,
            "requested": run["requested"].isoformat() if run["requested"] else None}


@router.post("/sources/read/{source}")
async def settings_read_now(request: Request, source: str) -> Response:
    """'Read now': runs a source's scheduled sync straight away. The page's script asks for JSON
    and follows the run with /status; without it, the page reloads with a message."""
    user, store = _user(request), _store()
    await _form(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    back = f"/dashboard/sources?focus={source}#section-{source}"
    try:
        await run_in_threadpool(syncs.read_now, source, store)
    except ValueError as exc:
        if wants_json:  # already being read: the page follows that run instead
            return JSONResponse({"error": str(exc), **_sync_json(source, store)}, status_code=409)
        return _back(back, err=str(exc))
    events.record(store, "sync_started", f"{syncs.SOURCES[source][1]}: read now", via="dashboard", actor=user.actor)
    if wants_json:
        return JSONResponse(_sync_json(source, store))
    return _back(back, msg=f"Reading {syncs.SOURCES[source][1]} now. New items appear on Items as they're saved.")


@router.get("/sources/read/{source}/status")
def settings_read_status(request: Request, source: str) -> Response:
    _user(request)
    if source not in syncs.SOURCES:
        raise HTTPException(404)
    return JSONResponse(_sync_json(source, _store()), headers={"Cache-Control": "no-store"})


@router.get("/sources/refresh/{source}")
def settings_refresh(request: Request, source: str) -> Response:
    """'Refresh the list': for a folder shared or a channel made a moment ago."""
    _user(request)
    if source not in _choices:
        raise HTTPException(404)
    _choices[source].reset()
    section = next((s.key for s in settings_form.SECTIONS if source in s.sources), "")
    return RedirectResponse(f"/dashboard/sources#section-{section}", status_code=303)


@router.post("/settings")
async def settings_save(request: Request) -> Response:
    return await _save(request, "settings")


@router.post("/sources")
async def sources_save(request: Request) -> Response:
    return await _save(request, "sources")


async def _save(request: Request, page: str) -> Response:
    """Saves what the page shows, and leaves what's on the other page as it is."""
    user, store = _user(request), _store()
    form = await _form(request)
    data = settings_store.current(store)
    home = f"/dashboard/{page}"
    notes: list[str] = []
    if page == "settings":
        data["company"] = str(form.get("company", "")).strip()
        if "llm_provider" in form:
            data["llm"] = {**(data.get("llm") or {}), "provider": str(form["llm_provider"]),
                           "model": str(form.get("llm_model", "")).strip()}
            fast = str(form.get("fast_provider", "")).strip()
            data["fast"] = {"provider": fast, "model": str(form.get("fast_model", "")).strip()} if fast else None
            audio = str(form.get("transcription_provider", "")).strip()
            data["transcription"] = {"provider": audio, "model": str(form.get("transcription_model", "")).strip()} \
                if audio else None
        prompt = str(form.get("prompt", "")).strip()
        data["prompts"] = {**data.get("prompts", {}), "tagging": prompt} if prompt else \
            {k: v for k, v in data.get("prompts", {}).items() if k != "tagging"}
    else:
        picked = {k: c.get() for k, c in _choices.items()}
        names = {k: {c["id"]: c["name"] for c in v} for k, v in picked.items() if v}
        # Sections that Slack or Drive can't be reached for are shown greyed out and left as they are.
        posted = form.get("sections", [])
        blocked = _section_errors()
        form["sections"] = [s for s in (posted if isinstance(posted, list) else [posted]) if s not in blocked]
        try:
            sources = settings_form.read(form, data.get("sources", {}), names)
        except ValueError as exc:
            return _back(home, err=str(exc))
        if any(f"visibility_{t}" in form for t in SOURCE_TYPES):
            sources["default_visibility"] = {t: form.get(f"visibility_{t}", "private") for t in SOURCE_TYPES}
        data["sources"] = sources
    try:
        settings_store.save(store, data, actor=user.actor)
    except ValueError as exc:
        return _back(home, err=str(exc))
    if page == "settings":
        return _back(home, msg="Saved.")
    notes = _join_channels(sources.get("slack", {}).get("channels") or [], picked["slack"], store)
    focus = form.get("focus")
    focus = (focus[-1] if isinstance(focus, list) else focus or "").strip()
    message = " ".join([WHEN_READ.get(focus.split("|")[0], "Saved."), *notes])
    if focus:
        # Saved from one channel, folder or place: back to it, with the message in its section.
        query = urlencode({"focus": focus, "msg": message})
        return RedirectResponse(f"{home}?{query}#section-{focus.split('|')[0]}", status_code=303)
    return _back(home, msg=message)


# What happens after saving a source: when it's next read. Saving doesn't start a scan.
WHEN_READ = {
    "slack": "Saved. Channels saving every message are read every 15 minutes, as sessions, from their Starting from "
             "date; reacted messages are saved as they happen.",
    "drive": "Saved. Drive is read every hour, including files already in the folder, and files edited since "
             "they were saved are read again.",
    "clickup": "Saved. ClickUp is read every 15 minutes: tagged tasks and Docs of any age, and Chat from the last week.",
    "email": "Saved. The inbox is read every 5 minutes.",
}


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
    data = resources.files("tle.static").joinpath(name).read_bytes()
    return Response(data, media_type=THEME_FILES[name], headers={"Cache-Control": "max-age=300"})


for _name in THEME_FILES:
    router.add_api_route(f"/{_name}", lambda name=_name: _theme_file(name), methods=["GET"])
