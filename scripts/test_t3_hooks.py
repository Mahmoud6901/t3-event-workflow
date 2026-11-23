"""The orchestrator's hooks against a real binding and a stand-in T3 projection database."""
import base64
from contextlib import closing
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import flow
import harness
import t3_hooks

PROJECT, ORCH = "app", "orch"
NEXT = "t3-workflow '{\"op\":\"next\"}'"


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.object(flow, "T3_CACHES", root / "no-t3")  # Never this computer's T3.
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workspace = root / "app"
        (self.workspace / ".git").mkdir(parents=True)
        self.db = root / "state" / "harness.sqlite3"
        self.assertTrue(self.op("init", workspace=str(self.workspace), repo="owner/app", requiredChecks=["acceptance"],
                                noChecksReason="Hook test")["ok"])
        self.assertTrue(self.op("control", action="activate", authorization="Go.")["ok"])
        self.native = root / "native.sqlite"
        with closing(sqlite3.connect(self.native)) as db, db:
            db.executescript("""
                CREATE TABLE orchestration_v2_projection_provider_threads (thread_id TEXT, payload_json TEXT);
                CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT, payload_json TEXT);
                CREATE TABLE orchestration_v2_projection_runs (run_id TEXT, thread_id TEXT, ordinal INT, status TEXT, payload_json TEXT);
                CREATE TABLE orchestration_v2_projection_messages (message_id TEXT, thread_id TEXT, run_id TEXT, role TEXT, created_at TEXT, payload_json TEXT);""")
            for thread in (ORCH, "child"):
                db.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?)",
                           (thread, json.dumps({"nativeThreadRef": {"nativeId": "sess-" + thread}})))
        for name, value in (("HARNESS_DB", self.db), ("NATIVE_DB", self.native), ("HOMES", root / "homes")):
            patcher = patch.object(t3_hooks, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = 0

    def op(self, op, **fields):
        return harness.run(["--db", str(self.db)], json.dumps({"op": op, "projectId": PROJECT, "threadId": ORCH, **fields}),
                           hub=lambda repo: None, jev=lambda config, context, role: {})

    def message(self, text, thread=ORCH, run=1, created_by="user", source="web", **extra):
        """A message in the thread's turn `run`; the turn is running."""
        self.sent += 1
        with closing(sqlite3.connect(self.native)) as db, db:
            if not db.execute("SELECT 1 FROM orchestration_v2_projection_runs WHERE run_id=?", (f"run-{thread}-{run}",)).fetchone():
                db.execute("UPDATE orchestration_v2_projection_runs SET status='completed' WHERE thread_id=?", (thread,))
                db.execute("INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?)",
                           (f"run-{thread}-{run}", thread, run, "running", json.dumps({"ordinal": run})))
            db.execute("INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?)", (
                f"m{self.sent}", thread, f"run-{thread}-{run}", "user", f"2026-11-10T00:00:{self.sent:02d}Z",
                json.dumps({"id": f"m{self.sent}", "role": "user", "createdBy": created_by, "creationSource": source, "text": text,
                            **extra})))

    def wake(self, task_id, run=1, status="completed", body="Done.", words=None):
        """T3's notice that the run of T3 task `task_id` ended, worded as T3 words it since 2026-11-23 unless `words`."""
        words = words or f"Delegated task {task_id} (M30: Fix the login typo) in child thread child [{status}]:"
        self.message(f"{words}\n{body}", run=run, created_by="agent", source="server",
                     delegatedCompletion={"parentRunId": f"run-{ORCH}-1", "generation": 1, "taskIds": [task_id]})
        return f"{words}\n{body}"

    def hook(self, hook, session=f"sess-{ORCH}", cwd=None, runtime="claude", **data):
        return t3_hooks.handle(runtime, hook, {"cwd": str(cwd or self.workspace), "session_id": session, **data})

    def tool(self, name, **tool_input):
        return self.hook("pre_tool_use", tool_name=name, tool_input=tool_input)

    def codex_tool(self, name, **tool_input):
        return self.hook("pre_tool_use", runtime="codex", tool_name=name, tool_input=tool_input)

    def decision(self, verdict):
        return ((verdict or {}).get("hookSpecificOutput") or {}).get("permissionDecision")

    def started(self):
        """A plan queued and its implementer's start made and recorded: T3 task task-1 in thread child."""
        self.assertTrue(self.op("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])["ok"])
        (call,) = self.op("next")["calls"]
        self.assertTrue(self.op("done", results=[{"call": call["call"], "taskId": "task-1", "childThreadId": "child"}])["ok"])

    def test_sessions_outside_t3_the_workspace_or_the_orchestrator_are_left_alone(self):
        self.message("use t3 workflow: fix the login typo")
        self.assertIsNone(self.hook("pre_tool_use", session="not-a-t3-session", tool_name="Edit", tool_input={}))
        self.assertIsNone(self.hook("pre_tool_use", cwd=Path(self.tmp.name), tool_name="Edit", tool_input={}))
        self.message("Implement it.", thread="child", created_by="agent", source="mcp")
        self.assertIsNone(self.hook("pre_tool_use", session="sess-child", tool_name="Edit", tool_input={}))

    def test_in_a_workflow_turn_the_orchestrator_only_reads_uses_t3_tools_and_runs_the_workflow(self):
        self.message("use t3 workflow: fix the login typo")
        for name in ("Edit", "Write", "Bash"):
            verdict = self.tool(name, command="git status", file_path="src/x.ts")
            self.assertEqual(self.decision(verdict), "deny", name)
            self.assertIn("T3_WORKFLOW_ORCHESTRATOR", verdict["hookSpecificOutput"]["permissionDecisionReason"])
        for name in ("Read", "Grep", "mcp__t3-code__delegate_task", "mcp__t3-code__t3_thread_send",
                     "mcp__t3-code__task_cancel", "mcp__t3-code__t3_thread_organize"):
            self.assertIsNone(self.tool(name), name)

    def test_an_ordinary_turn_is_not_guarded(self):
        self.message("What does the login page do?")
        self.assertIsNone(self.tool("Edit", file_path="src/x.ts"))

    def saved(self, command):
        """The operation the hook saved for the token its command names."""
        token = re.search(r"--request ([0-9a-f]{32})$", command).group(1)
        return json.loads((self.db.parent / harness.REQUESTS / f"{token}.json").read_text(encoding="utf-8"))

    def test_the_workflow_command_runs_the_harness_as_this_orchestrator(self):
        self.message("use t3 workflow: status")
        verdict = self.tool("Bash", command="t3-workflow '{\"op\":\"status\",\"projectId\":\"other\",\"threadId\":\"forged\"}'")
        self.assertEqual(self.decision(verdict), "allow")
        command = verdict["hookSpecificOutput"]["updatedInput"]["command"]
        sent = self.saved(command)
        self.assertEqual((sent["op"], sent["projectId"], sent["threadId"]), ("status", PROJECT, ORCH))
        bash = shutil.which("bash")
        if bash:  # Claude Code's Bash tool runs Git Bash on Windows.
            done = subprocess.run([bash, "-c", command], capture_output=True, text=True, timeout=60)
            self.assertEqual(json.loads(done.stdout)["control"], "active", done.stderr)

    def test_a_malformed_workflow_command_is_denied_with_the_right_form(self):
        self.message("use t3 workflow: next")
        verdict = self.tool("Bash", command="t3-workflow next")
        self.assertEqual(self.decision(verdict), "deny")
        self.assertIn("t3-workflow '<JSON>'", verdict["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_wake_notice_records_the_workers_end_once_and_next_asks_it_to_finish(self):
        self.started()
        self.wake("task-1", run=2)
        context = self.hook("user_prompt_submit", prompt="Delegated task task-1")
        self.assertIn(NEXT, context["hookSpecificOutput"]["additionalContext"])
        (nudge,) = self.op("next")["calls"]
        self.assertEqual((nudge["tool"], nudge["arguments"]["threadId"]), ("t3_thread_send", "child"))
        self.hook("user_prompt_submit", prompt="Delegated task task-1")
        self.assertEqual([c["call"] for c in self.op("next")["calls"]], [nudge["call"]], "the same end is not news")

    def test_a_wake_notice_of_a_run_stopped_at_its_quota_starts_a_new_implementer_instead_of_asking_it_to_finish(self):
        self.started()
        self.wake("task-1", run=2, status="failed", body=(
            "Antigravity stopped this turn because of an error.\n\nLast assistant message before the run failed:\n\n\n"
            "⚠ Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 4h11m25s.\n"))
        self.hook("user_prompt_submit", prompt="Delegated task task-1")
        (fresh,) = self.op("next")["calls"]
        self.assertEqual(fresh["tool"], "delegate_task", "a follow-up would stop at the same quota")

    def test_a_notice_in_t3s_earlier_wording_is_read_too(self):
        self.started()
        self.wake("task-1", run=2, words="Delegated task task-1 (M30: Fix the login typo) [completed]:")
        self.hook("user_prompt_submit", prompt="Delegated task task-1")
        (nudge,) = self.op("next")["calls"]
        self.assertEqual(nudge["tool"], "t3_thread_send")

    def test_a_label_with_brackets_of_its_own_does_not_hide_the_runs_status(self):
        self.started()
        self.wake("task-1", run=2, status="failed", body="⚠ Individual quota reached. Resets in 2h.",
                  words="Delegated task task-1 (M30: Fix [the] login (typo)) in child thread child [failed]:")
        self.hook("user_prompt_submit", prompt="Delegated task task-1")
        (fresh,) = self.op("next")["calls"]
        self.assertEqual(fresh["tool"], "delegate_task", "read as failed at its quota")

    def test_a_notice_t3_words_differently_again_is_still_read_from_the_task_it_lists(self):
        self.started()
        prompt = self.wake("task-1", run=2, words="Your delegated work task-1 is finished.")
        self.hook("user_prompt_submit", prompt=prompt)
        (nudge,) = self.op("next")["calls"]
        self.assertEqual(nudge["tool"], "t3_thread_send")

    def test_the_turn_cannot_end_with_calls_unrecorded_or_a_workers_end_unread(self):
        self.message("use t3 workflow: fix the login typo")
        self.assertTrue(self.op("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])["ok"])
        (call,) = self.op("next")["calls"]
        held = self.hook("stop")
        self.assertEqual(held["decision"], "block")
        self.assertIn(call["call"], held["reason"])
        self.assertIsNone(self.hook("stop", stop_hook_active=True), "one reminder per turn end")
        self.assertTrue(self.op("done", results=[{"call": call["call"], "taskId": "task-1", "childThreadId": "child"}])["ok"])
        self.assertIsNone(self.hook("stop"))
        self.wake("task-1")  # T3 adds the notice to the running turn
        late = self.hook("stop")
        self.assertEqual(late["decision"], "block")
        self.assertIn(NEXT, late["reason"])
        self.assertIsNone(self.hook("stop"))

    def test_the_orchestrator_gets_its_context_only_in_a_workflow_turn(self):
        self.message("What does the login page do?")
        self.assertIsNone(self.hook("user_prompt_submit", prompt="What does the login page do?"))
        self.message("use t3 workflow: fix the login typo", run=2)
        context = self.hook("user_prompt_submit", prompt="use t3 workflow: fix the login typo")
        self.assertIn(NEXT, context["hookSpecificOutput"]["additionalContext"])

    def run_main(self, runtime, data):
        out = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(data).encode()))
        with patch.object(t3_hooks, "handle", side_effect=RuntimeError("boom")), \
                patch.object(t3_hooks.sys, "argv", ["t3_hooks.py", runtime, "pre_tool_use"]), \
                patch.object(t3_hooks.sys, "stdin", stdin), patch.object(t3_hooks.sys, "stdout", out):
            t3_hooks.main()
        return out.getvalue()

    def test_a_crash_inside_a_bound_workspace_fails_closed(self):
        out = self.run_main("claude", {"cwd": str(self.workspace), "session_id": f"sess-{ORCH}", "tool_name": "Edit"})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_crash_in_a_codex_hook_fails_closed_and_other_runtimes_are_left_alone(self):
        data = {"cwd": str(self.workspace), "session_id": f"sess-{ORCH}", "tool_name": "Bash"}
        self.assertEqual(json.loads(self.run_main("codex", data))["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.run_main("goose", data), "")

    def test_a_codex_orchestrator_runs_the_workflow_from_powershell(self):
        self.message("use t3 workflow: status")
        for key in ("command", "cmd"):  # Codex names the shell input either way.
            verdict = self.codex_tool("Bash", **{key: "t3-workflow '{\"op\":\"status\",\"threadId\":\"forged\"}'"})
            self.assertEqual(self.decision(verdict), "allow", key)
            command = verdict["hookSpecificOutput"]["updatedInput"][key]
            self.assertNotIn('"', command, "PowerShell treats a quoted first word as a string, not a command")
            self.assertIn("--ascii", command)
            sent = self.saved(command)
            self.assertEqual((sent["op"], sent["threadId"]), ("status", ORCH))
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell:  # Codex runs its shell commands in PowerShell on Windows.
            done = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
            self.assertEqual(json.loads(done.stdout)["control"], "active", done.stderr)

    def test_the_harness_answers_in_ascii_when_asked(self):
        self.assertTrue(self.op("queue_set", plans=[{"plan": "M30 — اختبار", "owns": ["src/"], "tasks": [{"task": "Fix"}]}])["ok"])
        request = base64.b64encode(json.dumps({"op": "status", "projectId": PROJECT, "threadId": ORCH}).encode()).decode()
        out = io.StringIO()
        with patch.object(harness.sys, "stdout", out):
            harness.main(["--db", str(self.db), "--ascii", "--op-base64", request])
        self.assertTrue(out.getvalue().isascii(), "PowerShell decodes a program's output with the console code page")
        self.assertIn("M30 — اختبار", json.loads(out.getvalue())["plans"])

    def test_a_codex_orchestrator_reads_with_simple_read_commands_and_runs_nothing_else(self):
        self.message("use t3 workflow: plan the login fix")
        for command in ("Get-Content -LiteralPath 'E:\\app\\references\\orchestrator.md'", "Get-ChildItem src -Recurse",
                        'rg -n "login" src', "Select-String -Path src\\*.ts -Pattern login", "cat README.md"):
            self.assertIsNone(self.codex_tool("Bash", command=command), command)
        self.assertIsNone(self.codex_tool("Bash", cmd="Get-Content README.md"))
        for command in ("Get-Content a.txt | Set-Content b.txt", "Get-Content a; Remove-Item b", "cat a > b",
                        "Get-Content a\nRemove-Item b", "Get-Content a & calc", "Get-Content $(Remove-Item b)",
                        "Get-Content (Remove-Item b)", "Remove-Item src -Recurse", "git status", "rg --pre calc.exe login",
                        "rg -nz login", "rg --hostname-bin=calc.exe --hyperlink-format=default login"):
            self.assertEqual(self.decision(self.codex_tool("Bash", command=command)), "deny", command)
        self.assertEqual(self.decision(self.codex_tool("apply_patch", command="*** Begin Patch")), "deny")
        self.assertIsNone(self.codex_tool("mcp__t3_code__delegate_task"))

    def test_a_codex_orchestrator_gets_its_context_and_its_stop_reminder(self):
        self.message("use t3 workflow: fix the login typo")
        context = self.hook("user_prompt_submit", runtime="codex", prompt="use t3 workflow: fix the login typo")
        self.assertIn(NEXT, context["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Get-Content", context["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.op("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])["ok"])
        self.op("next")
        self.assertEqual(self.hook("stop", runtime="codex")["decision"], "block")


if __name__ == "__main__":
    unittest.main()
