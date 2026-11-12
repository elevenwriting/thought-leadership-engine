# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""The Cloud Run service: ingestion endpoints, the Slack bot, and a query API
(which the ChatGPT action and the web form use). Email, Drive and ClickUp are read by
scheduled jobs instead (`tle sync-email`, `tle sync-drive`, `tle sync-clickup`)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any
from urllib.parse import quote

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import analytics, auth, dashboard, events, items, playbooks, topics
from .config import get_config
from .connectors import slack as slack_connector
from .attribution import attribute
from .attribution import metadata as attribution_metadata
from .extract import extract_text, guess_mime
from .ingest import prepare_item
from .links import default_source_type
from .links import fetch as fetch_link
from .models import Item
from .store import SearchQuery, get_store

logging.basicConfig(level=logging.INFO)

def _mcp_app():
    """The MCP server over HTTP, for Claude and other MCP clients: stateless, so any Cloud Run
    instance can answer, and only with the API token. Its localhost-only check is off because
    it's served from the service's own address, and the token is the protection."""
    from mcp.server.transport_security import TransportSecuritySettings

    from .mcp_server import mcp

    inner = mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def with_token(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
            if not supplied or not auth.check_token(supplied):
                body = json.dumps({"detail": "Missing or wrong API token."}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await inner(scope, receive, send)

    return mcp, with_token


_mcp, _mcp_asgi = _mcp_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Tables and first settings are made when the service starts, so there's no setup step."""
    try:
        get_store()  # makes the tables and first settings if they're missing
    except Exception:  # noqa: BLE001
        # Don't stop the service starting: pages say what's wrong, and the next request retries.
        logging.getLogger(__name__).exception("Couldn't prepare the database on start")
    async with _mcp.session_manager.run():
        yield


app = FastAPI(
    lifespan=lifespan,
    title="Thought Leadership Engine",
    version="0.1.0",
    description="Capture, tag and query your company's thought leadership.",
    servers=[{"url": os.environ["PUBLIC_URL"]}] if os.environ.get("PUBLIC_URL") else None,
)
# Dashboard sign-ins. Cookies are HTTPS-only on Cloud Run (K_SERVICE is set there).
app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(), session_cookie="tle_session",
                   same_site="lax", https_only=bool(os.environ.get("K_SERVICE")), max_age=14 * 24 * 3600)


app.include_router(dashboard.router)
app.mount("/mcp", _mcp_asgi)


class _McpWithoutSlash:
    """Clients are given the address as /mcp; answer it directly rather than redirect a POST."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


app.add_middleware(_McpWithoutSlash)


def _database_errors() -> tuple[type[Exception], ...]:
    import sqlite3

    import psycopg

    return (psycopg.Error, sqlite3.Error)


async def _database_unavailable(request: Request, exc: Exception):
    """The database can't be reached (or its tables can't be used): say so plainly, rather than
    a bare server error."""
    reason = str(exc).strip().splitlines()[-1][:300] if str(exc).strip() else type(exc).__name__
    logging.getLogger(__name__).exception("Database error on %s", request.url.path)
    if request.url.path.startswith("/dashboard") or request.url.path.startswith("/ingest"):
        return dashboard.unavailable_page(request, reason)
    return JSONResponse({"detail": f"The database can't be reached: {reason}"}, status_code=503)


for _error in _database_errors():
    app.add_exception_handler(_error, _database_unavailable)


@app.exception_handler(dashboard.SignInRequired)
async def _sign_in(request: Request, exc: dashboard.SignInRequired):
    return RedirectResponse(f"/dashboard/login?next={quote(request.url.path)}", status_code=303)


def require_token(request: Request) -> None:
    """The API token, or a dashboard sign-in. A sign-in only counts with the X-TLE-Client
    header, which other sites can't add to a request without the browser asking first."""
    if not os.environ.get("TLE_API_TOKEN", ""):
        raise HTTPException(500, "TLE_API_TOKEN isn't set on the server.")
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if supplied and auth.check_token(supplied):
        request.state.actor, request.state.via = "", "api"
        return
    if request.headers.get("x-tle-client"):
        user = auth.current_user(request)
        if user:
            request.state.actor, request.state.via = user.actor, "web"
            return
    raise HTTPException(401, "Missing or wrong API token.")


