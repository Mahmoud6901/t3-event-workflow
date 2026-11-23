"""Each background part reports its own health; a problem shows only after three failures in a row."""
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sys
import tempfile
import unittest
import json
from unittest.mock import MagicMock, patch

import flow
import harness
import health
import t3_hooks

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class HealthTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)

    def test_a_problem_shows_only_after_three_failures_in_a_row_and_its_next_success_clears_it(self):
        health.failed(self.state, "scan", "database is locked", NOW)
        health.failed(self.state, "scan", "database is locked", NOW + timedelta(seconds=30))
        self.assertEqual(health.problems(self.state), [])
        health.failed(self.state, "scan", "disk full", NOW + timedelta(seconds=60))
        self.assertEqual(health.problems(self.state), [{"part": "scan", "failures": 3, "since": NOW.isoformat(), "lastError": "disk full"}])
        health.ok(self.state, "scan")
        self.assertEqual(health.problems(self.state), [])
        health.failed(self.state, "scan", "again", NOW)  # The count starts over after a success.
        self.assertEqual(health.problems(self.state), [])

    def test_one_parts_failures_do_not_count_for_another(self):
        for _ in range(2):
            health.failed(self.state, "scan", "x", NOW)
        health.failed(self.state, "send", "x", NOW)
        self.assertEqual(health.problems(self.state), [])

    def test_a_watcher_that_stopped_scanning_is_a_problem_even_with_no_failure(self):
        health.ok(self.state, "scan", NOW, beat=True)
        later = NOW + timedelta(seconds=health.SCAN_QUIET_SECONDS - 1)
        self.assertEqual(health.problems(self.state, scanning=True, now=later), [])
        much_later = NOW + timedelta(seconds=health.SCAN_QUIET_SECONDS + 1)
        self.assertEqual(health.problems(self.state, scanning=True, now=much_later)[0]["part"], "scan")
        self.assertEqual(health.problems(self.state, now=much_later), [])  # T3 closed: the watcher rightly does not scan.

    def test_a_watcher_with_no_scan_on_record_is_not_called_quiet(self):
        self.assertEqual(health.problems(self.state, scanning=True, now=NOW), [])

    def test_a_success_writes_nothing_when_nothing_failed(self):
        health.ok(self.state, "hooks")
        self.assertFalse((self.state / "health").exists())

    def test_a_damaged_record_is_ignored_and_reporting_never_raises(self):
        (self.state / "health").mkdir()
        (self.state / "health" / "scan.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(health.problems(self.state), [])
        health.failed(self.state, "scan", "x", NOW)  # Starts a fresh count.
        health.failed(self.state / "health" / "scan.json" / "not-a-folder", "scan", "x", NOW)

    def test_a_long_error_is_cut(self):
        for _ in range(3):
            health.failed(self.state, "scan", "e" * 5000, NOW)
        self.assertEqual(len(health.problems(self.state)[0]["lastError"]), health.ERROR_CHARS)


class PartsReportTests(unittest.TestCase):
    """Every background part writes its own record, and its next success clears it. The watcher's scan and send are
    covered in test_watch."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)
        patcher = patch.object(flow, "T3_CACHES", self.state / "no-t3")  # Never this computer's T3.
        patcher.start()
        self.addCleanup(patcher.stop)

    def hook_call(self, handle, inside=True):
        stdin = MagicMock(buffer=io.BytesIO(b"{}"))
        with patch.object(t3_hooks, "HARNESS_DB", self.state / "x.sqlite3"), patch.object(t3_hooks, "handle", handle), patch.object(t3_hooks, "HOMES", None),                 patch.object(t3_hooks, "inside_workspace", return_value=inside), patch.object(t3_hooks, "bindings", return_value=[]),                 patch.object(t3_hooks, "TRACE", []), patch.object(sys, "argv", ["t3_hooks", "claude", "pre_tool_use"]),                 patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", io.StringIO()):
            return t3_hooks.main()

    def test_a_crashing_hook_in_a_bound_workspace_is_a_problem_and_its_next_good_call_clears_it(self):
        broken = MagicMock(side_effect=KeyError("native"))
        for _ in range(3):
            self.hook_call(broken)
        self.assertEqual([p["part"] for p in health.problems(self.state)], ["hooks"])
        self.hook_call(MagicMock(return_value=None))
        self.assertEqual(health.problems(self.state), [])

    def test_a_hook_error_outside_a_bound_workspace_is_nobodys_problem(self):
        for _ in range(3):
            self.hook_call(MagicMock(side_effect=KeyError("native")), inside=False)
        self.assertEqual(health.problems(self.state), [])

    def op(self, jev, op, **fields):
        (self.state / "app").mkdir(exist_ok=True)
        result = harness.run(["--db", str(self.state / "harness.sqlite3")],
                             json.dumps({"op": op, "projectId": "app", "threadId": "orch", **fields}),
                             hub=lambda repo: None, jev=jev)
        self.assertTrue(result["ok"], result)
        return result

    def test_jev_failing_to_answer_is_a_problem_and_its_next_answer_clears_it(self):
        # The implementer falls back to the default when Jev is down, so nobody would otherwise learn of it.
        def down(config, context, role):
            raise RuntimeError("Jev did not decide: http_500_provider_unavailable")

        self.op(down, "init", workspace=str(self.state / "app"), repo="owner/app", requiredChecks=["acceptance"], noChecksReason="Test")
        self.op(down, "control", action="activate", authorization="Go.")
        self.op(down, "queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": f"Task {n}"} for n in range(3)]}],
                noSpec="No spec.")
        self.assertEqual(len(self.op(down, "next")["calls"]), 3, "the defaults stand in")
        (problem,) = health.problems(self.state)
        self.assertEqual((problem["part"], problem["failures"]), ("jev", 3))
        self.assertIn("http_500_provider_unavailable", problem["lastError"])
        self.op(down, "queue_edit", plan="M30", tasks=[{"task": "Task 3"}])
        self.op(lambda config, context, role: {}, "next")
        self.assertEqual(health.problems(self.state), [])


if __name__ == "__main__":
    unittest.main()
