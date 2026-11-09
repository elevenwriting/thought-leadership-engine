"""Command line: `tle check`, `tle init-db`, `tle ingest`, `tle sync-email`, `tle sync-drive`, `tle sync-clickup`, `tle serve`..."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from . import analytics
from .config import SOURCE_TYPES, get_config
from .store import SearchQuery, get_store


def _print(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def _load_env_file(path: Path) -> None:
    """KEY=value lines, for development. Variables already set win, and nothing is printed."""
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="tle", description="Thought Leadership Engine")
    parser.add_argument("--env-file", type=Path, metavar="PATH",
                        help="For development: read KEY=value lines (keys, TLE_DEV_USER...) from this file first.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create or upgrade the tables (the service also does this when it starts).")
    sub.add_parser("check", help="Check that config, database, keys and sources work, and say what to fix.")

    p = sub.add_parser("ingest", help="Ingest a file, text with --text, or a link with --url.")
    p.add_argument("file", nargs="?", type=Path)
    p.add_argument("--text")
    p.add_argument("--url")
    p.add_argument("--note", default="", help="Guidance for the tagger, e.g. 'Jane's view on pricing'")
    p.add_argument("--author", action="append", required=True, help="Repeat for several authors.")
    p.add_argument("--source-type", choices=SOURCE_TYPES)
    p.add_argument("--visibility", choices=["public", "private"])
    p.add_argument("--date", type=datetime.fromisoformat, help="When it was said or written, e.g. 2026-09-19")
    p.add_argument("--label", help="Short label for citations, e.g. 'All-hands'")

    sub.add_parser("sync-drive", help="Ingest new files from the Drive folders chosen in Settings.")
    sub.add_parser("sync-clickup", help="Ingest tagged tasks, Docs and reacted Chat messages from ClickUp.")
    sub.add_parser("sync-email", help="Ingest new mail from the inbox set in Settings.")
    sub.add_parser("relate", help="Link items saved since the last run to related ones from other sources.")

    p = sub.add_parser("search", help="Search items.")
    p.add_argument("text", nargs="?")
    p.add_argument("--theme", action="append", default=[])
    p.add_argument("--narrative", action="append", default=[])
    p.add_argument("--author", action="append", default=[])
    p.add_argument("--visibility", choices=["public", "private"])
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("coverage", help="Theme x narrative coverage and gaps.")
    p.add_argument("--visibility", choices=["public", "private"])

    p = sub.add_parser("trends", help="Activity now against the previous period.")
    p.add_argument("--days", type=int, default=30)

    p = sub.add_parser("serve", help="Run the API locally.")
    p.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)
    if args.env_file:
        _load_env_file(args.env_file)
    if args.command == "check":
        from . import check

        results = check.run()
        print(check.report(results))
        sys.exit(1 if any(r.status == check.FAIL for r in results) else 0)
    config = get_config()

    if args.command == "serve":
        import uvicorn

        uvicorn.run("tle.api:app", host="127.0.0.1", port=args.port, reload=False)
        return

    store = get_store()

    if args.command == "init-db":
        from .settings import prepare

        fresh = prepare(store)
        print(f"Ready: {config.storage.get('backend')} storage" + (", starting from the built-in settings." if fresh else "."))
    elif args.command == "ingest":
        from .ingest import ingest_file, ingest_text

        common = dict(config=config, store=store, via="cli", authors=args.author, visibility=args.visibility,
                      date_created=args.date, source_metadata={"label": args.label} if args.label else None)
        if args.url:
            from .links import default_source_type, fetch

            page = fetch(args.url, config)
            common["source_metadata"] = {**page.metadata(), "label": args.label or page.site or page.title}
            common["date_created"] = args.date or page.date
            item = ingest_text(page.text, source_type=args.source_type or default_source_type(page.final_url),
                               note=args.note, **common)
        elif args.text:
            item = ingest_text(args.text, source_type=args.source_type or "braindump", note=args.note, **common)
        elif args.file:
            item = ingest_file(args.file.read_bytes(), args.file.name, source_type=args.source_type,
                               note=args.note, **common)
        else:
            sys.exit("Give a file, --text or --url.")
        _print(item.to_result(include_text=False))
    elif args.command in ("sync-drive", "sync-clickup", "sync-email"):
        from . import syncs

        print(syncs.sync(args.command.removeprefix("sync-"), config, store))
        _relate(config, store)
    elif args.command == "relate":
        _relate(config, store)
    elif args.command == "search":
        items = store.search(SearchQuery(text=args.text, themes=args.theme, narratives=args.narrative,
                                         authors=args.author, visibility=args.visibility, limit=args.limit))
        _print([i.to_result(include_text=False) for i in items])
    elif args.command == "coverage":
        _print(analytics.coverage(store, config, args.visibility))
    elif args.command == "trends":
        _print(analytics.trends(store, args.days))


if __name__ == "__main__":
    main()


def _relate(config, store) -> None:
    """After a sync, the new items are linked to related ones from other sources. A failure here
    never fails the sync."""
    from . import related

    try:
        print(f"Looked for related items for {related.catch_up(config, store)} new items.")
    except Exception as exc:  # noqa: BLE001
        print(f"Couldn't look for related items: {exc}")
