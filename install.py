#!/usr/bin/env python3
"""Install the T3 event workflow on this computer (Windows, macOS or Linux).

    t3-workflow install     add the hooks and instruction pointers (safe to repeat)
    t3-workflow status      show what is installed, and each project's own copy
    t3-workflow upgrade     after updating the package: move each project into its own copy and bring every copy
                            to this version (each bound project runs its own copy of the code, with its own data)
    t3-workflow uninstall   remove them again

From a clone without the command, `python install.py <action>` does the same.
Standard library only. The workflow runs from where it is installed; the installer only
points your tools at it. It edits these files in your home folder and backs each
one up first, to state/backups in its data folder (a clone's own state/, else ~/.t3-workflow):

    ~/.claude/settings.json   Claude Code hooks: they guard the orchestrator and read its workers' wakes
    ~/.codex/hooks.json       the same hooks for an orchestrator on a Codex model (trust them once with /hooks)
    ~/.claude/CLAUDE.md       pointer to references/session-default.md
    ~/.codex/AGENTS.md        pointer to references/session-default.md
    ~/.t3/userdata/settings.json  OpenRouter models T3 does not list for goose (only while T3 is closed)
    Windows Startup folder    t3-workflow-watch.cmd: starts scripts/watch.py at logon, in place of t3-capacity-watch.cmd
    macOS / Linux             a LaunchAgent / a systemd user unit that does the same
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
import health  # noqa: E402 - standard library only, like this file
import homes  # noqa: E402
import places  # noqa: E402
import store  # noqa: E402
import watcher  # noqa: E402
STATE, SECRETS = places.STATE, places.SECRETS  # A clone's own state/, or ~/.t3-workflow for an installed copy.
HOOK_SCRIPT = ROOT / "scripts" / "t3_hooks.py"
WATCH_SCRIPT = ROOT / "scripts" / "watch.py"
STARTUP = Path("AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup")  # Under the home folder.
WATCH_ENTRY, OLD_WATCH_ENTRY = "t3-workflow-watch.cmd", "t3-capacity-watch.cmd"
LAUNCH_AGENT, AGENT_LABEL = Path("Library/LaunchAgents/com.t3-workflow.watch.plist"), "com.t3-workflow.watch"  # macOS
SYSTEMD_UNIT = Path(".config/systemd/user/t3-workflow-watch.service")  # Linux
PLATFORM = sys.platform
BEGIN, END = "<!-- BEGIN T3 EVENT WORKFLOW DEFAULT -->", "<!-- END T3 EVENT WORKFLOW DEFAULT -->"
WINDOWS = os.name == "nt"


def fwd(path):
    return str(path).replace("\\", "/")


# Codex's shell, patch and MCP tools. Its code-mode wrapper is left out: Codex hooks the calls inside it by their own names.
CODEX_MATCHER = "^(Bash|apply_patch|Edit|Write|mcp__.*)$"


def windows_short(path):
    """Codex on Windows cannot start a hook whose first word is quoted, so the path must have no spaces."""
    if " " not in path:
        return path
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)) and " " not in buffer.value:
        return buffer.value
    raise SystemExit(f"Path contains spaces and has no short form: {path}. Move the folder or Python to a path without spaces.")


def installed_command():
    """The t3-workflow command: the one running now (its folder may not be on PATH), else the one on PATH."""
    running = Path(sys.argv[0])
    if running.stem == "t3-workflow" and running.is_file():
        return str(running.resolve())
    return shutil.which("t3-workflow")


def command(hook, runtime="claude"):
    """The installed t3-workflow command when there is one (its path survives upgrades), else Python and the file."""
    tool = installed_command()
    paths, words = ([tool], ["hook", runtime, hook]) if tool else ([sys.executable, str(HOOK_SCRIPT)], [runtime, hook])
    if not WINDOWS:
        return shlex.join(paths + words)
    if runtime == "claude":  # Claude Code runs hooks through Bash, where quotes are fine.
        return " ".join([f'"{fwd(path)}"' for path in paths] + words)
    return " ".join([fwd(windows_short(path)) for path in paths] + words)


def hook_groups(runtime="claude"):
    matcher = "*" if runtime == "claude" else CODEX_MATCHER
    return {"PreToolUse": {"matcher": matcher, "hooks": [{"type": "command", "command": command("pre_tool_use", runtime), "timeout": 120}]},
            "UserPromptSubmit": {"hooks": [{"type": "command", "command": command("user_prompt_submit", runtime), "timeout": 30}]},
            "Stop": {"hooks": [{"type": "command", "command": command("stop", runtime), "timeout": 30}]}}


OUR_COMMAND = re.compile(r"\shook\s+(claude|codex)\s+(pre_tool_use|user_prompt_submit|stop)$")


def ours(group):
    """A hook this installer wrote: the Python-file form, or the t3-workflow command (whatever its path looks like)."""
    return any("t3_hooks.py" in h.get("command", "") or OUR_COMMAND.search(h.get("command", ""))
               for h in group.get("hooks", []))


def pointer_block():
    target = ROOT / "references" / "session-default.md"
    return (f"{BEGIN}\n## T3 Code workflow (opt-in)\n\n"
            "Inside T3 Code, use the T3 event workflow only when the user's message says\n"
            "\"use t3 workflow\", or when a harness event arrives in a bound session. Then read\n"
            f"`{target}` and follow it,\n"
            "reusing the instructions in context. Otherwise answer as an ordinary session,\n"
            "even in a bound one. This policy does not apply merely because another\n"
            f"application exposes a T3 connector.\n{END}")


class Installer:
    def __init__(self, home, dry_run=False):
        self.home = Path(home)
        self.stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.backups = STATE / "backups"
        self.dry_run = dry_run
        self.changed = []

    # ---- files -------------------------------------------------------------
    def read_json(self, path):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def write(self, path, text):
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == text:
            return
        self.changed.append(str(path))
        if self.dry_run:
            return
        if old is not None:
            self.backups.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, self.backups / f"{path.parent.name}-{path.name}-before-install-{self.stamp}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def write_json(self, path, data):
        self.write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def delete(self, path):
        if path.exists():
            self.changed.append(str(path))
            if not self.dry_run:
                path.unlink()

    # ---- hooks -------------------------------------------------------------
    def set_hooks(self, path, groups, remove=False):
        if remove and not path.exists():
            return
        data = self.read_json(path)
        hooks = data.setdefault("hooks", {})
        for event, group in groups.items():
            existing = hooks.setdefault(event, [])
            index = next((i for i, g in enumerate(existing) if ours(g)), None)
            existing[:] = [g for g in existing if not ours(g)]
            if not remove:
                existing.insert(len(existing) if index is None else index, group)  # Keep its place among the others.
            if not existing:
                del hooks[event]
        self.write_json(path, data)

    def set_pointer(self, path, remove=False):
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if BEGIN in text and END in text:
            start, end = text.index(BEGIN), text.index(END) + len(END)
            text = text[:start] + ("" if remove else pointer_block()) + text[end:]
        elif not remove:
            text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + pointer_block() + "\n"
        self.write(path, text)

    # ---- T3 goose models -------------------------------------------------------
    def goose_models(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import flow
        return [m for m, spec in flow.MODELS.items() if spec["provider"] == flow.GOOSE]

    def t3_running(self):
        try:
            if WINDOWS:
                out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30).stdout
                return any(line.strip('"').startswith("T3 Code") for line in out.splitlines())
            return subprocess.run(["pgrep", "-f", "T3 Code"], capture_output=True).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return True  # Unknown: do not risk an edit T3 would overwrite.

    def set_goose_models(self, remove=False):
        settings = self.home / ".t3" / "userdata" / "settings.json"
        data = self.read_json(settings)
        goose = (data.get("providerInstances") or {}).get("acpRegistry_goose")
        if goose is None:
            return "T3 has no goose provider; OpenRouter models need goose with the OpenRouter provider."
        if self.t3_running():
            return "T3 is running, so its settings were not changed. Quit T3 and run this again for goose models."
        cache = self.read_json(self.home / ".t3" / "caches" / "acpRegistry_goose.json")
        listed = {m.get("slug") for m in cache.get("models") or []}
        custom = goose.setdefault("config", {}).setdefault("customModels", [])
        wanted = [m for m in self.goose_models() if m not in listed]  # T3 lists at most 256 discovered models.
        if remove:
            custom[:] = [m for m in custom if m not in self.goose_models()]
        else:
            custom.extend(m for m in wanted if m not in custom)
        self.write_json(settings, data)
        return None

    # ---- watcher -----------------------------------------------------------------
    def watcher_entry(self):
        """What starts the watcher at logon: the Startup folder on Windows, a LaunchAgent on macOS, else systemd."""
        if WINDOWS:
            return self.home / STARTUP / WATCH_ENTRY
        return self.home / (LAUNCH_AGENT if PLATFORM == "darwin" else SYSTEMD_UNIT)

    def watcher_command(self):
        if WINDOWS:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            python = pythonw if pythonw.exists() else Path(sys.executable)  # pythonw runs without a console window.
            return f'@start "" "{python}" "{WATCH_SCRIPT}"\n'
        if PLATFORM == "darwin":
            return plistlib.dumps({"Label": AGENT_LABEL, "ProgramArguments": [sys.executable, str(WATCH_SCRIPT)],
                                   "RunAtLoad": True, "ProcessType": "Background"}).decode("utf-8")
        return ("[Unit]\nDescription=T3 event workflow watcher\n\n"
                f'[Service]\nExecStart="{sys.executable}" "{WATCH_SCRIPT}"\nRestart=on-failure\n\n'
                "[Install]\nWantedBy=default.target\n")

    def set_watcher(self, remove=False):
        """Start watch.py at logon (on Windows in place of the old capacity watcher); it acts only while T3 runs."""
        if WINDOWS:
            self.delete(self.home / STARTUP / OLD_WATCH_ENTRY)
        systemd = not WINDOWS and PLATFORM != "darwin"
        if remove:
            note = self.systemctl("disable") if systemd and self.watcher_entry().exists() else None
            self.delete(self.watcher_entry())
            return note
        self.write(self.watcher_entry(), self.watcher_command())
        return self.systemctl("enable") if systemd else None

    def systemctl(self, verb):
        """A systemd unit starts at logon only once enabled. Best effort: a note says what to run when it fails."""
        name, manual = SYSTEMD_UNIT.name, f"systemctl --user {verb} {SYSTEMD_UNIT.name}"
        if self.dry_run:
            return None
        if not shutil.which("systemctl"):
            return f"No systemctl here: start the watcher at login yourself ({sys.executable} {WATCH_SCRIPT})."
        try:
            done = subprocess.run(["systemctl", "--user", verb, name], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            return f"{manual} did not run ({error}); run it yourself."
        return f"{manual} failed; run it yourself." if done.returncode else None

    # ---- commands --------------------------------------------------------------
    def apply(self, remove=False):
        self.set_hooks(self.home / ".claude" / "settings.json", hook_groups(), remove)
        self.set_hooks(self.home / ".codex" / "hooks.json", hook_groups("codex"), remove)
        self.set_pointer(self.home / ".claude" / "CLAUDE.md", remove)
        self.set_pointer(self.home / ".codex" / "AGENTS.md", remove)
        note = self.set_goose_models(remove)
        watcher_note = self.set_watcher(remove)
        note = "\n".join(n for n in (note, watcher_note) if n) or None
        if not remove and not self.dry_run:
            STATE.mkdir(parents=True, exist_ok=True)
        return note

    def status(self):
        rows = []
        for name, path, runtime in (("Claude hooks", self.home / ".claude" / "settings.json", "claude"),
                                    ("Codex hooks", self.home / ".codex" / "hooks.json", "codex")):
            hooks = self.read_json(path).get("hooks") or {}
            rows.append((name, all(group in hooks.get(event, []) for event, group in hook_groups(runtime).items())))
        for name, path in (("Claude pointer", self.home / ".claude" / "CLAUDE.md"), ("Codex pointer", self.home / ".codex" / "AGENTS.md")):
            rows.append((name, path.exists() and pointer_block() in path.read_text(encoding="utf-8")))
        entry = self.watcher_entry()
        rows.append(("Watcher at logon", entry.exists() and str(WATCH_SCRIPT) in entry.read_text(encoding="utf-8")
                     and not (WINDOWS and (self.home / STARTUP / OLD_WATCH_ENTRY).exists())))
        key = bool(os.environ.get("TYPESAFE_API_KEY")) or (SECRETS / "typesafe-api-key.txt").is_file()
        rows.append((f"Jev API key, optional ({SECRETS / 'typesafe-api-key.txt'} or TYPESAFE_API_KEY)", key))
        return rows


def requirements():
    problems = []
    if sys.version_info < (3, 11):
        problems.append(f"Python 3.11+ needed (this is {sys.version.split()[0]}).")
    for tool in ("git", "gh"):
        if not shutil.which(tool):
            problems.append(f"`{tool}` is not on PATH.")
    if not (shutil.which("claude") or shutil.which("codex")):
        problems.append("Neither `claude` nor `codex` is on PATH: the orchestrator runs on one of them.")
    if shutil.which("gh"):
        try:
            if subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=60).returncode:
                problems.append("`gh` is not signed in to GitHub: run gh auth login.")
        except (OSError, subprocess.SubprocessError):
            problems.append("`gh auth status` did not run.")
    return problems


NEXT_STEPS = f"""Next steps:
  1. Jev key (optional): put the raw key on one line in {SECRETS / "typesafe-api-key.txt"}, or set TYPESAFE_API_KEY.
  2. GitHub (optional): gh auth refresh -s workflow. Pushes that change CI workflow files need it when git signs in
     through gh; with Git Credential Manager, only a DevOps update where a branch and main changed the same
     workflow file does.
  3. Codex (only for an orchestrator on a Codex model): run codex, type /hooks and trust the three t3_hooks.py
     hooks. Codex skips a hook it has not trusted, and asks again after an install changes one.
  4. Restart T3 Code so its Claude and Codex sessions load the hooks. The watcher starts at your next logon, or
     with the next workflow turn.
  5. In a T3 project hosted on GitHub, open a Claude or Codex session and say "use t3 workflow: set up this
     project". The binding starts paused."""


