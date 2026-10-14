import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from nex_knowledge.cli import main
from nex_knowledge.index import (Document, Snapshot, conversations, markdown, notes, recent_snapshots,
                                 search, stale_confirmation_token, sync, sync_snapshots)


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
        changed = sync(self.db, revised)
        self.assertEqual([changed[key] for key in ("added", "updated", "unchanged", "missing_preserved")],
                         [0, 1, 0, 0])
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
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["sync", "--db", str(self.db), "--notes-jsonl", str(path),
                  "--markdown-dir", str(generated)])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.db.exists())

    def test_authority_stale_and_reappearance(self):
        first = Document("apple_notes", "n1", "Plan", "violet", "notes.jsonl", "2024-01-01")
        second = Document("apple_notes", "n2", "Todo", "rose", "notes.jsonl")
        snapshot = lambda records, authoritative=False: Snapshot(
            "apple_notes", "notes.jsonl", tuple(records), authoritative)
        added = sync_snapshots(self.db, [snapshot([first, second], True)])
        self.assertEqual(added["added"], 2)
        self.assertEqual(added["stale_marked"], 0)
        unchanged = sync_snapshots(self.db, [snapshot([first, second], True)])
        self.assertEqual(unchanged["unchanged"], 2)
        modified = Document("apple_notes", "n1", "Plan", "violet and iris", "notes.jsonl", "2024-01-01")
        partial = sync_snapshots(self.db, [snapshot([modified])])
        self.assertEqual(partial["updated"], 1)
        self.assertEqual(partial["missing"], 1)
        self.assertEqual(partial["stale_marked"], 0)
        self.assertEqual(next(row for row in search(self.db, "rose"))["status"], "active")
        with self.assertRaisesRegex(ValueError, "verify completeness"):
            sync_snapshots(self.db, [snapshot([modified], True)])
        token = stale_confirmation_token(snapshot([modified], True), ["n2"])
        authoritative = sync_snapshots(self.db, [Snapshot("apple_notes", "notes.jsonl", (modified,), True, token)])
        self.assertEqual(authoritative["missing"], 1)
        self.assertEqual(authoritative["stale_marked"], 1)
        self.assertEqual(next(row for row in search(self.db, "rose"))["status"], "stale")
        repeated = sync_snapshots(self.db, [snapshot([modified], True)])
        self.assertEqual(repeated["missing"], 1)
        self.assertEqual(repeated["stale_marked"], 0)
        reappeared = sync_snapshots(self.db, [snapshot([modified, second])])
        self.assertEqual(reappeared["reappeared"], 1)
        self.assertEqual(reappeared["unchanged"], 2)
        self.assertEqual(next(row for row in search(self.db, "rose"))["status"], "active")
        history = recent_snapshots(self.db)
        self.assertEqual(history[0]["run_id"], reappeared["run_id"])
        self.assertEqual(history[0]["authoritative"], 0)
        self.assertEqual(history[0]["reappeared"], 1)
        with closing(sqlite3.connect(self.db)) as connection:
            seen = connection.execute("SELECT first_seen_at, last_seen_at, last_seen_run_id "
                                      "FROM documents WHERE source_id='n2'").fetchone()
            self.assertLessEqual(seen[0], seen[1])
            self.assertEqual(seen[2], reappeared["run_id"])

    def test_same_content_from_different_sources_stays_separate(self):
        one = Document("apple_notes", "shared", "Same", "identical body", "notes.jsonl")
        two = Document("markdown", "shared", "Same", "identical body", "same.md")
        counts = sync_snapshots(self.db, [Snapshot("apple_notes", "notes.jsonl", (one,), True),
                                          Snapshot("markdown", "docs", (two,), True)])
        self.assertEqual(counts["added"], 2)
        self.assertEqual({row["source"] for row in search(self.db, "identical")},
                         {"apple_notes", "markdown"})

    def test_scopes_do_not_stale_each_other(self):
        first = Document("markdown", "a", "A", "alpha", "one/a.md")
        second = Document("markdown", "b", "B", "beta", "two/b.md")
        sync_snapshots(self.db, [Snapshot("markdown", "one", (first,), True),
                                 Snapshot("markdown", "two", (second,), True)])
        pending = Snapshot("markdown", "one", (), True)
        token = stale_confirmation_token(pending, ["a"])
        result = sync_snapshots(self.db, [Snapshot("markdown", "one", (), True, token)])
        self.assertEqual(result["stale_marked"], 1)
        self.assertEqual(next(row for row in search(self.db, "beta"))["status"], "active")

    def test_duplicate_id_across_snapshots_is_rejected_before_writes(self):
        record = Document("markdown", "same", "Title", "body", "a.md")
        with self.assertRaisesRegex(ValueError, "duplicate source identity"):
            sync_snapshots(self.db, [Snapshot("markdown", "one", (record,)),
                                     Snapshot("markdown", "two", (record,))])
        self.assertFalse(self.db.exists())

    def test_scope_collision_rolls_back_whole_run(self):
        original = Document("markdown", "a", "Original", "oldword", "one/a.md")
        sync_snapshots(self.db, [Snapshot("markdown", "one", (original,))])
        other = Document("apple_notes", "n1", "New", "newword", "notes.jsonl")
        with self.assertRaisesRegex(ValueError, "moved between snapshots"):
            sync_snapshots(self.db, [Snapshot("apple_notes", "notes.jsonl", (other,)),
                                     Snapshot("markdown", "two", (original,))])
        self.assertEqual(search(self.db, "newword"), [])
        self.assertEqual(len(recent_snapshots(self.db)), 1)

    def test_existing_index_migrates_without_losing_search(self):
        self.db.parent.mkdir()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript("""
                CREATE TABLE documents (id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, source_path TEXT NOT NULL,
                    created_at TEXT, updated_at TEXT, content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL,
                    UNIQUE(source, source_id));
                CREATE VIRTUAL TABLE document_fts USING fts5(title, body, content='documents', content_rowid='id');
                INSERT INTO documents VALUES (1, 'apple_notes', 'old', 'Old', 'legacyword', 'old.jsonl',
                    NULL, NULL, 'hash', '2024-01-01');
                INSERT INTO document_fts(rowid, title, body) VALUES (1, 'Old', 'legacyword');
            """)
        sync_snapshots(self.db, [Snapshot("apple_notes", "new.jsonl", (), True)])
        self.assertEqual(next(row for row in search(self.db, "legacyword"))["status"], "active")
        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute("SELECT scope, first_seen_at FROM documents WHERE source_id='old'").fetchone()
            self.assertEqual(row, ("", "2024-01-01"))

    def test_cli_authoritative_snapshot_and_history(self):
        path = self.root / "notes.jsonl"
        path.write_text(json.dumps({"id": "n1", "title": "Plan", "body_text": "lavender"}) + "\n")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["sync", "--db", str(self.db),
                                   "--authoritative-notes-jsonl", str(path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["added"], 1)
        path.write_text("")
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as blocked:
            main(["sync", "--db", str(self.db), "--authoritative-notes-jsonl", str(path)])
        self.assertEqual(blocked.exception.code, 2)
        self.assertEqual(next(row for row in search(self.db, "lavender"))["status"], "active")
        token = stale_confirmation_token(Snapshot("apple_notes", str(path.resolve()), (), True), ["n1"])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["sync", "--db", str(self.db),
                                   "--authoritative-notes-jsonl", str(path),
                                   "--confirm-stale", token]), 0)
        self.assertEqual(json.loads(output.getvalue())["stale_marked"], 1)
        self.assertEqual(next(row for row in search(self.db, "lavender"))["status"], "stale")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["snapshots", "--db", str(self.db)]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["missing"], 1)

    def test_incomplete_authoritative_import_cannot_silently_stale_records(self):
        first = Document("apple_notes", "n1", "First", "original", "notes.jsonl")
        second = Document("apple_notes", "n2", "Second", "preserved", "notes.jsonl")
        sync_snapshots(self.db, [Snapshot("apple_notes", "notes.jsonl", (first, second), True)])
        before_runs = len(recent_snapshots(self.db))
        altered = Document("apple_notes", "n1", "First", "partial content", "notes.jsonl")
        incomplete = Snapshot("apple_notes", "notes.jsonl", (altered,), True)
        with self.assertRaisesRegex(ValueError, "verify completeness"):
            sync_snapshots(self.db, [incomplete])
        self.assertEqual(len(recent_snapshots(self.db)), before_runs)
        self.assertEqual(next(row for row in search(self.db, "preserved"))["status"], "active")
        self.assertEqual(len(search(self.db, "original")), 1)
        self.assertEqual(search(self.db, "partial"), [])
        empty = Snapshot("apple_notes", "notes.jsonl", (), True)
        with self.assertRaisesRegex(ValueError, "verify completeness"):
            sync_snapshots(self.db, [empty])
        self.assertEqual(len(recent_snapshots(self.db)), before_runs)

    def test_stale_confirmation_is_bound_to_exact_snapshot(self):
        first = Document("markdown", "m1", "First", "one", "one.md")
        second = Document("markdown", "m2", "Second", "two", "two.md")
        sync_snapshots(self.db, [Snapshot("markdown", "docs", (first, second), True)])
        token = stale_confirmation_token(Snapshot("markdown", "docs", (first,), True), ["m2"])
        changed = Document("markdown", "m1", "First", "different", "one.md")
        with self.assertRaisesRegex(ValueError, "verify completeness"):
            sync_snapshots(self.db, [Snapshot("markdown", "docs", (changed,), True, token)])
        self.assertEqual(next(row for row in search(self.db, "two"))["status"], "active")

    def test_failed_authoritative_parse_preserves_existing_status(self):
        path = self.root / "notes.jsonl"
        path.write_text(json.dumps({"id": "n1", "title": "First", "body_text": "safe"}) + "\n")
        with redirect_stdout(StringIO()):
            main(["sync", "--db", str(self.db), "--authoritative-notes-jsonl", str(path)])
        before_runs = len(recent_snapshots(self.db))
        path.write_text('{"id":"n1","body_text":"safe"}\n{"id":')
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as failed:
            main(["sync", "--db", str(self.db), "--authoritative-notes-jsonl", str(path)])
        self.assertEqual(failed.exception.code, 2)
        self.assertEqual(len(recent_snapshots(self.db)), before_runs)
        self.assertEqual(next(row for row in search(self.db, "safe"))["status"], "active")


if __name__ == "__main__":
    unittest.main()
