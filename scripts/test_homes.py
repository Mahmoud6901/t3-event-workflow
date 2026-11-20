"""Each project's own copy of the workflow: made at setup, moved into from the shared database, upgraded on request."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import flow
import harness
import homes
import places

CODE = Path(__file__).resolve().parent.parent


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()  # The long name: Windows' temporary folder can be a short one.
        self.shared = self.root / "shared"
        self.db = self.shared / "state" / "harness.sqlite3"
        self.homes = self.shared / "state" / "homes"
        self.workspace = self.root / "work" / "my app"
        (self.workspace / ".git").mkdir(parents=True)
        for name, value in (("T3CODE_HOME", str(self.root / "t3")), ("TYPESAFE_API_KEY", "")):
            patcher = patch.dict(os.environ, {name: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(flow, "T3_CACHES", self.root / "t3" / "caches")
        patcher.start()
        self.addCleanup(patcher.stop)

    def bind_shared(self, project, workspace, thread):
        """A binding the way 0.3.0 kept it: in the shared database."""
        engine = flow.Flow(self.db, hub=lambda repo: None, jev=lambda config, context, role: {})
        try:
            engine.orchestrator({"op": "init", "projectId": project, "threadId": thread, "workspace": str(workspace),
                                 "repo": "owner/app", "requiredChecks": ["acceptance"], "noChecksReason": "Test"})
            engine.orchestrator({"op": "control", "projectId": project, "threadId": thread, "action": "activate",
                                 "authorization": "Go."})
        finally:
            engine.close()

    def shared_ids(self):
        engine = flow.Flow(self.db)
        try:
            return [project for project, _, _ in engine.projects()]
        finally:
            engine.close()

    def test_a_home_is_the_projects_own_copy_of_the_code_with_its_own_data_folder(self):
        home = homes.create("5c1e7a2d-0b44", self.workspace, self.homes, CODE, self.shared)
        self.assertEqual(home.name, "my-app-5c1e7a2d")
        self.assertTrue((home / "scripts" / "harness.py").is_file())
        self.assertTrue((home / "references" / "orchestrator.md").is_file())
        self.assertFalse(list(home.glob("scripts/test_*.py")))
        self.assertEqual(places.base(home), home)  # Its data is its own state/.
        self.assertEqual(places.home_of(home)["shared"], str(self.shared))
        tree = self.root / "t3" / "worktrees" / "app" / "t1"
        tree.mkdir(parents=True)
        (self.workspace / ".git" / "worktrees" / "t1").mkdir(parents=True)
        (self.workspace / ".git" / "worktrees" / "t1" / "gitdir").write_text(str(tree / ".git"))
        for cwd in (self.workspace, self.workspace / "src", tree):
            self.assertEqual(homes.find(str(cwd), self.homes)[0], home)
        self.assertIsNone(homes.find(str(self.root / "work"), self.homes))
        with self.assertRaises(homes.Failed):
            homes.create("5c1e7a2d-0b44", self.workspace, self.homes, CODE, self.shared)

    def test_upgrade_brings_a_homes_code_to_the_installed_version_and_keeps_its_data(self):
        home = homes.create("p1", self.workspace, self.homes, CODE, self.shared)
        self.assertFalse(homes.upgrade(home, CODE))
        (home / "scripts" / "flow.py").write_text("# an older version\n")
        (home / "scripts" / "retired.py").write_text("# a file this version no longer has\n")
        (home / "state" / "harness.sqlite3").write_text("the project's data")
        self.assertTrue(homes.upgrade(home, CODE))
        self.assertEqual((home / "scripts" / "flow.py").read_bytes(), (CODE / "scripts" / "flow.py").read_bytes())
        self.assertFalse((home / "scripts" / "retired.py").exists())
        self.assertEqual((home / "state" / "harness.sqlite3").read_text(), "the project's data")
        self.assertEqual(homes.info(home)["code"], homes.fingerprint(CODE))

    def test_a_binding_moves_into_its_own_home_with_its_history_and_nothing_of_it_stays_shared(self):
        other = self.root / "work" / "shop"
        (other / ".git").mkdir(parents=True)
        self.bind_shared("app", self.workspace, "orch-app")
        self.bind_shared("shop", other, "orch-shop")
        state = self.db.parent
        (state / "harness-artifacts" / "app" / "tests").mkdir(parents=True)
        (state / "harness-artifacts" / "app" / "tests" / "log.txt").write_text("passed")
        home = homes.move(self.db, "app", self.homes, CODE, self.shared)
        self.assertEqual(self.shared_ids(), ["shop"])
        engine = flow.Flow(homes.database(home))
        try:
            self.assertEqual([p for p, _, _ in engine.projects()], ["app"])
            self.assertEqual(engine.load("app")[1]["control"], "active")
            self.assertEqual([op for (op,) in engine.db.execute("SELECT operation FROM audit WHERE project='app' ORDER BY id")],
                             ["control"])
        finally:
            engine.close()
        self.assertEqual((home / "state" / "harness-artifacts" / "app" / "tests" / "log.txt").read_text(), "passed")
        self.assertFalse((state / "harness-artifacts" / "app").exists())
        self.assertEqual(homes.find(str(other), self.homes), None)

    def test_a_move_interrupted_after_its_home_took_over_is_finished_by_the_next_one(self):
        self.bind_shared("app", self.workspace, "orch-app")
        home = homes.move(self.db, "app", self.homes, CODE, self.shared)
        self.bind_shared("app", self.workspace, "orch-app")  # The shared rows an interruption left behind.
        self.assertEqual(homes.move(self.db, "app", self.homes, CODE, self.shared), home)
        self.assertEqual(self.shared_ids(), [])

    def test_a_folder_that_is_not_a_whole_home_is_no_home(self):
        for name, text in (("half-made", None), ("no-workspace", '{"project": "x"}'), ("a-list", "[]"), ("broken", "{")):
            (self.homes / name / "state").mkdir(parents=True)
            if text is not None:
                (self.homes / name / "home.json").write_text(text)
        self.assertEqual(homes.listing(self.homes), [])
        self.assertIsNone(homes.find(str(self.workspace), self.homes))
        self.bind_shared("app", self.workspace, "orch-app")
        (self.homes / "my-app-app" / "state").mkdir(parents=True)  # A move cut short before its home was named.
        self.assertEqual(homes.move(self.db, "app", self.homes, CODE, self.shared).name, "my-app-app")

    def test_a_move_that_fails_leaves_the_binding_where_it_was(self):
        self.bind_shared("app", self.workspace, "orch-app")
        with patch.object(homes, "TABLES", homes.TABLES + (("no_such_table", "project"),)), self.assertRaises(Exception):
            homes.move(self.db, "app", self.homes, CODE, self.shared)
        self.assertEqual(self.shared_ids(), ["app"])
        self.assertEqual(homes.listing(self.homes), [])

    def test_setup_binds_a_project_in_its_own_home_and_its_workers_report_through_either_command(self):
        def op(op, **fields):
            return harness.run(["--db", str(self.db)], json.dumps({"op": op, "projectId": "app", "threadId": "orch", **fields}),
                               homes_root=self.homes)
        self.assertTrue(op("init", workspace=str(self.workspace), repo="owner/app", requiredChecks=["acceptance"],
                           noChecksReason="Test")["ok"])
        (home, data), = homes.listing(self.homes)
        self.assertEqual(data["project"], "app")
        self.assertFalse(self.db.exists() and self.shared_ids())  # Nothing of the project in the shared database.
        self.assertTrue(op("control", action="activate", authorization="Go.")["ok"])
        self.assertTrue(op("queue_set", plans=[{"plan": "M1", "owns": ["src/"], "tasks": [
            {"task": "Fix the typo", "brief": "Fix it."}]}])["ok"])
        (call,) = op("next")["calls"]
        brief = call["arguments"]["task"]
        self.assertIn(flow.slash(home / "scripts" / "harness.py"), brief)  # Its workers run the project's own copy.
        self.assertTrue(op("done", results=[{"call": call["call"], "taskId": "task-1", "childThreadId": "child"}])["ok"])
        worker, token = call["worker"], brief.split("--token ")[1].split()[0]
        shared = harness.run(["--db", str(self.db), "worker", "stuck", "--worker", worker, "--token", token, "--why",
                              "No access"], homes_root=self.homes)
        self.assertTrue(shared["ok"], shared)  # A brief written before the move names the shared command.
        wrong = harness.run(["--db", str(self.db), "worker", "stuck", "--worker", worker, "--token", "0" * 32, "--why", "x"],
                            homes_root=self.homes)
        self.assertFalse(wrong["ok"])
        self.assertIn(f"No worker {worker}", wrong["error"])

    def test_a_worker_flag_given_twice_reaches_the_projects_own_copy_twice(self):
        args = harness.parser().parse_args(["worker", "waits", "--worker", "w2", "--token", "t", "--on", "Add a footer",
                                            "--on", "Add a header", "--why", "src/footer.ts has no footer yet"])
        with patch.object(homes, "holder", return_value=self.homes / "app"), \
                patch.object(homes, "run", side_effect=lambda home, argv: {"ok": True, "argv": argv}):
            passed = harness.forward(args, None, self.homes)
        self.assertIn("--on=Add a footer", passed["argv"])
        self.assertIn("--on=Add a header", passed["argv"])


if __name__ == "__main__":
    unittest.main()