def _who(request: Request) -> dict:
    via = getattr(request.state, "via", "api")
    if request.headers.get("x-tle-client") == "web-form":
        via = "web"
    return {"via": via, "actor": getattr(request.state, "actor", "")}


def _split(values: list[str] | None) -> list[str]:
    """Accepts ?themes=A&themes=B or ?themes=A,B."""
    return [v.strip() for raw in values or [] for v in raw.split(",") if v.strip()]


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


# --- Ingestion ---------------------------------------------------------------


class IngestIn(BaseModel):
    text: str | None = None
    url: str | None = None
    source_type: str | None = None
    authors: list[str] = []
    date_created: datetime | None = None
    visibility: str | None = None
    label: str | None = None
    note: str = ""
    source_metadata: dict[str, Any] = {}
    summary: str | None = None
    theme_tags: list[str] | None = None
    narrative_tags: list[str] | None = None


UNKNOWN_AUTHOR = "Unknown"


def _prepare(body: IngestIn, *, text: str | None = None, file_metadata: dict | None = None,
             default_type: str = "braindump") -> Item:
    """Turns a request into a tagged (but unsaved) item: fetches a link if there's no text,
    works out the author when none is given, and uses supplied tags as they are."""
    config = get_config()
    metadata = {**(file_metadata or {}), **body.source_metadata}
    if body.label:
        metadata["label"] = body.label
    text = text if text is not None else body.text
    source_type, date, context = body.source_type, body.date_created, ""

    if not (text or "").strip() and body.url:
        page = fetch_link(body.url, config)
        text = page.text
        metadata = {**page.metadata(), **metadata}
        metadata.setdefault("label", page.site or page.title)
        source_type = source_type or default_source_type(page.final_url)
        date = date or page.date
        context = page.context()
    if not (text or "").strip():
        raise ValueError("Give some text, a file or a link.")

    authors = [a for a in body.authors if a.strip()]
    if not authors:
        note = f"\nNote from the person sharing it: {body.note}" if body.note.strip() else ""
        who = attribute(text, config=config, submitter=UNKNOWN_AUTHOR, context=context + note)
        authors = who.authors
        metadata.update(attribution_metadata(who, "web form"))
        date = date or who.date
    else:
        metadata.setdefault("author_source", "provided")

    given = {"summary": body.summary, "theme_tags": body.theme_tags, "narrative_tags": body.narrative_tags}
    tags = given if any(v is not None for v in given.values()) else None
    return prepare_item(text, config=config, source_type=source_type or default_type, authors=authors,
                        date_created=date, visibility=body.visibility, source_metadata=metadata,
                        note=body.note, tags=tags)


async def _file_input(file: UploadFile, authors: str, source_type: str | None, visibility: str | None,
                      date_created: datetime | None, label: str | None, note: str) -> tuple[IngestIn, str, dict, str]:
    data = await file.read()
    filename = file.filename or "upload"
    mime = guess_mime(filename, file.content_type)
    # Extraction can call an LLM (images, audio), so it runs off the event loop.
    text = await run_in_threadpool(extract_text, data, filename, get_config(), mime)
    body = IngestIn(authors=_split([authors]) if authors else [], source_type=source_type or None,
                    visibility=visibility or None, date_created=date_created,
                    label=label or filename.rsplit(".", 1)[0], note=note or "")
    default_type = "voicenote" if mime.startswith("audio/") else "document"
    return body, text, {"file_name": filename, "mime_type": mime}, default_type


@app.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/ingest", response_class=HTMLResponse, include_in_schema=False)
def upload_form(request: Request) -> HTMLResponse:
    return dashboard.add_page(request)


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"ok": True}


def _save(item: Item, request: Request) -> dict:
    store, who = get_store(), _who(request)
    item.via = who["via"]
    store.insert(item)
    events.item_saved(store, item, **who)
    return item.to_result(include_text=False)


def _failed(exc: Exception, request: Request, what: str) -> HTTPException:
    events.problem(get_store(), f"Couldn't save {what}: {exc}", **_who(request))
    return _bad_request(exc)


