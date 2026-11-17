"""What a project's T3 sessions may read and write, in every turn: their own project, never another's or the workflow's."""
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import fence


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.mine, self.tree, self.other = root / "work" / "app", root / "t3" / "worktrees" / "app" / "t1", root / "work" / "shop"
        self.code = root / "workflow"
        self.home = self.code / "state" / "homes" / "app-1"
        self.scratch = root / "scratch"
        self.python = root / "python" / "python.exe"
        self.fence = fence.Fence(cwd=self.mine, own=[self.mine, self.tree], others=[self.other], code=[self.code],
                                 data=[self.code / "state", self.code / "secrets"], home=self.home, temp=self.scratch,
                                 t3=root / "t3" / "userdata")

    def check(self, tool, runtime="claude", **tool_input):
        return self.fence.check(runtime, tool, tool_input)

    def shell(self, command, runtime="claude"):
        return self.check("Bash", runtime, command=command)

    def test_files_are_written_only_in_the_own_project_its_worktrees_and_the_temporary_folder(self):
        self.assertIsNone(self.check("Write", file_path=str(self.mine / "src" / "a.ts")))
        self.assertIsNone(self.check("Edit", file_path="src/a.ts"))  # Relative to the session's folder.
        self.assertIsNone(self.check("Edit", file_path=str(self.tree / "a.ts")))
        self.assertIsNone(self.check("Write", file_path=str(self.scratch / "body.md")))
        for path in (self.other / "a.ts", self.code / "scripts" / "flow.py", self.home / "scripts" / "flow.py",
                     self.mine.parent / "notes.md", self.mine / ".." / "shop" / "a.ts"):
            self.assertIn("T3_WORKFLOW_FENCE", self.check("Write", file_path=str(path)), path)
        self.assertIn("T3_WORKFLOW_FENCE", self.check("NotebookEdit", notebook_path=str(self.other / "n.ipynb")))

    def test_another_projects_files_and_the_workflows_data_are_not_read(self):
        for path in (self.other / "src" / "a.ts", self.code / "state" / "harness.sqlite3", self.code / "secrets" / "key.txt",
                     self.home / "state" / "harness.sqlite3", self.code / "state" / "homes" / "shop-2" / "home.json",
                     self.tmp.name + "/t3/userdata/statev2.sqlite"):
            self.assertIn("T3_WORKFLOW_FENCE", self.check("Read", file_path=str(path)), path)
        self.assertIn("T3_WORKFLOW_FENCE", self.check("Grep", pattern="key", path=str(self.mine.parent)))  # Holds the other project.
        self.assertIn("T3_WORKFLOW_FENCE", self.check("Glob", pattern=str(self.other / "**" / "*.ts")))
        for path in (self.code / "docs" / "notes.md", self.code / ".git" / "config", self.home / "home.json"):
            self.assertIn("T3_WORKFLOW_FENCE", self.check("Read", file_path=str(path)), path)  # Only the published parts.
        for path in (self.mine / "README.md", self.code / "references" / "orchestrator.md", self.home / "references" / "protocol.md",
                     self.code / "scripts" / "flow.py", self.code / "README.md",
                     self.home / "SKILL.md", Path(self.tmp.name) / "elsewhere" / "notes.md"):
            self.assertIsNone(self.check("Read", file_path=str(path)), path)
        self.assertIsNone(self.check("Grep", pattern="x"))
        self.assertIsNone(self.check("mcp__t3__delegate_task", prompt=str(self.other)))

    def test_a_shell_command_naming_another_project_or_the_workflows_data_is_denied(self):
        other = str(self.other).replace("\\", "/")
        for command in (f"cat {other}/src/a.ts", f'Get-Content "{self.other}\\src\\a.ts"', "cat ../shop/a.ts",
                        f"rg key {str(self.code / 'state').replace(chr(92), '/')}", f"rg key {str(self.mine.parent)}"):
            self.assertIn("T3_WORKFLOW_FENCE", self.shell(command), command)
        self.assertIsNone(self.shell("npm test"))
        self.assertIsNone(self.shell("git -C . log --oneline -3 origin/main"))
        self.assertIsNone(self.shell(f"cat {self.code / 'references' / 'implementer.md'}", "codex"))

    def test_the_workflows_code_is_read_but_never_changed_from_a_shell(self):
        flow_py = str(self.code / "scripts" / "flow.py").replace("\\", "/")
        for command in (f"sed -i 's/a/b/' {flow_py}", f"echo x > {flow_py}", f"Set-Content {flow_py} x",
                        f"git -C {str(self.code).replace(chr(92), '/')} commit -am x", f"cp a.py {flow_py}"):
            self.assertIn("T3_WORKFLOW_FENCE", self.shell(command), command)
        self.assertIsNone(self.shell(f"cat {flow_py}"))
        self.assertIn("T3_WORKFLOW_FENCE", self.check("apply_patch", "codex", command=f"*** Begin Patch\n*** Update File: {flow_py}\n@@\n-a\n+b\n*** End Patch"))
        self.assertIsNone(self.check("apply_patch", "codex", command=f"*** Begin Patch\n*** Add File: {self.tree / 'a.ts'}\n+x\n*** End Patch"))

    def test_listing_a_folder_that_holds_hidden_ones_is_fine_but_searching_it_is_not(self):
        parent = str(self.mine.parent).replace("\\", "/")
        for command in (f"ls {parent}", "Get-ChildItem ~", "cat ~/.gitconfig"):
            self.assertIsNone(self.shell(command), command)
        for command in (f"rg key {parent}", f"Get-ChildItem -Recurse {parent}", f"grep -rn key {parent}", f"find {parent} -name x"):
            self.assertIn("T3_WORKFLOW_FENCE", self.shell(command), command)
        self.assertIsNone(self.check("Glob", pattern="*.md", path=parent))
        self.assertIn("T3_WORKFLOW_FENCE", self.check("Glob", pattern="**/*.md", path=parent))

    def test_powershell_and_cmd_variables_name_the_paths_they_hold(self):
        t3 = str(Path(self.tmp.name) / "t3")
        with patch.dict(os.environ, {"T3TESTROOT": t3}):
            for command in ("Get-Content $env:t3testroot/userdata/statev2.sqlite", "type %T3TESTROOT%\\userdata\\statev2.sqlite"):
                self.assertIn("T3_WORKFLOW_FENCE", self.shell(command), command)

    def test_claude_codes_own_plan_and_project_memory_files_are_written(self):
        claude = Path.home() / ".claude"
        memory = claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.mine)) / "memory" / "note.md"
        self.assertIsNone(self.check("Write", file_path=str(claude / "plans" / "plan.md")))
        self.assertIsNone(self.check("Write", file_path=str(memory)))
        for path in (claude / "settings.json", claude / "projects" / "E--other" / "memory" / "note.md"):
            self.assertIn("T3_WORKFLOW_FENCE", self.check("Write", file_path=str(path)), path)

    def test_a_project_bound_in_the_workflows_own_folder_works_in_it(self):
        own = fence.Fence(cwd=self.code, own=[self.code], code=[self.code], data=[self.code / "state"], temp=self.scratch)
        self.assertIsNone(own.check("claude", "Write", {"file_path": str(self.code / "docs" / "notes.md")}))
        self.assertIn("T3_WORKFLOW_FENCE", own.check("claude", "Read", {"file_path": str(self.code / "state" / "x.sqlite3")}))

    def test_a_worker_runs_its_own_projects_harness_and_the_orchestrator_its_workflow_commands(self):
        tree = str(self.tree).replace("\\", "/")
        harness_py = str(self.home / "scripts" / "harness.py").replace("\\", "/")
        self.assertIsNone(self.shell(f"cd {tree}; {self.python} {harness_py} worker pr --worker w1 --token abc --pr 4"))
        self.assertIsNone(self.shell(f"{self.python} -B {harness_py} worker stuck --worker w1 --token abc --why '-x: no access'"))
        python = str(self.python).replace("\\", "/")
        for script in (self.home / "scripts" / "harness.py", self.code / "scripts" / "harness.py"):
            command = f"{python} {str(script).replace(chr(92), '/')} worker pr --worker w1 --token abc --pr 4 --path {self.tree}"
            self.assertIsNone(self.shell(command), command)
            self.assertIsNone(self.shell(f'"{self.python}" -B "{script}" worker stuck --worker w1 --token abc --why "No access"'))
        shop = self.code / "state" / "homes" / "shop-2" / "scripts" / "harness.py"
        self.assertIn("T3_WORKFLOW_FENCE", self.shell(f"{python} {shop} worker pr --worker w1 --token abc --pr 4"))
        self.assertIn("T3_WORKFLOW_FENCE", self.shell(
            f"{python} {self.home / 'scripts' / 'harness.py'} worker pr --worker w1 --token abc; cat {self.other / 'a.ts'}"))
        self.assertIsNone(self.shell("t3-workflow '{\"op\":\"queue_set\",\"text\":\"compare with " + str(self.other).replace("\\", "/") + "\"}'"))


if __name__ == "__main__":
    unittest.main()
