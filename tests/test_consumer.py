import hashlib
import json
import socket
import tempfile
import unittest
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from nex_knowledge.consumer import handle_request, main
from nex_knowledge.index import Document, Snapshot, stale_confirmation_token, sync_snapshots
from nex_knowledge.search_service import SearchRequest, search_documents


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "private" / "knowledge.db"
        long_body = "orchid " + "privateparagraph " * 100
        records = (
            Snapshot("apple_notes", "notes.jsonl", (
                Document("apple_notes", "n1", "Garden", long_body, "notes.jsonl",
                         "2024-01-01", "2024-03-01"),), True),
            Snapshot("chatgpt_archive", "archive", (
                Document("chatgpt_archive", "c1", "Garden chat", "orchid advice", "chat.json",
                         "2024-02-01", "2024-04-01"),), True),
            Snapshot("markdown", "docs", (
                Document("markdown", "m1", "Old garden", "orchid history", "old.md",
                         "2023-01-01"),), True),
        )
        sync_snapshots(self.db, records)
        pending = Snapshot("markdown", "docs", (), True)
        sync_snapshots(self.db, [Snapshot("markdown", "docs", (), True,
                                          stale_confirmation_token(pending, ["m1"]))])

    def request(self, value):
        return handle_request(self.db, json.dumps(value).encode("utf-8"))

    def invoke(self, raw: bytes, db: Path | None = None):
        output = StringIO()
        code = main(["--db", str(db or self.db)], input_stream=BytesIO(raw), output_stream=output)
        return code, json.loads(output.getvalue())

    def test_successful_query_and_active_default(self):
        response = self.request({"query": "orchid"})
        self.assertEqual(response["protocol_version"], 1)
        self.assertEqual(response["count"], 2)
        self.assertEqual(response["filters"]["status"], "active")
        self.assertEqual({row["source"] for row in response["results"]},
                         {"apple_notes", "chatgpt_archive"})
        self.assertTrue(all(row["document_id"].startswith("kd_") for row in response["results"]))

    def test_no_results_and_explicit_stale(self):
        self.assertEqual(self.request({"query": "absentword"})["results"], [])
        stale = self.request({"query": "orchid", "status": "stale"})
        self.assertEqual([(row["source"], row["status"]) for row in stale["results"]],
                         [("markdown", "stale")])

    def test_filters_are_forwarded_to_existing_search_service(self):
        payload = {"query": "orchid", "limit": 5, "source": "apple_notes", "scope": "notes.jsonl",
                   "from_date": "2024-03-01", "to_date": "2024-03-31", "status": "active"}
        with patch("nex_knowledge.consumer.search_documents", wraps=search_documents) as delegated:
            response = self.request(payload)
        delegated.assert_called_once_with(self.db, SearchRequest(**payload))
        self.assertEqual([row["source_id"] for row in response["results"]], ["n1"])
        self.assertEqual(response["filters"]["scope"], "notes.jsonl")

    def test_result_limit_and_bounded_snippet(self):
        response = self.request({"query": "orchid", "limit": 1})
        self.assertEqual(response["count"], 1)
        all_results = self.request({"query": "orchid", "status": "all"})["results"]
        self.assertEqual(len(all_results), 3)
        self.assertTrue(all(len(row["snippet"]) <= 320 for row in all_results))
        self.assertNotIn("privateparagraph " * 100, json.dumps(all_results))
        self.assertTrue(all("body" not in row for row in all_results))

    def test_malformed_requests_fail_with_structured_errors(self):
        invalid = [b"", b"{", b"[]", b'{"query":"orchid","query":"rose"}',
                   b'{"query":"orchid","db":"another.db"}', b'{"query":""}',
                   b'{"query":"orchid","limit":101}', b'{"query":"orchid","status":[]}',
                   b"a" * 8193]
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                code, response = self.invoke(raw)
                self.assertEqual(code, 2)
                self.assertEqual(response["error"]["code"], "invalid_request")
                self.assertNotIn("results", response)

    def test_unavailable_database_and_untrusted_relative_path(self):
        code, unavailable = self.invoke(b'{"query":"orchid"}', self.root / "missing.db")
        self.assertEqual(code, 2)
        self.assertEqual(unavailable["error"]["code"], "database_unavailable")
        self.assertFalse((self.root / "missing.db").exists())
        code, relative = self.invoke(b'{"query":"orchid"}', Path("relative.db"))
        self.assertEqual(code, 2)
        self.assertEqual(relative["error"]["code"], "invalid_configuration")

    def test_database_remains_unchanged_and_no_network_is_used(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with patch.object(socket, "socket", side_effect=AssertionError("network used")):
            response = self.request({"query": "orchid"})
        self.assertEqual(response["count"], 2)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_deterministic_one_line_output(self):
        raw = b'{"query":"orchid","source":"apple_notes"}'
        first = StringIO()
        second = StringIO()
        self.assertEqual(main(["--db", str(self.db)], input_stream=BytesIO(raw), output_stream=first), 0)
        self.assertEqual(main(["--db", str(self.db)], input_stream=BytesIO(raw), output_stream=second), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        self.assertEqual(first.getvalue().count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