def shared_bindings(state):
    """(project id, workspace) of each binding still kept in the shared database, as before each project had its own copy."""
    if not (Path(state) / "harness.sqlite3").exists():
        return []
    db = store.connect(Path(state) / "harness.sqlite3", readonly=True)
    try:
        return [(project, json.loads(config)["workspace"]) for project, config in db.execute("SELECT id, config FROM projects")]
    finally:
        db.close()


def upgrade(state):
    """Each binding still in the shared database moves into its own copy, and every copy's code becomes this version's.
    A worker started before keeps working: the shared harness passes its reports on. Returns the lines to show and
    whether every project made it; one that fails is named, and the others go on."""
    lines, ok = [], True
    for project, workspace in shared_bindings(state):
        try:
            home = homes.move(Path(state) / "harness.sqlite3", project, Path(state) / "homes", shared=Path(state).parent)
            lines.append(f"{Path(workspace).name}: moved into its own folder {home}")
        except Exception as error:  # Its binding stays in the shared data, served as before.
            ok = False
            lines.append(f"{Path(workspace).name}: not moved, it stays in the shared data: {type(error).__name__}: {error}")
    for home, data in homes.listing(Path(state) / "homes"):
        try:
            if homes.upgrade(home):
                lines.append(f"{Path(data['workspace']).name}: brought to this version")
        except OSError as error:
            ok = False
            lines.append(f"{Path(data['workspace']).name}: not brought to this version: {error}")
    return lines or ["Every project already has its own copy at this version."], ok


