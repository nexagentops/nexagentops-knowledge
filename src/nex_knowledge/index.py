"""Rebuildable, local SQLite keyword projection over existing canonical exports."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
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


@dataclass(frozen=True)
class Snapshot:
    """One fully parsed import from one source path; authority is explicit."""

    source: str
    scope: str
    records: tuple[Document, ...]
    authoritative: bool = False
    confirm_stale: str | None = None


def stale_confirmation_token(snapshot: Snapshot, missing_ids: list[str]) -> str:
    """Bind a confirmation to the exact source, contents, and stale candidates."""
    evidence = [snapshot.source, snapshot.scope,
                sorted((record.source_id, record.content_hash) for record in snapshot.records),
                sorted(missing_ids)]
    encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    indexed_at TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', first_seen_at TEXT,
    last_seen_at TEXT, last_seen_run_id TEXT, UNIQUE(source, source_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    title, body, content='documents', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS source_snapshots (
    run_id TEXT NOT NULL REFERENCES sync_runs(run_id), source TEXT NOT NULL,
    scope TEXT NOT NULL, authoritative INTEGER NOT NULL, record_count INTEGER NOT NULL,
    added INTEGER NOT NULL, updated INTEGER NOT NULL, unchanged INTEGER NOT NULL,
    reappeared INTEGER NOT NULL, missing INTEGER NOT NULL, stale_marked INTEGER NOT NULL,
    PRIMARY KEY (run_id, source, scope)
);
"""


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
    for name, definition in {
        "scope": "TEXT NOT NULL DEFAULT ''", "status": "TEXT NOT NULL DEFAULT 'active'",
        "first_seen_at": "TEXT", "last_seen_at": "TEXT", "last_seen_run_id": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
    connection.execute("CREATE INDEX IF NOT EXISTS documents_scope_status ON documents(source, scope, status)")
    if "first_seen_at" not in columns:
        connection.execute("UPDATE documents SET first_seen_at=indexed_at WHERE first_seen_at IS NULL")
    if "last_seen_at" not in columns:
        connection.execute("UPDATE documents SET last_seen_at=indexed_at WHERE last_seen_at IS NULL")
    connection.commit()


def sync_snapshots(db: Path, snapshots: Iterable[Snapshot]) -> dict[str, int | str]:
    snapshots = list(snapshots)
    scopes = [(snapshot.source, snapshot.scope) for snapshot in snapshots]
    if len(scopes) != len(set(scopes)):
        raise ValueError("duplicate source snapshot in input; index was not changed")
    records = [record for snapshot in snapshots for record in snapshot.records]
    keys = [(record.source, record.source_id) for record in records]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate source identity in input; index was not changed")
    if any(not snapshot.source or any(record.source != snapshot.source or not record.source_id
                                      for record in snapshot.records) for snapshot in snapshots):
        raise ValueError("snapshot source and record source IDs must agree")
    db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not db.exists():
        fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    counts: dict[str, int | str] = {"run_id": run_id, "added": 0, "updated": 0,
                                    "unchanged": 0, "reappeared": 0, "missing": 0,
                                    "stale_marked": 0, "missing_preserved": 0}
    with closing(sqlite3.connect(db)) as connection:
        _initialize(connection)
        with connection:
            connection.execute("INSERT INTO sync_runs(run_id, started_at) VALUES (?, ?)", (run_id, now))
            for snapshot in snapshots:
                local = {key: 0 for key in ("added", "updated", "unchanged", "reappeared", "missing", "stale_marked")}
                for record in snapshot.records:
                    previous = connection.execute(
                        "SELECT id, content_hash, source_path, status, scope FROM documents WHERE source=? AND source_id=?",
                        (record.source, record.source_id),
                    ).fetchone()
                    if previous is None:
                        cursor = connection.execute(
                            "INSERT INTO documents(source, source_id, title, body, source_path, created_at, updated_at, "
                            "content_hash, indexed_at, scope, status, first_seen_at, last_seen_at, last_seen_run_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                            (record.source, record.source_id, record.title, record.body, record.source_path,
                             record.created_at, record.updated_at, record.content_hash, now, snapshot.scope,
                             now, now, run_id),
                        )
                        rowid = cursor.lastrowid
                        local["added"] += 1
                        connection.execute("INSERT INTO document_fts(rowid, title, body) VALUES (?, ?, ?)",
                                           (rowid, record.title, record.body))
                        continue
                    rowid, old_hash, old_path, old_status, old_scope = previous
                    if old_scope != snapshot.scope and old_scope != "":
                        raise ValueError(f"source identity moved between snapshots: {record.source}:{record.source_id}")
                    if old_status == "stale":
                        local["reappeared"] += 1
                    changed = old_hash != record.content_hash or old_path != record.source_path
                    if changed:
                        local["updated"] += 1
                        connection.execute("DELETE FROM document_fts WHERE rowid=?", (rowid,))
                        connection.execute(
                            "UPDATE documents SET title=?, body=?, source_path=?, created_at=?, updated_at=?, "
                            "content_hash=?, indexed_at=?, scope=?, status='active', last_seen_at=?, last_seen_run_id=? WHERE id=?",
                            (record.title, record.body, record.source_path, record.created_at, record.updated_at,
                             record.content_hash, now, snapshot.scope, now, run_id, rowid),
                        )
                        connection.execute("INSERT INTO document_fts(rowid, title, body) VALUES (?, ?, ?)",
                                           (rowid, record.title, record.body))
                    else:
                        local["unchanged"] += 1
                        connection.execute(
                            "UPDATE documents SET scope=?, status='active', last_seen_at=?, last_seen_run_id=? WHERE id=?",
                            (snapshot.scope, now, run_id, rowid),
                        )
                local["missing"] = connection.execute(
                    "SELECT COUNT(*) FROM documents WHERE source=? AND scope=? AND "
                    "(last_seen_run_id IS NULL OR last_seen_run_id<>?)",
                    (snapshot.source, snapshot.scope, run_id),
                ).fetchone()[0]
                if snapshot.authoritative:
                    missing_active = [row[0] for row in connection.execute(
                        "SELECT source_id FROM documents WHERE source=? AND scope=? AND status='active' "
                        "AND (last_seen_run_id IS NULL OR last_seen_run_id<>?) ORDER BY source_id",
                        (snapshot.source, snapshot.scope, run_id),
                    )]
                    if missing_active and snapshot.confirm_stale != stale_confirmation_token(snapshot, missing_active):
                        token = stale_confirmation_token(snapshot, missing_active)
                        raise ValueError(
                            f"authoritative import would mark {len(missing_active)} record(s) stale; "
                            f"verify completeness and rerun with --confirm-stale {token}"
                        )
                    local["stale_marked"] = connection.execute(
                        "UPDATE documents SET status='stale' WHERE source=? AND scope=? AND status='active' "
                        "AND (last_seen_run_id IS NULL OR last_seen_run_id<>?)",
                        (snapshot.source, snapshot.scope, run_id),
                    ).rowcount
                else:
                    counts["missing_preserved"] += local["missing"]
                connection.execute(
                    "INSERT INTO source_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, snapshot.source, snapshot.scope, int(snapshot.authoritative),
                     len(snapshot.records), *(local[key] for key in
                                              ("added", "updated", "unchanged", "reappeared", "missing", "stale_marked"))),
                )
                for key, value in local.items():
                    counts[key] += value
            connection.execute("UPDATE sync_runs SET finished_at=? WHERE run_id=?",
                               (datetime.now(timezone.utc).isoformat(), run_id))
    return counts


def sync(db: Path, records: Iterable[Document], scanned_sources: Iterable[str] = ()) -> dict[str, int | str]:
    """Compatibility entry point: all imports are non-authoritative."""
    records = list(records)
    sources = {record.source for record in records} | set(scanned_sources)
    return sync_snapshots(db, [Snapshot(source, "", tuple(record for record in records if record.source == source))
                               for source in sorted(sources)])


def search(db: Path, query: str, limit: int = 20) -> list[dict[str, str | None]]:
    """Legacy Python entry point, retaining all-status behavior."""
    from .search_service import SearchRequest, search_documents

    return search_documents(db, SearchRequest(query=query, limit=limit, status="all"))["results"]


def recent_snapshots(db: Path, limit: int = 20) -> list[dict[str, str | int]]:
    """Inspect committed import decisions without exposing indexed content."""
    if not db.is_file():
        raise ValueError(f"index does not exist: {db}")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT s.*, r.finished_at FROM source_snapshots s JOIN sync_runs r ON r.run_id=s.run_id "
            "ORDER BY r.finished_at DESC, s.run_id DESC, s.source, s.scope LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
