"""One-shot, local JSON protocol for trusted read-only search consumers."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import BinaryIO, TextIO

from .search_service import SearchRequest, search_documents


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 8192
REQUEST_FIELDS = {"query", "limit", "source", "scope", "from_date", "to_date", "status"}


class ConsumerError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConsumerError("invalid_request", "duplicate JSON key")
        result[key] = value
    return result


def parse_request(raw: bytes) -> SearchRequest:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ConsumerError("invalid_request", "request must contain 1 to 8192 bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsumerError("invalid_request", "request must be a UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise ConsumerError("invalid_request", "request must be a JSON object")
    if "query" not in value or set(value) - REQUEST_FIELDS:
        raise ConsumerError("invalid_request", "request requires query and only supported search fields")
    return SearchRequest(**value)


def handle_request(db: Path, raw: bytes) -> dict:
    """Parse one request and delegate retrieval to the existing search service."""
    request = parse_request(raw)
    if not db.is_absolute():
        raise ConsumerError("invalid_configuration", "database path must be absolute")
    if not db.is_file():
        raise ConsumerError("database_unavailable", "knowledge index is unavailable")
    try:
        response = search_documents(db, request)
    except ValueError as exc:
        raise ConsumerError("invalid_request", str(exc)) from exc
    except sqlite3.Error as exc:
        raise ConsumerError("search_error", "search could not be completed") from exc
    return {"protocol_version": PROTOCOL_VERSION, **response}


def main(argv: list[str] | None = None, *, input_stream: BinaryIO | None = None,
         output_stream: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nex-knowledge-consumer",
                                     description="One-shot, read-only local JSON search adapter")
    parser.add_argument("--db", type=Path, required=True, help="trusted absolute path to the local index")
    args = parser.parse_args(argv)
    input_stream = input_stream if input_stream is not None else sys.stdin.buffer
    output_stream = output_stream if output_stream is not None else sys.stdout
    try:
        response = handle_request(args.db, input_stream.read(MAX_REQUEST_BYTES + 1))
        code = 0
    except ConsumerError as exc:
        response = {"protocol_version": PROTOCOL_VERSION,
                    "error": {"code": exc.code, "message": str(exc)}}
        code = 2
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), file=output_stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
