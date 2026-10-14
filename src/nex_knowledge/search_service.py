"""Bounded, read-only JSON search contract over the local FTS5 projection."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int = 20
    source: str | None = None
    scope: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    status: str = "active"


def _validate(request: SearchRequest) -> None:
    if not isinstance(request.query, str) or not request.query.strip():
        raise ValueError("query must not be empty")
    if len(request.query) > 512 or "\x00" in request.query:
        raise ValueError("query must be at most 512 characters without NUL bytes")
    if isinstance(request.limit, bool) or not isinstance(request.limit, int) or not 1 <= request.limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if not isinstance(request.status, str) or request.status not in {"active", "stale", "all"}:
        raise ValueError("status must be active, stale, or all")
    for name in ("source", "scope"):
        value = getattr(request, name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{name} must be a nonempty string")
    for name in ("from_date", "to_date"):
        value = getattr(request, name)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{name} must be YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{name} must be a valid calendar date") from exc
    if request.from_date and request.to_date and request.from_date > request.to_date:
        raise ValueError("from_date must not be later than to_date")


def _document_id(source: str, source_id: str) -> str:
    identity = f"nexagentops-knowledge:v1\0{source}\0{source_id}".encode("utf-8")
    return "kd_" + hashlib.sha256(identity).hexdigest()


def search_documents(db: Path, request: SearchRequest) -> dict:
    """Search without schema creation or writes; response is JSON serializable."""
    _validate(request)
    if not db.is_file():
        raise ValueError(f"index does not exist: {db}")
    clauses = ["document_fts MATCH ?"]
    parameters: list[str | int] = [request.query.strip()]
    if request.status != "all":
        clauses.append("d.status=?")
        parameters.append(request.status)
    if request.source is not None:
        clauses.append("d.source=?")
        parameters.append(request.source)
    if request.scope is not None:
        clauses.append("d.scope=?")
        parameters.append(request.scope)
    document_date = "date(COALESCE(NULLIF(d.updated_at, ''), NULLIF(d.created_at, '')))"
    if request.from_date is not None:
        clauses.append(f"{document_date}>=?")
        parameters.append(request.from_date)
    if request.to_date is not None:
        clauses.append(f"{document_date}<=?")
        parameters.append(request.to_date)
    parameters.append(request.limit)
    sql = (
        "SELECT d.source, d.source_id, d.scope, d.title, d.source_path, d.created_at, d.updated_at, "
        "d.status, snippet(document_fts, -1, '[', ']', '…', 18) AS snippet, "
        "bm25(document_fts) AS rank FROM document_fts "
        "JOIN documents d ON d.id=document_fts.rowid WHERE " + " AND ".join(clauses) +
        " ORDER BY rank, d.source, d.source_id LIMIT ?"
    )
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        if not {"scope", "status"}.issubset(columns):
            raise ValueError("index schema is older; run a local sync to upgrade before searching")
        rows = connection.execute(sql, parameters).fetchall()
    results = [{
        "document_id": _document_id(row["source"], row["source_id"]),
        "source": row["source"], "source_id": row["source_id"], "scope": row["scope"],
        "title": row["title"], "source_path": row["source_path"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "status": row["status"], "snippet": row["snippet"][:320],
        "score": -float(row["rank"]),
    } for row in rows]
    filters = asdict(request)
    filters.pop("query")
    return {"query": request.query.strip(), "filters": filters,
            "count": len(results), "results": results}
