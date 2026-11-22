"""The installer against a temporary home folder."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # noqa: E402


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / ".codex").mkdir()
        self.other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "graphify hook-check"}]}
        old = {"hooks": [{"type": "command", "command": "python E:/x/scripts/t3_hooks.py codex user_prompt_submit"}]}
        (self.home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [self.other], "UserPromptSubmit": [old]}}))
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "CLAUDE.md").write_text(f"# Mine\n\n{install.BEGIN}\nold path\n{install.END}\n\nkeep me\n")
        t3 = self.home / ".t3"
        (t3 / "userdata").mkdir(parents=True)
        (t3 / "caches").mkdir()
        (t3 / "userdata" / "settings.json").write_text(json.dumps({"providerInstances": {"acpRegistry_goose": {"driver": "acpRegistry"}}}))
        # An approved OpenRouter model T3 lists, and one it does not, which install adds to goose's custom models.
        patch.object(install.Installer, "goose_models", return_value=["vendor/listed-model", "vendor/unlisted-model"]).start()
        (t3 / "caches" / "acpRegistry_goose.json").write_text(json.dumps({"models": [{"slug": "vendor/listed-model"}]}))
        patch.object(install.Installer, "t3_running", return_value=False).start()
        patch.object(install, "STATE", self.home / "workflow" / "state").start()  # Backups land in the temp folder.
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.temp.cleanup()

    def run_installer(self, action):
        installer = install.Installer(self.home)
        installer.apply(remove=action == "uninstall")
        return installer

    def test_status_names_a_health_problem_and_exits_non_zero(self):
        import contextlib
        import io
        import health
        with patch.object(install, "STATE", self.home / "state"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(install.main(["status", "--home", str(self.home)]), 0)
            self.assertNotIn("Problem", out.getvalue())
            for _ in range(3):
                health.failed(self.home / "state", "scan", "database is locked")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(install.main(["status", "--home", str(self.home)]), 1)
            self.assertIn("Problem: scan failed 3 times in a row", out.getvalue())
            self.assertIn("database is locked", out.getvalue())

    def test_upgrade_moves_each_project_into_its_own_copy_and_brings_each_copy_to_this_version(self):
        import contextlib
        import io
        import flow
        import homes
        import store
        state = install.STATE
        workspace = self.home / "my app"
        (workspace / ".git").mkdir(parents=True)
        with patch.object(flow, "T3_CACHES", self.home / "no-t3"):
            engine = flow.Flow(state / "harness.sqlite3", hub=lambda repo: None, jev=lambda config, context, role: {})
            engine.orchestrator({"op": "init", "projectId": "app", "threadId": "orch", "workspace": str(workspace),
                                 "repo": "owner/app", "requiredChecks": ["acceptance"], "noChecksReason": "Test"})
            engine.close()

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = install.main([*argv, "--home", str(self.home)])
            return code, out.getvalue()
        code, out = run("status")
        self.assertIn("my app is still in the shared data: run t3-workflow upgrade", out)
        code, out = run("upgrade")
        self.assertEqual(code, 0)
        (home, data), = homes.listing(state / "homes")
        self.assertIn(f"my app: moved into its own folder {home}", out)
        db = store.connect(state / "harness.sqlite3", readonly=True)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone(), (0,))
        db.close()
        self.assertIn("[x] my app: its own copy, this version", run("status")[1])
        (home / "scripts" / "flow.py").write_text("# older\n")
        self.assertIn("[ ] my app: its own copy is an older version: run t3-workflow upgrade", run("status")[1])
        self.assertIn("my app: brought to this version", run("upgrade")[1])
        self.assertIn("[x] my app: its own copy, this version", run("status")[1])

    def test_a_project_that_cannot_move_is_named_and_the_others_still_move(self):
        import contextlib
        import io
        with patch.object(install, "shared_bindings", return_value=[("a", "/w/first"), ("b", "/w/second")]), \
                patch.object(install.homes, "move", side_effect=[install.homes.Failed("locked"), Path("/h/second")]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(install.main(["upgrade", "--home", str(self.home)]), 1)
        self.assertIn("first: not moved, it stays in the shared data: Failed: locked", out.getvalue())
        self.assertIn("second: moved into its own folder", out.getvalue())

    def test_the_command_runs_a_hook(self):
        import t3_hooks
        with patch.object(t3_hooks, "main", return_value=0) as hook:
            self.assertEqual(install.main(["hook", "codex", "stop"]), 0)
        hook.assert_called_once_with(["codex", "stop"])

    def test_the_hooks_call_the_installed_command_in_place_of_the_python_file(self):
        import shlex
        command = str(self.home / "bin" / "t3-workflow")
        with patch.object(install.shutil, "which", lambda name: command if name == "t3-workflow" else None), \
                patch.object(install, "WINDOWS", False):
            self.run_installer("install")
            self.assertEqual(self.run_installer("install").changed, [])
        claude = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(claude["hooks"]["Stop"][0]["hooks"][0]["command"], shlex.join([command, "hook", "claude", "stop"]))
        codex = json.loads((self.home / ".codex" / "hooks.json").read_text())
        commands = [h["command"] for groups in codex["hooks"].values() for g in groups for h in g["hooks"]]
        self.assertFalse([c for c in commands if "t3_hooks.py" in c], "the old Python-file hook is replaced")
        self.assertIn(shlex.join([command, "hook", "codex", "user_prompt_submit"]), commands)

    def test_the_hooks_call_the_running_command_when_it_is_not_on_path(self):
        # uv warns, but installs, when its tool folder is not on PATH.
        running = self.home / "tools" / "t3-workflow.exe"
        running.parent.mkdir()
        running.write_text("")
        with patch.object(install.shutil, "which", return_value=None), patch.object(install.sys, "argv", [str(running), "install"]), \
                patch.object(install, "WINDOWS", False):
            self.run_installer("install")
        claude = json.loads((self.home / ".claude" / "settings.json").read_text())
        import shlex
        self.assertEqual(shlex.split(claude["hooks"]["Stop"][0]["hooks"][0]["command"]),
                         [str(running.resolve()), "hook", "claude", "stop"])

    def test_install_is_idempotent_and_keeps_other_hooks_in_place(self):
        self.assertTrue(self.run_installer("install").changed)
        self.assertEqual(self.run_installer("install").changed, [])
        codex = json.loads((self.home / ".codex" / "hooks.json").read_text())["hooks"]
        self.assertEqual(codex["PreToolUse"][0], self.other, "other hooks keep their place")
        self.assertIn("mcp__", codex["PreToolUse"][1]["matcher"])
        self.assertNotIn("E:/x", json.dumps(codex), "the old workflow's Codex hook is replaced")
        for event, hook in (("PreToolUse", "pre_tool_use"), ("UserPromptSubmit", "user_prompt_submit"), ("Stop", "stop")):
            (ours,) = [g for g in codex[event] if install.ours(g)]
            command = ours["hooks"][0]["command"]
            self.assertTrue(command.endswith(f"codex {hook}"), command)
            if install.WINDOWS:
                self.assertNotIn('"', command, "Codex on Windows cannot start a hook whose first word is quoted")
        claude = json.loads((self.home / ".claude" / "settings.json").read_text())["hooks"]
        self.assertEqual(claude["PreToolUse"][0]["matcher"], "*")
        self.assertTrue(claude["Stop"][0]["hooks"][0]["command"].endswith("claude stop"))  # The orchestrator's held-report reminder.
        text = (self.home / ".claude" / "CLAUDE.md").read_text()
        self.assertIn("session-default.md", text)
        self.assertNotIn("old path", text)
        self.assertTrue(text.startswith("# Mine") and "keep me" in text)
        self.assertTrue((self.home / ".codex" / "AGENTS.md").read_text().startswith(install.BEGIN))
        goose = json.loads((self.home / ".t3" / "userdata" / "settings.json").read_text())
        self.assertEqual(goose["providerInstances"]["acpRegistry_goose"]["config"]["customModels"], ["vendor/unlisted-model"])
        self.assertTrue(all(ok for _, ok in install.Installer(self.home).status()[:4]))

    def test_install_adds_the_codex_hooks_without_a_hooks_file_and_uninstall_creates_none(self):
        (self.home / ".codex" / "hooks.json").unlink()
        self.run_installer("install")
        codex = json.loads((self.home / ".codex" / "hooks.json").read_text())["hooks"]
        self.assertEqual(sorted(codex), ["PreToolUse", "Stop", "UserPromptSubmit"])
        self.assertTrue(dict(install.Installer(self.home).status())["Codex hooks"])
        (self.home / ".codex" / "hooks.json").unlink()
        self.run_installer("uninstall")
        self.assertFalse((self.home / ".codex" / "hooks.json").exists())

    def test_uninstall_removes_only_workflow_entries(self):
        self.run_installer("install")
        self.run_installer("uninstall")
        codex = json.loads((self.home / ".codex" / "hooks.json").read_text())["hooks"]
        self.assertEqual(codex, {"PreToolUse": [self.other]})
        self.assertNotIn("t3_hooks.py", (self.home / ".claude" / "settings.json").read_text())
        self.assertNotIn(install.BEGIN, (self.home / ".claude" / "CLAUDE.md").read_text())
        goose = json.loads((self.home / ".t3" / "userdata" / "settings.json").read_text())
        self.assertEqual(goose["providerInstances"]["acpRegistry_goose"]["config"]["customModels"], [])

    def test_the_watcher_starts_at_logon_on_windows_in_place_of_the_capacity_watcher(self):
        with patch.object(install, "WINDOWS", True):
            old = self.home / install.STARTUP / "t3-capacity-watch.cmd"
            old.parent.mkdir(parents=True)
            old.write_text('@start "" "pythonw.exe" "E:/x/scripts/capacity_watch.py"')
            self.run_installer("install")
            startup = self.home / install.STARTUP / "t3-workflow-watch.cmd"
            self.assertIn("watch.py", startup.read_text())
            self.assertFalse(old.exists())
            self.assertTrue(dict(install.Installer(self.home).status())["Watcher at logon"])
            self.run_installer("uninstall")
            self.assertFalse(startup.exists())

    def test_the_watcher_starts_at_logon_on_macos_from_a_launch_agent(self):
        import plistlib
        with patch.object(install, "WINDOWS", False), patch.object(install, "PLATFORM", "darwin"):
            self.run_installer("install")
            agent = self.home / "Library" / "LaunchAgents" / "com.t3-workflow.watch.plist"
            plist = plistlib.loads(agent.read_bytes())
            self.assertEqual(plist["ProgramArguments"][1:], [str(install.WATCH_SCRIPT)])
            self.assertTrue(plist["RunAtLoad"])
            self.assertTrue(dict(install.Installer(self.home).status())["Watcher at logon"])
            self.run_installer("uninstall")
            self.assertFalse(agent.exists())

    def test_the_watcher_starts_at_logon_on_linux_from_an_enabled_systemd_user_unit(self):
        calls = []
        with patch.object(install, "WINDOWS", False), patch.object(install, "PLATFORM", "linux"), \
                patch.object(install.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None), \
                patch.object(install.subprocess, "run",
                             lambda args, **kwargs: calls.append(args) or install.subprocess.CompletedProcess(args, 0)):
            self.run_installer("install")
            unit = self.home / ".config" / "systemd" / "user" / "t3-workflow-watch.service"
            self.assertIn(f'"{install.WATCH_SCRIPT}"', unit.read_text())
            self.assertIn(["systemctl", "--user", "enable", "t3-workflow-watch.service"], calls)
            self.assertTrue(dict(install.Installer(self.home).status())["Watcher at logon"])
            self.run_installer("uninstall")
            self.assertIn(["systemctl", "--user", "disable", "t3-workflow-watch.service"], calls)
            self.assertFalse(unit.exists())

    def test_the_requirements_are_python_git_gh_and_claude(self):
        def which(tool):
            return None if tool == "gh" else f"/bin/{tool}"
        with patch.object(install.shutil, "which", which):
            self.assertEqual(install.requirements(), ["`gh` is not on PATH."])

    def test_the_orchestrator_needs_claude_or_codex_not_both(self):
        signed_in = install.subprocess.CompletedProcess(["gh"], 0)
        for missing in (("claude",), ("codex",), ("claude", "codex")):
            with patch.object(install.shutil, "which", lambda tool, m=missing: None if tool in m else f"/bin/{tool}"), \
                    patch.object(install.subprocess, "run", return_value=signed_in):
                problems = install.requirements()
            if len(missing) == 1:
                self.assertEqual(problems, [], missing)
            else:
                self.assertEqual(problems, ["Neither `claude` nor `codex` is on PATH: the orchestrator runs on one of them."])


if __name__ == "__main__":
    unittest.main()
