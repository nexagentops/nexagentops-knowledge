"""Explicit, read-only source ingestion and local keyword search CLI."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .index import conversations, markdown, notes, search, sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nex-knowledge")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("sync", help="index explicitly selected local exports")
    ingest.add_argument("--db", type=Path, required=True)
    ingest.add_argument("--notes-jsonl", type=Path, action="append", default=[])
    ingest.add_argument("--chatgpt-archive", type=Path, action="append", default=[])
    ingest.add_argument("--markdown-dir", type=Path, action="append", default=[])
    lookup = commands.add_parser("search", help="search the local index")
    lookup.add_argument("--db", type=Path, required=True)
    lookup.add_argument("query")
    lookup.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            print(json.dumps(search(args.db, args.query, args.limit), ensure_ascii=False, indent=2))
            return 0
        if not (args.notes_jsonl or args.chatgpt_archive or args.markdown_dir):
            parser.error("sync requires at least one source")
        generated = [root / "markdown" for root in args.chatgpt_archive]
        generated += [path.parent / "markdown" for path in args.notes_jsonl]
        for root in args.markdown_dir:
            if any(root.resolve() == item.resolve() or item.resolve().is_relative_to(root.resolve())
                   or root.resolve().is_relative_to(item.resolve())
                   for item in generated):
                raise ValueError("Markdown directory overlaps a generated source export")
        records = []
        for path in args.notes_jsonl:
            records.extend(notes(path))
        for root in args.chatgpt_archive:
            records.extend(conversations(root))
        for root in args.markdown_dir:
            records.extend(markdown(root))
        scanned_sources = []
        if args.notes_jsonl:
            scanned_sources.append("apple_notes")
        if args.chatgpt_archive:
            scanned_sources.append("chatgpt_archive")
        if args.markdown_dir:
            scanned_sources.append("markdown")
        print(json.dumps(sync(args.db, records, scanned_sources), sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
