"""The record of how each model did: counts by model, role and size, shared by every bound project on a computer."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import places
import record


class RecordTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.file = self.root / "shared" / "state" / "record.sqlite3"

    def test_a_prs_size_comes_from_its_changed_files_and_lines(self):
        self.assertEqual(record.size(3, 100), "small")
        self.assertEqual(record.size(4, 10), "medium")
        self.assertEqual(record.size(1, 101), "medium")
        self.assertEqual(record.size(12, 600), "medium")
        self.assertEqual(record.size(13, 5), "large")
        self.assertEqual(record.size(2, 601), "large")
        self.assertEqual(record.size(None, 10), "unknown")
        self.assertEqual(record.size(2, None), "unknown")

    def test_counts_add_up_by_model_role_and_size_and_read_as_one_line_per_model(self):
        record.add(self.file, [("made-up/a", "implementer", "small", {"tasks": 1, "merged": 1, "first_approved": 1}),
                               ("made-up/a", "implementer", "small", {"tasks": 1, "merged": 1, "rounds": 2, "blocked": 1}),
                               ("made-up/a", "implementer", "large", {"quiet": 1}),
                               ("made-up/a", "reviewer", "small", {"tasks": 1, "rounds": 1, "dropped": 1}),
                               ("made-up/b", "implementer", "medium", {})])
        self.assertEqual(record.lines(self.file, "implementer"), {"made-up/a": (
            "small: 2 tasks, 1 approved first time, 1.0 review rounds on average, 1 blocked; "
            "large: 1 replaced for being quiet")})
        self.assertEqual(record.lines(self.file, "reviewer"),
                         {"made-up/a": "small: 1 task, 1.0 change requests on average, 1 dropped"})
        self.assertEqual(record.lines(self.file, "devops"), {})

    def test_it_keeps_counts_alone(self):
        record.add(self.file, [("made-up/a", "implementer", "small", {"tasks": 1})])
        with closing(sqlite3.connect(self.file)) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(record)")]
        self.assertEqual(columns, ["model", "role", "size", *record.COUNTS])

    def test_without_a_record_every_model_has_none_yet(self):
        self.assertEqual(record.lines(self.file, "implementer"), {})
        self.assertFalse(self.file.exists(), "reading makes no file")

    def test_several_writers_at_once_lose_nothing(self):
        def write():
            for _ in range(20):
                record.add(self.file, [("made-up/a", "implementer", "small", {"tasks": 1, "rounds": 1})])
        record.add(self.file, [("made-up/a", "implementer", "small", {"tasks": 1})])
        threads = [threading.Thread(target=write) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(record.counts(self.file, "implementer")["made-up/a"]["small"]["tasks"], 101)

    def test_the_record_lives_in_the_shared_folder_and_never_inside_a_repository(self):
        shared = self.root / "shared"
        shared.mkdir()
        self.assertEqual(places.record_home(shared, home=self.root / "user"), shared)
        (shared / ".git").mkdir()  # A clone that keeps its own state/.
        self.assertEqual(places.record_home(shared, home=self.root / "user"), self.root / "user" / ".t3-workflow")
        self.assertEqual(places.RECORD.name, "record.sqlite3")
        self.assertFalse((places.RECORD.parent.parent / ".git").exists())


if __name__ == "__main__":
    unittest.main()
