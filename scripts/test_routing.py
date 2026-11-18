"""The shared hook in front of projects with their own copy: it fences every T3 session of a project in every turn,
and its orchestrator is served by the project's own copy."""
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import flow
import harness
import homes
import t3_hooks

CODE = Path(__file__).resolve().parent.parent
SESSIONS = {"orch": "sess-orch", "child": "sess-child", "orch-shop": "sess-shop"}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()  # The long name: Windows' temporary folder can be a short one.
        for name, value in (("T3CODE_HOME", str(root / "t3")), ("T3_WORKFLOW_NO_WATCHER", "1"), ("TYPESAFE_API_KEY", "")):
            patcher = patch.dict(os.environ, {name: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(flow, "T3_CACHES", root / "t3" / "caches")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.homes = root / "shared" / "state" / "homes"
        self.workspace, self.shop = root / "work" / "app", root / "work" / "shop"
        self.home = self.bind("app", "orch", self.workspace)
        self.bind("shop", "orch-shop", self.shop)
        self.native = root / "t3" / "userdata" / "statev2.sqlite"
        self.native.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.native)) as db, db:
            db.executescript("""
                CREATE TABLE orchestration_v2_projection_provider_threads (thread_id TEXT, payload_json TEXT);
                CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT, payload_json TEXT);
                CREATE TABLE orchestration_v2_projection_runs (run_id TEXT, thread_id TEXT, ordinal INT, status TEXT, payload_json TEXT);
                CREATE TABLE orchestration_v2_projection_messages (message_id TEXT, thread_id TEXT, run_id TEXT, role TEXT, created_at TEXT, payload_json TEXT);""")
            for thread, session in SESSIONS.items():
                db.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?)",
                           (thread, json.dumps({"nativeThreadRef": {"nativeId": session}})))
        for name, value in (("HOMES", self.homes), ("NATIVE_DB", self.native),
                            ("HARNESS_DB", root / "shared" / "state" / "harness.sqlite3")):
            patcher = patch.object(t3_hooks, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def bind(self, project, thread, workspace):
        (workspace / ".git").mkdir(parents=True)
        home = homes.create(project, workspace, self.homes, CODE, self.homes.parent.parent)
        for op, fields in (("init", {"workspace": str(workspace), "repo": f"owner/{project}", "requiredChecks": ["acceptance"],
                                     "noChecksReason": "Test"}),
                           ("control", {"action": "activate", "authorization": "Go."})):
            result = harness.run(["--db", str(homes.database(home))], json.dumps({"op": op, "projectId": project, "threadId": thread,
                                                                                  **fields}),
                                 hub=lambda repo: None, jev=lambda config, context: {})
            self.assertTrue(result["ok"], result)
        return home

    def say(self, text, thread="orch"):
        with closing(sqlite3.connect(self.native)) as db, db:
            db.execute("INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?)", (f"run-{thread}", thread, 1, "running", "{}"))
            db.execute("INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?)", (
                f"m-{thread}", thread, f"run-{thread}", "user", "2026-11-17T00:00:00Z",
                json.dumps({"id": f"m-{thread}", "role": "user", "createdBy": "user", "creationSource": "web", "text": text})))

    def call(self, tool, thread="orch", cwd=None, hook="pre_tool_use", **tool_input):
        return t3_hooks.route("claude", hook, {"cwd": str(cwd or self.workspace), "session_id": SESSIONS.get(thread, thread),
                                               "tool_name": tool, "tool_input": tool_input})

    def reason(self, verdict):
        output = (verdict or {}).get("hookSpecificOutput") or {}
        return output.get("permissionDecisionReason", "") if output.get("permissionDecision") == "deny" else ""

    def test_no_session_of_a_project_touches_another_project_or_the_workflow_in_any_turn(self):
        self.say("What does the login page do?")  # An ordinary turn: the orchestrator guard is off, the fence is not.
        for thread in ("orch", "child"):
            self.assertIn("T3_WORKFLOW_FENCE", self.reason(self.call("Write", thread, file_path=str(self.shop / "a.ts"))))
            self.assertIn("T3_WORKFLOW_FENCE", self.reason(self.call("Read", thread, file_path=str(self.shop / "a.ts"))))
            self.assertIn("T3_WORKFLOW_FENCE", self.reason(self.call("Edit", thread, file_path=str(CODE / "scripts" / "flow.py"))))
            self.assertIn("T3_WORKFLOW_FENCE", self.reason(self.call("Read", thread, file_path=str(homes.database(self.home)))))
        self.assertIsNone(self.call("Edit", "orch", file_path=str(self.workspace / "src" / "a.ts")))
        self.assertIsNone(self.call("Edit", "child", file_path=str(self.workspace / "src" / "a.ts")))
        self.assertIsNone(self.call("Write", "not-a-t3-session", file_path=str(self.shop / "a.ts")))  # Not T3's.

    def test_the_orchestrator_runs_its_projects_own_copy_of_the_workflow(self):
        self.say("use t3 workflow: status")
        verdict = self.call("Bash", command="t3-workflow '{\"op\":\"status\"}'")
        command = verdict["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn(flow.slash(self.home / "scripts" / "harness.py"), command)
        self.assertIn(flow.slash(homes.database(self.home)), command)
        self.assertIn("T3_WORKFLOW_ORCHESTRATOR", self.reason(self.call("Edit", file_path=str(self.workspace / "a.ts"))))
        context = self.call(None, hook="user_prompt_submit")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(str(self.home / "references" / "orchestrator.md"), context)

    def test_an_operation_of_any_size_runs_from_a_short_command_once(self):
        # Claude Code runs a command inside a script of capped size, and a session's saved environment grows that
        # script each time it resumes: the command names the operation instead of carrying it.
        self.say("use t3 workflow: plan it")
        brief = "Keep the login form as it is. " * 300
        op = {"op": "queue_set", "plans": [{"plan": "M1", "owns": ["src/"], "tasks": [{"task": "Big brief", "brief": brief}]}]}
        verdict = self.call("Bash", command=f"t3-workflow '{json.dumps(op)}'")
        command = verdict["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertLess(len(command), 400)
        argv = shlex.split(command)
        self.assertEqual(argv[2], flow.slash(self.home / "scripts" / "harness.py"))
        self.assertTrue(harness.run(argv[3:])["ok"])
        status = harness.run(["--db", str(homes.database(self.home))], json.dumps(
            {"op": "status", "projectId": "app", "threadId": "orch"}))
        self.assertIn("Big brief", json.dumps(status))
        self.assertFalse(harness.run(argv[3:])["ok"], "a command runs its operation once")

    def test_a_crash_inside_a_moved_project_fails_closed(self):
        self.say("use t3 workflow: status")
        out = []
        data = {"cwd": str(self.workspace), "session_id": "sess-orch", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        with patch.object(t3_hooks, "delegate", side_effect=RuntimeError("boom")), \
                patch.object(t3_hooks.sys, "stdin") as stdin, patch.object(t3_hooks.sys.stdout, "write", out.append):
            stdin.buffer.read.return_value = json.dumps(data).encode()
            t3_hooks.main(["claude", "pre_tool_use"])
        self.assertEqual(json.loads("".join(out))["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
