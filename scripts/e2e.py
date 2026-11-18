# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""End-to-end test of a deployed Expertise Database, through every way in and out that needs
no third-party account: text, files (PDF, DOCX, image, audio, transcript) and links into /ingest,
every REST operation, the OpenAPI schema ChatGPT's Action imports, the MCP server over HTTP,
and the same MCP server through mcp-remote (how Claude Desktop and Cursor connect).

It saves real items, labelled "E2E test", and archives them at the end. Run it against a test
deployment, not the one your team uses: the API can't delete items.

    TLE_URL=https://...run.app TLE_API_TOKEN=... python scripts/e2e.py [--label openai] [--skip-remote]

The token is read from the environment so it never appears on a command line. Needs Python with
this project installed (for the MCP client, Pillow and python-docx), and for audio macOS's `say`
plus ffmpeg; for mcp-remote, Node's npx. Prints a table and writes e2e-<label>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx

LABEL = "E2E test"
LINK = "https://www.paulgraham.com/greatwork.html"
TEXT = (
    "Sam Patel, head of content: I've stopped believing that more AI content wins. Our pricing "
    "pages got the most organic leads this quarter once we rewrote them in our salespeople's own "
    "words, with real objections from calls. My prediction: by next year buyers will skip anything "
    "that sounds like it came from a template, and the brands that win will publish what their "
    "experts actually argue about internally."
)
TRANSCRIPT = """WEBVTT

00:00:01.000 --> 00:00:09.000
<v Sam Patel>The mistake most teams make is treating case studies as marketing. They're evidence.

00:00:09.000 --> 00:00:18.000
<v Alex Moore>Agreed. We doubled demo requests when we led with the customer's numbers, not our features.
"""
AUDIO_TEXT = ("This is Sam Patel with a voice note. I think podcasts are underrated for B2B, because "
              "an hour of a founder talking gives you ten articles' worth of real opinions.")


class Results:
    def __init__(self):
        self.rows: list[dict] = []

    def check(self, area: str, name: str, fn):
        started = time.monotonic()
        try:
            detail = fn()
            ok = True
        except Exception as exc:  # noqa: BLE001
            while getattr(exc, "exceptions", None):  # an async task group's error wraps the real one
                exc = exc.exceptions[0]
            detail = f"{type(exc).__name__}: {exc}"
            ok = False
            if os.environ.get("E2E_DEBUG"):
                traceback.print_exc()
        row = {"area": area, "check": name, "ok": ok, "detail": str(detail or "")[:300],
               "seconds": round(time.monotonic() - started, 1)}
        self.rows.append(row)
        print(f"{'PASS' if ok else 'FAIL'}  {area:<10} {name:<38} {row['detail'][:110]}", flush=True)
        return detail if ok else None


def expect(condition, message: str):
    if not condition:
        raise AssertionError(message)


# --- Fixtures ----------------------------------------------------------------------

