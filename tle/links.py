# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""Fetches a shared link and turns it into text.

Web pages are read as HTML first and the main content is pulled out with trafilatura
(dropping menus, footers and cookie banners), along with the title, author and date
when the page has them. Pages that build their content with JavaScript come back
nearly empty that way, so those are rendered in a headless Chromium (Playwright) and
read again. Links to files (PDF, DOCX, audio, images...) are downloaded and read like
an upload.

Every address is checked before it's fetched, including each redirect and every
request the headless browser makes: anything on a private or internal network is
refused, so a shared link can't be used to reach the server's own metadata or
internal services.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from urllib.parse import urljoin, urlparse

import httpx

from .config import Config
from .extract import extract_text

log = logging.getLogger(__name__)

URL = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
MAX_BYTES = 25 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
# Links in emails that are never the thing being shared.
NOT_CONTENT = re.compile(
    r"unsubscribe|/optout|list-manage\.com|calendar\.google\.com|meet\.google\.com|zoom\.us/j/"
    r"|teams\.microsoft\.com/l/meetup|aka\.ms|/signature|googleusercontent\.com|\.(png|jpe?g|gif|svg)(\?|$)",
    re.IGNORECASE,
)


@dataclass
class Page:
    url: str
    final_url: str
    text: str
    title: str = ""
    author: str = ""
    date: datetime | None = None
    site: str = ""
    method: str = "html"  # html | rendered | file

    def metadata(self) -> dict:
        return {k: v for k, v in {
            "url": self.final_url, "shared_url": self.url if self.url != self.final_url else None,
            "page_title": self.title, "page_author": self.author, "site": self.site,
            "page_date": self.date.date().isoformat() if self.date else None, "fetched_by": self.method,
        }.items() if v}

    def context(self) -> str:
        """What the author detector is told about the page."""
        parts = [f"Shared link: {self.final_url}"]
        parts += [f"{k}: {v}" for k, v in (("Site", self.site), ("Page title", self.title),
                                           ("Byline on the page", self.author)) if v]
        return "\n".join(parts)


def find_urls(text: str, limit: int = 5) -> list[str]:
    urls = []
    for match in URL.finditer(text or ""):
        # Markdown escapes (ClickUp writes What\_Is for What_Is) aren't part of the address.
        url = re.sub(r"\\([_*~`])", r"\1", match.group(0)).rstrip(".,;:!?*>")
        if not NOT_CONTENT.search(url) and url not in urls:
            urls.append(url)
    return urls[:limit]


def default_source_type(url: str) -> str:
    return "linkedin" if (urlparse(url).hostname or "").endswith("linkedin.com") else "web"


@lru_cache(maxsize=512)
def _host_is_public(host: str) -> bool:
    if host in {"localhost", "metadata", "metadata.google.internal"} or host.endswith(".internal"):
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Not a web address: {url}")
    if not _host_is_public(parsed.hostname.lower()):
        raise ValueError(f"Refusing to fetch {parsed.hostname}: it isn't a public address.")


def _download(url: str, timeout: float) -> tuple[str, str, bytes]:
    """Follows redirects by hand so each hop is checked. Returns (final url, content type, body)."""
    with httpx.Client(follow_redirects=False, timeout=timeout, headers={"User-Agent": USER_AGENT,
                      "Accept-Language": "en-GB,en;q=0.9"}) as client:
        for _ in range(6):
            check_url(url)
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code >= 400:
                    raise ValueError(f"{url} returned {response.status_code}.")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise ValueError(f"{url} is larger than 25 MB.")
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                return url, ctype, bytes(body)
    raise ValueError(f"Too many redirects from {url}.")


def _render(url: str, timeout: float) -> str:
    from playwright.sync_api import sync_playwright

    def route(request_route):
        req = request_route.request
        if req.resource_type in {"image", "media", "font"}:
            return request_route.abort()
        try:
            check_url(req.url)
        except ValueError:
            return request_route.abort()
        return request_route.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="en-GB")
            page.route("**/*", route)
            page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            return page.content()
        finally:
            browser.close()


def _from_html(html: str, url: str) -> dict:
    import trafilatura

    doc = trafilatura.bare_extraction(html, url=url, favor_recall=True, with_metadata=True,
                                      include_comments=False, include_tables=True)
    if not doc:
        return {"text": ""}
    date = None
    if doc.date:
        try:
            date = datetime.fromisoformat(doc.date)
        except ValueError:
            pass
    return {"text": (doc.text or "").strip(), "title": doc.title or "", "author": doc.author or "",
            "site": doc.sitename or urlparse(url).hostname or "", "date": date}


def fetch(url: str, config: Config) -> Page:
    settings = config.sources.get("links", {})
    render = settings.get("render", "auto")  # auto | always | never
    min_chars = int(settings.get("min_chars", 400))
    timeout = float(settings.get("timeout_seconds", 30))

    final_url, ctype, body = _download(url.strip(), timeout)

    if ctype in {"text/html", "application/xhtml+xml"} or (not ctype and body.lstrip()[:1] == b"<"):
        found = _from_html(body.decode("utf-8", errors="replace"), final_url)
        method = "html"
        if render == "always" or (render == "auto" and len(found["text"]) < min_chars):
            try:
                rendered = _from_html(_render(final_url, timeout), final_url)
                if len(rendered["text"]) > len(found["text"]):
                    found, method = rendered, "rendered"
            except Exception as exc:  # noqa: BLE001
                log.warning("Couldn't render %s: %s", final_url, exc)
        if not found["text"]:
            raise ValueError(f"Couldn't find any readable text at {final_url}.")
        text = f"{found['title']}\n\n{found['text']}" if found.get("title") else found["text"]
        return Page(url=url, final_url=final_url, text=text, title=found.get("title", ""),
                    author=found.get("author", ""), date=found.get("date"), site=found.get("site", ""),
                    method=method)

    filename = urlparse(final_url).path.rsplit("/", 1)[-1] or "download"
    text = extract_text(body, filename, config, ctype or None)
    return Page(url=url, final_url=final_url, text=text, title=filename,
                site=urlparse(final_url).hostname or "", method="file")