def project_status(state):
    """A status row per project: its own copy at this version, an older one, or still in the shared data."""
    rows = [(f"{Path(workspace).name} is still in the shared data: run t3-workflow upgrade", False)
            for _, workspace in shared_bindings(state)]
    version = homes.fingerprint(ROOT)
    for home, data in homes.listing(Path(state) / "homes"):
        current = homes.fingerprint(home) == version
        rows.append((f"{Path(data['workspace']).name}: its own copy, this version" if current else
                     f"{Path(data['workspace']).name}: its own copy is an older version: run t3-workflow upgrade", current))
    return rows


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:  # t3-workflow hook <runtime> <hook>: what the installed hooks run.
        import t3_hooks
        return t3_hooks.main(argv[1:])
    parser = argparse.ArgumentParser(prog="t3-workflow", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["install", "status", "upgrade", "uninstall"])
    parser.add_argument("--home", default=Path.home(), help=argparse.SUPPRESS)  # For tests.
    parser.add_argument("--dry-run", action="store_true", help="show which files would change")
    args = parser.parse_args(argv)
    installer = Installer(args.home, args.dry_run)
    if args.action == "status":
        for name, ok in installer.status() + project_status(STATE):
            print(f"  [{'x' if ok else ' '}] {name}")
        problems = [(None, p) for p in health.problems(STATE, scanning=watcher.running(STATE))]
        for home, data in homes.listing(STATE / "homes"):
            problems += [(Path(data["workspace"]).name, p) for p in
                         health.problems(home / "state", scanning=watcher.running(home / "state"))]
        for name, problem in problems:
            print(f"  Problem{f' in {name}' if name else ''}: {problem['part']} failed {problem['failures']} times in a row "
                  f"since {problem['since']}: {problem['lastError']}")
        return 1 if problems else 0
    if args.action == "upgrade":
        lines, ok = upgrade(STATE)
        for line in lines:
            print(f"  {line}")
        return 0 if ok else 1
    if args.action == "install":
        problems = requirements()
        for problem in problems:
            print("  missing:", problem)
    note = installer.apply(remove=args.action == "uninstall")
    verb = "Would change" if args.dry_run else "Changed"
    print(f"{verb}: " + (", ".join(installer.changed) if installer.changed else "nothing (already up to date)"))
    if note:
        print("Note:", note)
    if args.action == "install":
        print(NEXT_STEPS)
    else:
        print(f"Removed. Backups are in {STATE / 'backups'}; restart T3 Code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