def _pdf(path: Path, lines: list[str]) -> None:
    """A one-page PDF with a real text layer, written by hand so nothing extra is needed."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 12 Tf 72 720 Td 16 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def fixtures(folder: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    _pdf(folder / "e2e-memo.pdf", [
        "Memo from Alex Moore, CEO",
        "We will not chase every AI search trend. Our view: original research beats volume.",
        "Every quarter we publish one study built on our own customer data.",
    ])
    files["pdf"] = folder / "e2e-memo.pdf"

    import docx
    document = docx.Document()
    document.add_heading("Interview notes: Alex Moore on pricing", 1)
    document.add_paragraph("Alex Moore: Value-based pricing only works when you can show the value in the "
                           "customer's own numbers. Otherwise it's just a higher price.")
    document.save(folder / "e2e-interview.docx")
    files["docx"] = folder / "e2e-interview.docx"

    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1200, 500), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 40)
    except OSError:
        font = ImageFont.load_default()
    draw.multiline_text((50, 60), "Sam Patel on LinkedIn:\n\"Thought leadership isn't a format.\nIt's having "
                        "a view you'd defend\nin front of your customers.\"", fill="black", font=font, spacing=18)
    image.save(folder / "e2e-post.png")
    files["image"] = folder / "e2e-post.png"

    (folder / "e2e-call.vtt").write_text(TRANSCRIPT)
    files["transcript"] = folder / "e2e-call.vtt"

    if shutil.which("say") and shutil.which("ffmpeg"):
        aiff = folder / "e2e-voice.aiff"
        subprocess.run(["say", "-o", str(aiff), AUDIO_TEXT], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), str(folder / "e2e-voice.mp3")], check=True)
        files["audio"] = folder / "e2e-voice.mp3"
    return files


# --- REST -------------------------------------------------------------------------

def rest_checks(r: Results, api: httpx.Client, files: dict[str, Path], saved: list[str]) -> None:
    r.check("rest", "health", lambda: expect(api.get("/health").json().get("ok"), "not ok") or "ok")
    r.check("rest", "refuses a wrong token", lambda: expect(
        api.get("/items", headers={"Authorization": "Bearer wrong"}).status_code == 401, "expected 401") or "401")

    def tagged(item: dict) -> str:
        expect(item.get("summary"), "no summary")
        expect(item.get("theme_tags") or item.get("narrative_tags"), "no themes or narratives")
        return (f"{item.get('source_type')} by {', '.join(item.get('authors') or [])}; "
                f"themes {item.get('theme_tags')}, narratives {item.get('narrative_tags')}")

    def preview_text():
        res = api.post("/ingest/preview", json={"text": TEXT, "label": LABEL})
        res.raise_for_status()
        return tagged(res.json())

    def save_text():
        res = api.post("/ingest", json={"text": TEXT, "label": LABEL, "source_type": "braindump"})
        res.raise_for_status()
        item = res.json()
        saved.append(item["row_id"])
        expect("Sam Patel" in " ".join(item.get("authors") or []), f"author worked out as {item.get('authors')}")
        return tagged(item)

    def save_link():
        res = api.post("/ingest", json={"url": LINK, "label": LABEL}, timeout=180)
        res.raise_for_status()
        item = res.json()
        saved.append(item["row_id"])
        return tagged(item)

    r.check("ways in", "preview text", preview_text)
    r.check("ways in", "save text (authors worked out)", save_text)
    r.check("ways in", "save a link", save_link)

    for kind, path in files.items():
        def save_file(kind=kind, path=path):
            with path.open("rb") as fh:
                res = api.post("/ingest/file", files={"file": (path.name, fh)}, data={"label": LABEL}, timeout=300)
            if res.status_code >= 400:
                raise AssertionError(f"{res.status_code}: {res.text[:200]}")
            item = res.json()
            saved.append(item["row_id"])
            return tagged(item)
        r.check("ways in", f"save a file: {kind}", save_file)

    if not saved:
        return
    first = saved[0]

    def search():
        res = api.get("/items", params={"text": "pricing pages", "limit": 5})
        res.raise_for_status()
        found = [i["row_id"] for i in res.json()["items"]]
        expect(first in found, "the saved text wasn't found")
        return f"{res.json()['count']} found"

    def get_item():
        res = api.get(f"/items/{first}")
        res.raise_for_status()
        expect(res.json().get("citation"), "no citation")
        return res.json()["citation"]

    def update():
        res = api.patch(f"/items/{first}", json={"status": "reviewed"})
        res.raise_for_status()
        expect(res.json()["status"] == "reviewed", "status unchanged")
        return "reviewed"

    def deliverable():
        res = api.post(f"/items/{first}/deliverables", json={"format": "LinkedIn post", "title_or_url": "E2E post"})
        res.raise_for_status()
        expect(any("E2E post" in d for d in res.json()["deliverables"]), "not recorded")
        return "recorded"

    def simple(path, key):
        def go():
            res = api.get(path)
            res.raise_for_status()
            expect(key in res.json(), f"no {key}")
            return "ok"
        return go

    r.check("ways out", "REST searchItems", search)
    r.check("ways out", "REST getItem", get_item)
    r.check("ways out", "REST updateItem", update)
    r.check("ways out", "REST addDeliverable", deliverable)
    r.check("ways out", "REST getCoverage", simple("/coverage", "cells"))
    r.check("ways out", "REST getTrends", simple("/trends?days=30", "window_days"))
    r.check("ways out", "REST getTaxonomy", simple("/taxonomy", "themes"))
    r.check("ways out", "REST getPlaybook", simple("/playbooks/query", "instructions"))


def openapi_checks(r: Results, api: httpx.Client) -> None:
    """What ChatGPT's Action import needs: a server URL, an operationId on every operation, and
    descriptions within its limits (300 characters for an operation, 700 for a parameter)."""
    def check():
        schema = api.get("/openapi.json").json()
        expect(schema.get("servers"), "no servers: set PUBLIC_URL on the service")
        expect(schema["servers"][0]["url"].rstrip("/") == str(api.base_url).rstrip("/"),
               f"server is {schema['servers'][0]['url']}")
        problems, ops = [], 0
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                ops += 1
                if not op.get("operationId"):
                    problems.append(f"{method} {path}: no operationId")
                if len(op.get("description") or "") > 300:
                    problems.append(f"{op.get('operationId')}: description {len(op['description'])} chars")
                for p in op.get("parameters", []):
                    if len(p.get("description") or "") > 700:
                        problems.append(f"{op.get('operationId')}.{p['name']}: description too long")
        expect(ops <= 30, f"{ops} operations; ChatGPT allows 30")
        expect(not problems, "; ".join(problems))
        return f"{ops} operations, {schema['info']['title']}"
    r.check("chatgpt", "OpenAPI schema fits a GPT Action", check)


# --- MCP --------------------------------------------------------------------------

async def _mcp_session_checks(session, saved: list[str]) -> list[tuple[str, str]]:
    out = []
    await session.initialize()
    tools = sorted(t.name for t in (await session.list_tools()).tools)
    out.append(("list tools", ", ".join(tools)))
    prompts = (await session.list_prompts()).prompts
    out.append(("list prompts", f"{len(prompts)} playbooks"))

    async def call(tool, **args):
        res = await session.call_tool(tool, args)
        if getattr(res, "is_error", None) or getattr(res, "isError", None):  # mcp 2.x / 1.x
            raise AssertionError(res.content[0].text[:200] if res.content else "error")
        return res.content[0].text if res.content else ""

    text = await call("search_items", text="pricing", limit=3)
    out.append(("search_items", f"{len(text)} chars"))
    await call("get_taxonomy")
    out.append(("get_taxonomy", "ok"))
    await call("get_playbook", name="coverage")
    out.append(("get_playbook", "ok"))
    await call("get_coverage")
    out.append(("get_coverage", "ok"))
    await call("get_trends", days=30)
    out.append(("get_trends", "ok"))
    added = json.loads(await call("add_item", text="Alex Moore: AI-written listicles are the new keyword stuffing.",
                                  authors=["Alex Moore"], source_type="braindump", label=LABEL))
    saved.append(added["row_id"])
    out.append(("add_item", added["row_id"]))
    item = json.loads(await call("get_item", row_id=added["row_id"]))
    out.append(("get_item", item.get("citation", "")))
    await call("update_item", row_id=added["row_id"], status="reviewed")
    out.append(("update_item", "reviewed"))
    await call("add_deliverable", row_id=added["row_id"], format="Newsletter", title_or_url="E2E issue")
    out.append(("add_deliverable", "recorded"))
    topic = next((t.get("id") for t in item.get("topics") or [] if t.get("id")), None)
    if topic:
        await call("get_chain", topic_id=topic)
        out.append(("get_chain", "ok"))
    return out


def mcp_http_checks(r: Results, url: str, token: str, saved: list[str]) -> None:
    from mcp import ClientSession

    def transport():
        headers = {"Authorization": f"Bearer {token}"}
        try:  # mcp 2.x takes a configured client
            import httpx2
            from mcp.client.streamable_http import streamable_http_client
            return streamable_http_client(f"{url}/mcp", http_client=httpx2.AsyncClient(headers=headers, timeout=120))
        except ImportError:  # mcp 1.x
            from mcp.client.streamable_http import streamablehttp_client
            return streamablehttp_client(f"{url}/mcp", headers=headers)

    async def run():
        async with transport() as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                return await _mcp_session_checks(session, saved)

    results = r.check("mcp http", "connect and run every tool", lambda: asyncio.run(run()))
    for name, detail in results or []:
        r.rows.append({"area": "mcp http", "check": name, "ok": True, "detail": detail[:300], "seconds": 0})


def mcp_remote_checks(r: Results, url: str, token: str, saved: list[str]) -> None:
    """Claude Desktop and Cursor start `npx mcp-remote <url>` and talk to it over stdio."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if not shutil.which("npx"):
        r.rows.append({"area": "mcp-remote", "check": "npx", "ok": False, "detail": "npx not installed", "seconds": 0})
        return
    # mcp-remote fills ${AUTH_HEADER} from the environment, as its README suggests for clients
    # that mangle spaces, so the token isn't in the arguments.
    params = StdioServerParameters(
        command="npx", args=["-y", "mcp-remote", f"{url}/mcp", "--header", "Authorization:${AUTH_HEADER}"],
        env={**os.environ, "AUTH_HEADER": f"Bearer {token}"})

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                return await _mcp_session_checks(session, saved)

    results = r.check("mcp-remote", "connect and run every tool", lambda: asyncio.run(asyncio.wait_for(run(), 240)))
    for name, detail in results or []:
        r.rows.append({"area": "mcp-remote", "check": name, "ok": True, "detail": detail[:300], "seconds": 0})


