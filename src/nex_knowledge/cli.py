"""Explicit, read-only source ingestion and local keyword search CLI."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .index import Snapshot, conversations, markdown, notes, recent_snapshots, search, sync_snapshots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nex-knowledge")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("sync", help="index explicitly selected local exports")
    ingest.add_argument("--db", type=Path, required=True)
    ingest.add_argument("--notes-jsonl", type=Path, action="append", default=[])
    ingest.add_argument("--authoritative-notes-jsonl", type=Path, action="append", default=[])
    ingest.add_argument("--chatgpt-archive", type=Path, action="append", default=[])
    ingest.add_argument("--authoritative-chatgpt-archive", type=Path, action="append", default=[])
    ingest.add_argument("--markdown-dir", type=Path, action="append", default=[])
    ingest.add_argument("--authoritative-markdown-dir", type=Path, action="append", default=[])
    lookup = commands.add_parser("search", help="search the local index")
    lookup.add_argument("--db", type=Path, required=True)
    lookup.add_argument("query")
    lookup.add_argument("--limit", type=int, default=20)
    history = commands.add_parser("snapshots", help="inspect recent source snapshots")
    history.add_argument("--db", type=Path, required=True)
    history.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            print(json.dumps(search(args.db, args.query, args.limit), ensure_ascii=False, indent=2))
            return 0
        if args.command == "snapshots":
            print(json.dumps(recent_snapshots(args.db, args.limit), ensure_ascii=False, indent=2))
            return 0
        selected_notes = [(path, False) for path in args.notes_jsonl]
        selected_notes += [(path, True) for path in args.authoritative_notes_jsonl]
        selected_archives = [(path, False) for path in args.chatgpt_archive]
        selected_archives += [(path, True) for path in args.authoritative_chatgpt_archive]
        selected_markdown = [(path, False) for path in args.markdown_dir]
        selected_markdown += [(path, True) for path in args.authoritative_markdown_dir]
        if not (selected_notes or selected_archives or selected_markdown):
            parser.error("sync requires at least one source")
        generated = [root / "markdown" for root, _ in selected_archives]
        generated += [path.parent / "markdown" for path, _ in selected_notes]
        for root, _ in selected_markdown:
            if any(root.resolve() == item.resolve() or item.resolve().is_relative_to(root.resolve())
                   or root.resolve().is_relative_to(item.resolve())
                   for item in generated):
                raise ValueError("Markdown directory overlaps a generated source export")
        snapshots = []
        for path, authoritative in selected_notes:
            snapshots.append(Snapshot("apple_notes", str(path.resolve()), tuple(notes(path)), authoritative))
        for root, authoritative in selected_archives:
            snapshots.append(Snapshot("chatgpt_archive", str(root.resolve()),
                                      tuple(conversations(root)), authoritative))
        for root, authoritative in selected_markdown:
            snapshots.append(Snapshot("markdown", str(root.resolve()), tuple(markdown(root)), authoritative))
        print(json.dumps(sync_snapshots(args.db, snapshots), sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
