"""The watcher against a real binding and a stand-in T3 projection database."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import flow
import harness
import health
import watch

PROJECT, ORCH = "app", "orch"
START = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.object(flow, "T3_CACHES", root / "no-t3")  # Never this computer's T3.
        patcher.start()
        self.addCleanup(patcher.stop)
        (root / "app").mkdir()
        self.db = root / "state" / "harness.sqlite3"
        self.op("init", workspace=str(root / "app"), repo="owner/app", requiredChecks=["acceptance"], noChecksReason="Test")
        self.op("control", action="activate", authorization="Go.")
        self.op("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        (call,) = self.op("next")["calls"]
        self.worker = call["worker"]
        self.op("done", results=[{"call": call["call"], "taskId": "task-1", "childThreadId": "child"}])
        self.native_path = root / "native.sqlite"
        with closing(sqlite3.connect(self.native_path)) as db, db:
            db.executescript("""
                CREATE TABLE orchestration_v2_projection_runs (run_id TEXT, thread_id TEXT, status TEXT, requested_at TEXT);
                CREATE TABLE orchestration_v2_projection_messages (thread_id TEXT, updated_at TEXT);
                CREATE TABLE orchestration_v2_projection_turn_items (thread_id TEXT, updated_at TEXT);""")
            db.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('run-1', 'child', 'running', ?)", (at(0),))
        self.moved(1)
        self.sent = []

    def op(self, op, **fields):
        result = harness.run(["--db", str(self.db)], json.dumps({"op": op, "projectId": PROJECT, "threadId": ORCH, **fields}),
                             hub=lambda repo: None, jev=lambda config, context, role: {})
        self.assertTrue(result["ok"], result)
        return result

    def moved(self, minutes):
        """The child thread did something (a tool call, say) at START + minutes."""
        with closing(sqlite3.connect(self.native_path)) as db, db:
            db.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES ('child', ?)", (at(minutes),))

    def scan(self, minutes, send=None):
        engine = flow.Flow(self.db)
        native = sqlite3.connect(self.native_path)
        try:
            return watch.scan(engine, native, send or (lambda thread, text: self.sent.append((thread, text))),
                              now=START + timedelta(minutes=minutes))
        finally:
            native.close()
            engine.close()

    def test_a_worker_quiet_past_its_minutes_is_named_to_the_orchestrator_once_per_quiet_spell(self):
        self.assertEqual(self.scan(20), [])
        self.assertEqual(self.scan(40), [self.worker])
        ((thread, text),) = self.sent
        self.assertEqual(thread, ORCH)
        self.assertIn("use t3 workflow", text)
        self.assertIn(self.worker, text)
        tell = self.op("next")["tell"]
        self.assertTrue(any(self.worker in line and "restart Fix the login typo" in line for line in tell), tell)
        self.assertEqual(self.scan(50), [], "one message per quiet spell")
        self.moved(55)
        self.assertEqual(self.scan(90), [self.worker], "moving again starts a new spell")

    def test_a_worker_whose_run_ended_is_left_to_its_wake(self):
        with closing(sqlite3.connect(self.native_path)) as db, db:
            db.execute("UPDATE orchestration_v2_projection_runs SET status='completed'")
        self.assertEqual(self.scan(120), [])

    def test_a_failed_send_is_tried_again_on_each_pass_and_keeps_failing_into_a_problem(self):
        down = MagicMock(side_effect=OSError("T3 refused"))
        for minutes in (40, 41, 42):
            self.assertEqual(self.scan(minutes, send=down), [])
        (problem,) = health.problems(self.db.parent)
        self.assertEqual((problem["part"], problem["lastError"]), ("send", "OSError: T3 refused"))
        self.assertEqual(self.scan(43), [self.worker])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(health.problems(self.db.parent), [])

    def watch_once(self, native=None):
        with patch.object(watch, "DEFAULT_DB", self.db), patch.object(watch, "NATIVE_DB", native or self.native_path), \
                patch.object(watch, "LOG", self.db.parent / "watch.log"), patch.object(watch, "t3_running", return_value=True), \
                patch.object(watch, "t3_send", MagicMock()), patch.object(sys, "argv", ["watch.py", "--once"]):
            self.assertEqual(watch.main(), 0)

    def test_each_pass_keeps_every_projects_own_watcher_alive(self):
        root = Path(self.tmp.name) / "state" / "homes"
        for name in ("app-1", "shop-2"):
            (root / name / "state").mkdir(parents=True)
            (root / name / "home.json").write_text(json.dumps({"project": name, "workspace": str(root / name)}))
        ensure = MagicMock(return_value=False)
        with patch.object(watch, "HOMES", root), patch.object(watch.watcher, "ensure", ensure):
            self.watch_once()
        self.assertEqual([c.args for c in ensure.call_args_list],
                         [(root / n / "state", root / n / "scripts" / "watch.py") for n in ("app-1", "shop-2")])

    def test_a_watcher_whose_code_changed_ends_so_the_new_code_starts(self):
        with patch.object(watch, "code_version", side_effect=["before", "after"]), patch.object(watch.time, "sleep") as sleep, \
                patch.object(watch, "DEFAULT_DB", self.db), patch.object(watch, "NATIVE_DB", self.native_path), \
                patch.object(watch, "LOG", self.db.parent / "watch.log"), patch.object(watch, "t3_running", return_value=True), \
                patch.object(watch, "t3_send", MagicMock()), patch.object(sys, "argv", ["watch.py"]):
            self.assertEqual(watch.main(), 0)
        sleep.assert_not_called()
        self.assertIn("code changed", (self.db.parent / "watch.log").read_text())

    def test_a_pass_that_keeps_failing_is_a_problem_and_a_good_pass_clears_it(self):
        broken = Path(self.tmp.name) / "not-a-database.sqlite"
        broken.write_text("not sqlite")
        for _ in range(3):
            self.watch_once(broken)
        (problem,) = health.problems(self.db.parent)
        self.assertEqual((problem["part"], problem["failures"]), ("scan", 3))
        self.watch_once()
        self.assertEqual(health.problems(self.db.parent), [])


if __name__ == "__main__":
    unittest.main()
