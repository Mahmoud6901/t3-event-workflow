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
                CREATE TABLE orchestration_v2_projection_messages (thread_id TEXT, role TEXT, created_at TEXT, updated_at TEXT,
                                                                   payload_json TEXT);
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
        self.op("control", action="pause")  # Paused, nobody is replaced: the quiet worker is only told.
        self.assertEqual(self.scan(20), [])
        self.assertEqual(self.scan(40), [self.worker])
        ((thread, text),) = self.sent
        self.assertEqual(thread, ORCH)
        self.assertIn("use t3 workflow", text)
        self.assertIn(self.worker, text)
        step = self.op("next")
        self.assertEqual(step["calls"], [])
        self.assertTrue(any(self.worker in line and "restart Fix the login typo" in line for line in step["tell"]),
                        step["tell"])
        self.assertEqual(self.scan(50), [], "one message per quiet spell")
        self.moved(55)
        self.assertEqual(self.scan(90), [self.worker], "moving again starts a new spell")

    def test_an_active_bindings_quiet_worker_wakes_the_orchestrator_whose_next_replaces_it(self):
        self.assertEqual(self.scan(40), [self.worker])
        self.assertEqual(len(self.sent), 1)
        step = self.op("next")
        self.assertEqual([(c["tool"], c["worker"]) for c in step["calls"]][0], ("task_cancel", self.worker))
        (fresh,) = [c for c in step["calls"] if c["tool"] == "delegate_task"]
        self.assertNotEqual(fresh["worker"], self.worker)
        self.assertIn(f"{self.worker}, the implementer of Fix the login typo (M30) was quiet since {at(1)}, so it was "
                      f"stopped and a new implementer took over.", step["tell"])
        self.assertEqual(self.scan(90), [], "the stopped worker is not watched")

    def test_a_worker_whose_run_ended_is_left_to_its_wake(self):
        with closing(sqlite3.connect(self.native_path)) as db, db:
            db.execute("UPDATE orchestration_v2_projection_runs SET status='completed'")
        self.assertEqual(self.scan(120), [])

    def ended(self, minutes, words="Delegated task task-1 (M30: Fix the login typo) in child thread child [completed]:"):
        """The child's run ended, and T3 left its notice on the orchestrator's thread at START + minutes."""
        with closing(sqlite3.connect(self.native_path)) as db, db:
            db.execute("UPDATE orchestration_v2_projection_runs SET status='completed'")
            db.execute("INSERT INTO orchestration_v2_projection_messages VALUES (?, 'user', ?, ?, ?)", (ORCH, at(minutes), at(minutes), json.dumps({
                "role": "user", "createdBy": "agent", "creationSource": "server", "text": f"{words}\nDone.",
                "delegatedCompletion": {"parentRunId": "run-orch-1", "generation": 1, "taskIds": ["task-1"]}})))

    def repair(self, minutes):
        """One pass's repair and its wake: (the workers recorded, the projects woken)."""
        engine = flow.Flow(self.db)
        native = sqlite3.connect(self.native_path)
        try:
            recorded = watch.repair(engine, native, now=START + timedelta(minutes=minutes))
            return recorded, watch.lates(engine, lambda thread, text: self.sent.append((thread, text)))
        finally:
            native.close()
            engine.close()

    def test_a_workers_end_the_hook_missed_is_recorded_after_a_while_and_the_orchestrator_woken_once(self):
        self.ended(30)
        self.assertEqual(self.repair(35), ([], []), "the orchestrator's own turn reads a fresh notice")
        self.assertEqual(self.repair(41), ([self.worker], [PROJECT]))
        ((thread, text),) = self.sent
        self.assertEqual(thread, ORCH)
        self.assertIn("use t3 workflow", text)
        self.assertIn('{"op":"next"}', text)
        self.assertEqual(self.repair(45), ([], []), "recorded once, woken once")
        (nudge,) = self.op("next")["calls"]
        self.assertEqual(nudge["tool"], "t3_thread_send", "next acts on the end: the implementer has not reported")

    def test_a_late_end_next_already_acted_on_wakes_nobody(self):
        self.ended(30)
        engine = flow.Flow(self.db)
        native = sqlite3.connect(self.native_path)
        try:
            self.assertEqual(watch.repair(engine, native, now=START + timedelta(minutes=41)), [self.worker])
        finally:
            native.close()
            engine.close()
        self.op("next")
        self.assertEqual(self.repair(42), ([], []))
        self.assertEqual(self.sent, [])

    def test_an_end_the_hook_recorded_is_left_alone(self):
        self.ended(30)
        self.op("ended", taskId="task-1", status="completed")
        self.assertEqual(self.repair(41), ([], []))

    def stopped(self, minutes, words="⚠ Individual quota reached. Resets in 1h10m."):
        """The orchestrator's own turn failed at START + minutes, ending on `words` (by default its model's quota)."""
        with closing(sqlite3.connect(self.native_path)) as db, db:
            db.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('run-orch-2', ?, 'failed', ?)", (ORCH, at(minutes)))
            db.execute("INSERT INTO orchestration_v2_projection_messages VALUES (?, 'assistant', ?, ?, ?)",
                       (ORCH, at(minutes), at(minutes), json.dumps({"role": "assistant", "text": words})))

    def stalls(self, minutes):
        """One pass's check of the orchestrator's own stopped turn: the projects woken."""
        engine = flow.Flow(self.db)
        native = sqlite3.connect(self.native_path)
        try:
            return watch.stalls(engine, native, lambda thread, text: self.sent.append((thread, text)),
                                now=START + timedelta(minutes=minutes))
        finally:
            native.close()
            engine.close()

    def test_an_orchestrator_stopped_at_its_own_limit_is_woken_once_it_resets_when_work_waits(self):
        self.op("ended", taskId="task-1", status="completed")  # The hook recorded it; the orchestrator's turn then failed.
        self.stopped(30)
        self.assertEqual(self.stalls(60), [], "its limit has not reset yet")
        self.assertEqual(self.stalls(101), [PROJECT])
        ((thread, text),) = self.sent
        self.assertEqual(thread, ORCH)
        self.assertIn("use t3 workflow", text)
        self.assertIn('{"op":"next"}', text)
        self.assertEqual(self.stalls(110), [], "once per stopped turn")
        (nudge,) = self.op("next")["calls"]
        self.assertEqual(nudge["tool"], "t3_thread_send", "next acts on the end it could not act on before")

    def test_a_stopped_turn_with_no_work_waiting_or_not_stopped_at_a_limit_wakes_nobody(self):
        self.stopped(30)
        self.assertEqual(self.stalls(101), [], "nothing waits for next")
        self.op("ended", taskId="task-1", status="completed")
        with closing(sqlite3.connect(self.native_path)) as db, db:
            db.execute("DELETE FROM orchestration_v2_projection_messages WHERE role='assistant'")
        self.stopped(40, words="The provider session closed unexpectedly.")
        self.assertEqual(self.stalls(200), [], "a turn that failed for another reason is T3's to resume")
        self.assertEqual(self.sent, [])

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
