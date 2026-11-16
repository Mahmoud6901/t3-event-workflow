"""The harness database (seam 5): schema versions, WAL, the busy timeout, and v5 moving old bindings aside."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import store

OLD_BINDING = {"id": "5f0e7a21", "config": {"schemaVersion": 1, "projectId": "5f0e7a21", "workspace": "E:/work/app",
                                             "roles": {"orchestrator": "t-orch", "decision_maker": "t-dm"}, "tracks": {"max": 2}},
               "state": {"version": 41, "control": "active", "runs": {"A": {"id": "r1", "status": "done"}}, "events": {}}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state" / "harness.sqlite3"

    def facts(self):
        db = sqlite3.connect(self.path)
        try:
            return (db.execute("PRAGMA user_version").fetchone()[0], db.execute("PRAGMA journal_mode").fetchone()[0],
                    {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")})
        finally:
            db.close()

    def v4_database(self):
        """A database as the v4 code left it, holding one old binding and its audit."""
        self.path.parent.mkdir(parents=True)
        db = sqlite3.connect(self.path)
        for target in range(1, 5):
            if target in store.BEFORE:
                db.execute(store.BEFORE[target])
            store.MIGRATIONS[target](db)
            db.execute(f"PRAGMA user_version={target}")
            db.commit()
        db.execute("INSERT INTO projects VALUES (?,?,?)",
                   (OLD_BINDING["id"], store.packed(OLD_BINDING["config"]), store.packed(OLD_BINDING["state"])))
        db.execute("INSERT INTO audit(project,operation,version,detail) VALUES (?,?,?,?)", (OLD_BINDING["id"], "retry", 41, "{}"))
        db.commit()
        db.close()

    def test_a_fresh_database_lands_on_the_latest_version(self):
        store.connect(self.path).close()
        version, journal, names = self.facts()
        self.assertEqual((version, journal), (store.SCHEMA_VERSION, "wal"))
        self.assertLessEqual({"projects", "retired_bindings", "audit", "audit_created_at"}, names)
        store.connect(self.path).close()  # Opening again changes nothing.
        self.assertEqual(self.facts()[:2], (store.SCHEMA_VERSION, "wal"))

    def test_v5_moves_every_old_binding_aside_intact_and_keeps_the_history(self):
        self.v4_database()
        db = store.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM projects").fetchone()[0], 0)
            (pid, config, state, retired_at), = db.execute("SELECT id, config, state, retired_at FROM retired_bindings")
            self.assertEqual((pid, json.loads(config), json.loads(state)), (OLD_BINDING["id"], OLD_BINDING["config"], OLD_BINDING["state"]))
            self.assertRegex(retired_at, r"^\d{4}-\d\d-\d\dT")
            self.assertEqual(db.execute("SELECT operation FROM audit").fetchall(), [("retry",)])
        finally:
            db.close()
        version, _, names = self.facts()
        self.assertEqual(version, 5)
        self.assertLessEqual({"events", "deliveries", "hook_receipts", "model_limits", "runs", "usage"}, names)

    def test_a_database_newer_than_the_code_refuses_to_open(self):
        store.connect(self.path).close()
        db = sqlite3.connect(self.path)
        db.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
        db.commit()
        db.close()
        for readonly in (False, True):
            with self.assertRaisesRegex(ValueError, "This harness is older than its database; update the install."):
                store.connect(self.path, readonly=readonly)

    def test_two_writers_in_parallel_wait_for_each_other(self):
        # Live, 2026-11-02 09:04: the watcher's scan failed with "database is locked" (5 s default, rollback journal).
        store.connect(self.path).close()
        held, errors = threading.Event(), []

        def long_writer():
            db = store.connect(self.path)
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO audit(project,operation,version,detail) VALUES ('p','slow',1,'{}')")
                held.set()
                time.sleep(8)  # Well past the 5 s Python sqlite3 waits by default, within the 10 s it waits now.
                db.commit()
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)
            finally:
                db.close()

        writer = threading.Thread(target=long_writer)
        writer.start()
        held.wait(10)
        reader = store.connect(self.path, readonly=True)  # A reader is not blocked by the open write.
        try:
            self.assertEqual(reader.execute("SELECT count(*) FROM projects").fetchone()[0], 0)
        finally:
            reader.close()
        db = store.connect(self.path)  # A second writer waits instead of failing.
        try:
            with db:
                db.execute("INSERT INTO audit(project,operation,version,detail) VALUES ('p','scan',1,'{}')")
        finally:
            db.close()
        writer.join()
        self.assertEqual(errors, [])
        db = store.connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE operation IN ('slow','scan')").fetchone()[0], 2)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
