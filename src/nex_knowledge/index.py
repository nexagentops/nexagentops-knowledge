"""Rebuildable, local SQLite keyword projection over existing canonical exports."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Document:
    source: str
    source_id: str
    title: str
    body: str
    source_path: str
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def content_hash(self) -> str:
        payload = json.dumps([self.title, self.body, self.created_at, self.updated_at], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def notes(path: Path) -> list[Document]:
    result = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("record must be an object")
                note_id = _text(item.get("id"), "id")
                if not note_id:
                    raise ValueError("id is empty")
                result.append(Document(
                    "apple_notes", note_id, _text(item.get("title", ""), "title"),
                    _text(item.get("body_text", ""), "body_text"), str(path.resolve()),
                    item.get("created_at") or None, item.get("updated_at") or None,
                ))
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc
    return result


def conversations(root: Path) -> list[Document]:
    raw = root / "raw"
    if not raw.is_dir():
        raise ValueError(f"ChatGPT archive raw directory is missing: {raw}")
    result = []
    for path in sorted(raw.glob("*.json")):
        if path.is_symlink():
            raise ValueError(f"symlinked archive record is unsupported: {path}")
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                raise ValueError("record must be an object")
            provider = _text(item.get("provider", "chatgpt"), "provider")
            provider_id = _text(item.get("provider_conversation_id") or item.get("conversation_id"), "conversation_id")
            if not provider_id:
                raise ValueError("conversation_id is empty")
            canonical_id = item.get("canonical_conversation_id")
            source_id = _text(canonical_id, "canonical_conversation_id") if canonical_id else f"{provider}:{provider_id}"
            messages = item.get("messages", [])
            if not isinstance(messages, list):
                raise ValueError("messages must be a list")
            parts = []
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError("message must be an object")
                role = _text(message.get("role", ""), "role")
                body = _text(message.get("text", ""), "message text")
                parts.append(f"{role}: {body}" if role else body)
            result.append(Document(
                "chatgpt_archive", source_id, _text(item.get("title", ""), "title"),
                "\n\n".join(parts), str(path.resolve()),
                item.get("created_at") or None, item.get("updated_at") or None,
            ))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: {exc}") from exc
    return result


def markdown(root: Path) -> list[Document]:
    if not root.is_dir():
        raise ValueError(f"Markdown directory is missing: {root}")
    result = []
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink():
            raise ValueError(f"symlinked Markdown file is unsupported: {path}")
        if not path.is_file():
            continue
        resolved = path.resolve()
        body = path.read_text(encoding="utf-8")
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        result.append(Document("markdown", str(resolved), path.stem, body, str(resolved), None, modified))
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL, source_path TEXT NOT NULL,
    created_at TEXT, updated_at TEXT, content_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL, UNIQUE(source, source_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    title, body, content='documents', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
"""


def sync(db: Path, records: Iterable[Document], scanned_sources: Iterable[str] = ()) -> dict[str, int]:
    records = list(records)
    keys = [(record.source, record.source_id) for record in records]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate source identity in input; index was not changed")
    db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not db.exists():
        fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    counts = {"added": 0, "updated": 0, "unchanged": 0, "missing_preserved": 0}
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN")
        seen = set(keys)
        touched_sources = {source for source, _ in keys} | set(scanned_sources)
        for record in records:
            previous = connection.execute(
                "SELECT id, content_hash, source_path FROM documents WHERE source=? AND source_id=?",
                (record.source, record.source_id),
            ).fetchone()
            if previous and previous[1] == record.content_hash and previous[2] == record.source_path:
                counts["unchanged"] += 1
                continue
            timestamp = datetime.now(timezone.utc).isoformat()
            if previous:
                connection.execute("DELETE FROM document_fts WHERE rowid=?", (previous[0],))
                connection.execute(
                    "UPDATE documents SET title=?, body=?, source_path=?, created_at=?, updated_at=?, content_hash=?, indexed_at=? WHERE id=?",
                    (record.title, record.body, record.source_path, record.created_at, record.updated_at,
                     record.content_hash, timestamp, previous[0]),
                )
                rowid = previous[0]
                counts["updated"] += 1
            else:
                cursor = connection.execute(
                    "INSERT INTO documents(source, source_id, title, body, source_path, created_at, updated_at, content_hash, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record.source, record.source_id, record.title, record.body, record.source_path,
                     record.created_at, record.updated_at, record.content_hash, timestamp),
                )
                rowid = cursor.lastrowid
                counts["added"] += 1
            connection.execute("INSERT INTO document_fts(rowid, title, body) VALUES (?, ?, ?)",
                               (rowid, record.title, record.body))
        for source, source_id in connection.execute("SELECT source, source_id FROM documents"):
            if source in touched_sources and (source, source_id) not in seen:
                counts["missing_preserved"] += 1
    return counts


def search(db: Path, query: str, limit: int = 20) -> list[dict[str, str | None]]:
    if not db.is_file():
        raise ValueError(f"index does not exist: {db}")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT d.source, d.source_id, d.title, d.source_path, d.created_at, d.updated_at, "
            "snippet(document_fts, 1, '[', ']', '…', 18) AS snippet "
            "FROM document_fts JOIN documents d ON d.id=document_fts.rowid "
            "WHERE document_fts MATCH ? ORDER BY bm25(document_fts), d.source, d.source_id LIMIT ?",
            (query, limit),
        ).fetchall()
        return [dict(row) for row in rows]