@app.post("/ingest", dependencies=[Depends(require_token)], operation_id="ingestText")
def ingest_endpoint(body: IngestIn, request: Request) -> dict:
    """Save text (a post, notes, a braindump) or a link (url). Authors are worked out from the
    content if you leave them out. A note guides the tagging ("this is Jane's view on pricing").
    Give summary/theme_tags/narrative_tags to use them as they are instead of tagging."""
    try:
        item = _prepare(body)
    except ValueError as exc:
        raise _failed(exc, request, body.url or "text")
    return _save(item, request)


@app.post("/ingest/preview", dependencies=[Depends(require_token)], include_in_schema=False)
def preview_endpoint(body: IngestIn) -> dict:
    """Everything /ingest would save, without saving it, so it can be checked and edited."""
    try:
        return _prepare(body).to_result()
    except ValueError as exc:
        raise _bad_request(exc)


@app.post("/ingest/file", dependencies=[Depends(require_token)], include_in_schema=False)
async def ingest_file_endpoint(
    request: Request,
    file: UploadFile = File(...), authors: str = Form(""), source_type: str | None = Form(None),
    visibility: str | None = Form(None), date_created: datetime | None = Form(None),
    label: str | None = Form(None), note: str = Form(""),
) -> dict:
    try:
        body, text, meta, default_type = await _file_input(file, authors, source_type, visibility, date_created, label, note)
        item = await run_in_threadpool(_prepare, body, text=text, file_metadata=meta, default_type=default_type)
    except ValueError as exc:
        raise _failed(exc, request, file.filename or "the file")
    return await run_in_threadpool(_save, item, request)


@app.post("/ingest/preview/file", dependencies=[Depends(require_token)], include_in_schema=False)
async def preview_file_endpoint(
    file: UploadFile = File(...), authors: str = Form(""), source_type: str | None = Form(None),
    visibility: str | None = Form(None), date_created: datetime | None = Form(None),
    label: str | None = Form(None), note: str = Form(""),
) -> dict:
    try:
        body, text, meta, default_type = await _file_input(file, authors, source_type, visibility, date_created, label, note)
        item = await run_in_threadpool(_prepare, body, text=text, file_metadata=meta, default_type=default_type)
        return item.to_result()
    except ValueError as exc:
        raise _bad_request(exc)


@app.post("/slack/events", include_in_schema=False)
async def slack_events(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()
    if not slack_connector.verify(body, dict(request.headers)):
        raise HTTPException(401, "Bad Slack signature.")
    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}
    # Slack wants an answer within 3 seconds; tagging takes longer, so it runs after the reply.
    if not request.headers.get("x-slack-retry-num"):
        background.add_task(_handle_slack, payload)
    return {"ok": True}


def _handle_slack(payload: dict) -> None:
    store = get_store()
    try:
        slack_connector.handle_event(payload, get_config(), store)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Slack event failed")
        events.problem(store, f"Couldn't save from Slack: {exc}", via="slack")


# --- Query -------------------------------------------------------------------


class ItemUpdate(BaseModel):
    status: str | None = None
    visibility: str | None = None
    theme_tags: list[str] | None = None
    narrative_tags: list[str] | None = None
    deliverables: list[str] | None = None


class DeliverableIn(BaseModel):
    format: str
    title_or_url: str


@app.get("/items", dependencies=[Depends(require_token)], operation_id="searchItems")
def search_items(
    text: str | None = Query(None, description="Case-insensitive phrase to find in the text or summary."),
    themes: list[str] | None = Query(None, description="Any of these themes."),
    narratives: list[str] | None = Query(None, description="Any of these narratives."),
    authors: list[str] | None = Query(None),
    source_types: list[str] | None = Query(None, description="Kinds of content: linkedin, meeting, document..."),
    sources: list[str] | None = Query(None, description="Ways in: slack, clickup, email, drive, web, api, mcp."),
    include_reading: bool = Query(False, description="Also return reading: other people's content the team "
                                  "shared without comment. It's never the company's view."),
    visibility: str | None = Query(None, description="public or private. Leave out to get both."),
    status: str | None = Query(None),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    include_text: bool = Query(False, description="Include the full text (long). Summaries are always included."),
    limit: int = Query(20, le=100),
    offset: int = 0,
) -> dict:
    """Find captured items. Private items are returned with a private_flag: use them to
    inform content, but don't quote or publish them without the author's approval."""
    store = get_store()
    items = store.search(SearchQuery(
        text=text, themes=_split(themes), narratives=_split(narratives), authors=_split(authors),
        source_types=_split(source_types), vias=_split(sources), reading=None if include_reading else False,
        visibility=visibility, status=status, date_from=date_from, date_to=date_to, limit=limit, offset=offset,
    ))
    return {"count": len(items),
            "items": topics.results(items, store, include_text=include_text, max_text_chars=20_000)}