# --- Run --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--label", default="run", help="Names the results file, e.g. the model in use.")
    parser.add_argument("--skip-remote", action="store_true", help="Skip mcp-remote (needs npx).")
    parser.add_argument("--keep", action="store_true", help="Leave the test items as they are instead of archiving them.")
    args = parser.parse_args()
    url = os.environ.get("TLE_URL", "").rstrip("/")
    token = os.environ.get("TLE_API_TOKEN", "")
    if not url or not token:
        print("Set TLE_URL and TLE_API_TOKEN.", file=sys.stderr)
        return 2

    r, saved = Results(), []
    api = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=180)
    with tempfile.TemporaryDirectory() as folder:
        files = r.check("setup", "make test files", lambda: fixtures(Path(folder))) or {}
        rest_checks(r, api, files, saved)
    openapi_checks(r, api)
    mcp_http_checks(r, url, token, saved)
    if not args.skip_remote:
        mcp_remote_checks(r, url, token, saved)

    if not args.keep:
        for row_id in saved:
            api.patch(f"/items/{row_id}", json={"status": "archived"})

    failed = [row for row in r.rows if not row["ok"]]
    Path(f"e2e-{args.label}.json").write_text(json.dumps({"url": url, "label": args.label, "rows": r.rows,
                                                         "items": saved}, indent=2))
    print(f"\n{len(r.rows) - len(failed)} passed, {len(failed)} failed. Results in e2e-{args.label}.json")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
