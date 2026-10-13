import json
import tempfile
import unittest
from pathlib import Path

from nex_knowledge.cli import main
from nex_knowledge.index import Document, conversations, markdown, notes, search, sync


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / "private" / "index.db"

    def test_three_sources_and_incremental_update(self):
        note_path = self.root / "notes.jsonl"
        note_path.write_text(json.dumps({"id": "n1", "title": "Garden", "body_text": "orchid notes",
                                         "created_at": "2024-01-01", "updated_at": "2024-02-01"}) + "\n")
        archive = self.root / "archive"
        (archive / "raw").mkdir(parents=True)
        (archive / "raw" / "one.json").write_text(json.dumps({
            "conversation_id": "c1", "canonical_conversation_id": "cc_123",
            "title": "Garden chat", "messages": [{"role": "user", "text": "orchid advice"}],
            "created_at": "2024-03-01"}))
        md = self.root / "docs"
        md.mkdir()
        (md / "plan.md").write_text("orchid planting plan")
        records = notes(note_path) + conversations(archive) + markdown(md)
        self.assertEqual(sync(self.db, records)["added"], 3)
        self.assertEqual(sync(self.db, records)["unchanged"], 3)
        found = search(self.db, "orchid")
        self.assertEqual({row["source"] for row in found}, {"apple_notes", "chatgpt_archive", "markdown"})
        self.assertEqual(next(row for row in found if row["source"] == "chatgpt_archive")["source_id"], "cc_123")
        self.assertEqual(next(row for row in found if row["source"] == "apple_notes")["updated_at"], "2024-02-01")
        revised = [Document("apple_notes", "n1", "Garden", "lily notes", str(note_path))]
        self.assertEqual(sync(self.db, revised), {"added": 0, "updated": 1, "unchanged": 0,
                                                   "missing_preserved": 0})
        self.assertEqual(len(search(self.db, "orchid")), 2)
        self.assertEqual(len(search(self.db, "lily")), 1)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_duplicate_input_fails_without_mutating_index(self):
        record = Document("apple_notes", "n1", "Title", "body", "notes.jsonl")
        sync(self.db, [record])
        with self.assertRaisesRegex(ValueError, "duplicate source identity"):
            sync(self.db, [record, record])
        self.assertEqual(len(search(self.db, "body")), 1)

    def test_missing_record_is_preserved(self):
        first = Document("markdown", "a", "First", "keptword", "a.md")
        second = Document("markdown", "b", "Second", "otherword", "b.md")
        sync(self.db, [first, second])
        counts = sync(self.db, [first])
        self.assertEqual(counts["missing_preserved"], 1)
        self.assertEqual(len(search(self.db, "otherword")), 1)
        self.assertEqual(sync(self.db, [], ["markdown"])["missing_preserved"], 2)
        self.assertEqual(len(search(self.db, "keptword")), 1)

    def test_malformed_source_cannot_partially_sync(self):
        path = self.root / "notes.jsonl"
        path.write_text('{"id":"good","body_text":"hello"}\n{"id":\n')
        with self.assertRaises(ValueError):
            sync(self.db, notes(path))
        self.assertFalse(self.db.exists())

    def test_cli_rejects_generated_markdown_overlap(self):
        path = self.root / "processed" / "notes.jsonl"
        path.parent.mkdir()
        path.write_text("")
        generated = path.parent / "markdown"
        generated.mkdir()
        with self.assertRaises(SystemExit) as raised:
            main(["sync", "--db", str(self.db), "--notes-jsonl", str(path),
                  "--markdown-dir", str(generated)])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