@app.get("/items/{row_id}", dependencies=[Depends(require_token)], operation_id="getItem")
def get_item(row_id: str) -> dict:
    """One item with its full text. A chat session also lists its topics, each with the messages it
    covers (as #numbers in the text) and the chain it's on: the same work in other sessions."""
    store = get_store()
    item = store.get(row_id)
    if not item:
        raise HTTPException(404, "No item with that ID.")
    return topics.result(item, store)


@app.get("/topics/{topic_id}/chain", dependencies=[Depends(require_token)], operation_id="getChain")
def get_chain(topic_id: str) -> dict:
    """A chat topic and every topic linked to it (the same piece of work picked up in other
    sessions), oldest first, each with its messages and citation."""
    try:
        return {"chain": topics.chain_result(topic_id, get_store())}
    except KeyError:
        raise HTTPException(404, "No topic with that ID.")


@app.patch("/items/{row_id}", dependencies=[Depends(require_token)], operation_id="updateItem")
def update_item(row_id: str, body: ItemUpdate, request: Request) -> dict:
    """Change an item's status, visibility, tags or deliverables."""
    try:
        item = items.update(get_store(), get_config(), row_id, body.model_dump(exclude_none=True), **_who(request))
    except ValueError as exc:
        raise _bad_request(exc)
    if not item:
        raise HTTPException(404, "No item with that ID.")
    return item.to_result(include_text=False)


@app.post("/items/{row_id}/deliverables", dependencies=[Depends(require_token)], operation_id="addDeliverable")
def add_deliverable(row_id: str, body: DeliverableIn, request: Request) -> dict:
    """Record a piece of content made from this item, and mark the item as used."""
    item = items.add_deliverable(get_store(), get_config(), row_id, f"{body.format}: {body.title_or_url}",
                                 **_who(request))
    if not item:
        raise HTTPException(404, "No item with that ID.")
    return item.to_result(include_text=False)


@app.get("/coverage", dependencies=[Depends(require_token)], operation_id="getCoverage")
def get_coverage(visibility: str | None = None) -> dict:
    """Theme x narrative grid with item counts and which formats have been made, plus the gaps."""
    return analytics.coverage(get_store(), get_config(), visibility)


@app.get("/trends", dependencies=[Depends(require_token)], operation_id="getTrends")
def get_trends(days: int = Query(30, ge=1, le=365)) -> dict:
    """Activity per theme, narrative, author and source: the last N days against the N days before."""
    return analytics.trends(get_store(), days)


@app.get("/taxonomy", dependencies=[Depends(require_token)], operation_id="getTaxonomy")
def get_taxonomy() -> dict:
    """The company's themes, narratives, formats, people and source types."""
    config = get_config()
    return {
        "company": config.company,
        "themes": [t.__dict__ for t in config.themes],
        "narratives": [n.__dict__ for n in config.narratives],
        "formats": config.formats,
        "people": [{"name": p.name, "role": p.role, "responsible_for": p.responsible_for} for p in config.people],
        "source_types": list(config.sources.get("default_visibility", {})),
        "playbooks": playbooks.names(),
    }


@app.get("/playbooks/{name}", dependencies=[Depends(require_token)], operation_id="getPlaybook")
def get_playbook(name: str) -> dict:
    """Step-by-step instructions for a job: query, coverage, updates, trends, atomise, aggregate,
    case-studies, mining, debate, foresight, post-mortems, briefs, media, after-publishing."""
    try:
        return {"name": name, "instructions": playbooks.get(name)}
    except KeyError as exc:
        raise HTTPException(404, str(exc))
