import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from nex_knowledge.cli import main
from nex_knowledge.index import Document, Snapshot, search, stale_confirmation_token, sync_snapshots
from nex_knowledge.search_service import SearchRequest, search_documents


class SearchServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "private" / "knowledge.db"
        self.note = Document("apple_notes", "n1", "Garden", "orchid care", "notes.jsonl",
                             "2024-01-01", "2024-03-01")
        self.chat = Document("chatgpt_archive", "c1", "Garden", "orchid care", "chat.json",
                             "2024-02-01", "2024-04-01")
        self.old = Document("markdown", "m1", "Garden", "orchid care", "old.md",
                            "2023-01-01", None)
        sync_snapshots(self.db, [Snapshot("apple_notes", "notes.jsonl", (self.note,), True),
                                 Snapshot("chatgpt_archive", "archive", (self.chat,), True),
                                 Snapshot("markdown", "docs", (self.old,), True)])
        pending = Snapshot("markdown", "docs", (), True)
        sync_snapshots(self.db, [Snapshot("markdown", "docs", (), True,
                                          stale_confirmation_token(pending, ["m1"]))])

    def test_default_active_status_and_stable_document_ids(self):
        response = search_documents(self.db, SearchRequest("orchid"))
        self.assertEqual(response["count"], 2)
        self.assertEqual({row["status"] for row in response["results"]}, {"active"})
        self.assertEqual(len({row["document_id"] for row in response["results"]}), 2)
        self.assertTrue(all(row["document_id"].startswith("kd_") for row in response["results"]))
        self.assertTrue(all("[orchid]" in row["snippet"] for row in response["results"]))
        self.assertTrue(all(isinstance(row["score"], float) for row in response["results"]))
        self.assertTrue(all("body" not in row for row in response["results"]))
        self.assertEqual(search_documents(self.db, SearchRequest("orchid"))["results"], response["results"])

    def test_status_source_and_scope_filters(self):
        stale = search_documents(self.db, SearchRequest("orchid", status="stale"))["results"]
        self.assertEqual([(row["source"], row["scope"]) for row in stale], [("markdown", "docs")])
        self.assertEqual(search_documents(self.db, SearchRequest("orchid", status="all"))["count"], 3)
        chat = search_documents(self.db, SearchRequest("orchid", source="chatgpt_archive", scope="archive"))
        self.assertEqual([row["source_id"] for row in chat["results"]], ["c1"])
        self.assertEqual(search_documents(self.db, SearchRequest("orchid", source="chatgpt_archive",
                                                                 scope="notes.jsonl"))["count"], 0)

    def test_date_filters_and_no_matches(self):
        march = search_documents(self.db, SearchRequest("orchid", from_date="2024-03-01",
                                                        to_date="2024-03-31"))
        self.assertEqual([row["source_id"] for row in march["results"]], ["n1"])
        old = search_documents(self.db, SearchRequest("orchid", to_date="2023-12-31", status="all"))
        self.assertEqual([row["source_id"] for row in old["results"]], ["m1"])
        self.assertEqual(search_documents(self.db, SearchRequest("unmatchedterm"))["results"], [])

    def test_limit_and_deterministic_tie_order(self):
        response = search_documents(self.db, SearchRequest("orchid", limit=1, status="all"))
        self.assertEqual(response["count"], 1)
        all_results = search_documents(self.db, SearchRequest("orchid", status="all"))["results"]
        self.assertEqual(response["results"], all_results[:1])
        self.assertEqual([row["source"] for row in all_results],
                         ["apple_notes", "chatgpt_archive", "markdown"])

    def test_invalid_parameters_and_unavailable_database(self):
        bad = [SearchRequest(""), SearchRequest("   "), SearchRequest("orchid", limit=0),
               SearchRequest("orchid", limit=101), SearchRequest("orchid", limit=True),
               SearchRequest("orchid", status="unknown"), SearchRequest("orchid", source=""),
               SearchRequest("orchid", from_date="2024-99-01"),
               SearchRequest("orchid", from_date="2024-05-01", to_date="2024-04-01")]
        for request in bad:
            with self.subTest(request=request), self.assertRaises(ValueError):
                search_documents(self.db, request)
        with self.assertRaises(ValueError):
            search_documents(self.root / "missing.db", SearchRequest("orchid"))
        self.assertFalse((self.root / "missing.db").exists())
        with self.assertRaises(sqlite3.Error):
            search_documents(self.db, SearchRequest('"unterminated'))

    def test_legacy_index_requires_explicit_upgrade(self):
        legacy = self.root / "legacy.db"
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT)")
        with self.assertRaisesRegex(ValueError, "run a local sync"):
            search_documents(legacy, SearchRequest("orchid"))

    def test_read_only_connection_and_no_document_content_in_response(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        actual_connect = sqlite3.connect
        calls = []

        def observed_connect(*args, **kwargs):
            calls.append((args, kwargs))
            return actual_connect(*args, **kwargs)

        with patch("nex_knowledge.search_service.sqlite3.connect", side_effect=observed_connect):
            result = search_documents(self.db, SearchRequest("orchid"))
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0][0].endswith("?mode=ro"))
        self.assertTrue(calls[0][1]["uri"])
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_cli_returns_structured_json(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["search", "--db", str(self.db), "--source", "apple_notes",
                                   "--from-date", "2024-01-01", "orchid"]), 0)
        response = json.loads(output.getvalue())
        self.assertEqual(response["filters"]["status"], "active")
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["results"][0]["source_id"], "n1")


if __name__ == "__main__":
    unittest.main()
