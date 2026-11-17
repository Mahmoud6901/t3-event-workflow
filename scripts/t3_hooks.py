"""The orchestrator's guard, as Claude Code and Codex hooks.

Hook settings run: t3-workflow hook <claude|codex> <pre_tool_use|user_prompt_submit|stop>, with the hook JSON on stdin.
Only a bound orchestrator's thread is acted on, and only in a workflow turn: the user's "use t3 workflow", or a wake.
T3 keeps a thread's id when the user moves it between Claude and Codex, so one binding serves both runtimes.
GitHub's protection of main is the hard guard on what workers merge.
In the shared install, the hook first finds the session's project. Every T3 session of a bound project, orchestrator
or worker, in every turn, stays inside its own project (fence.py); goose runs no hooks. A project with its own copy
(homes.py) is then served by that copy's hook. Inside a bound workspace an error denies or blocks: fail closed.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
from fence import Fence, operation  # noqa: E402 - standard library only, like this file
import health  # noqa: E402 - standard library only, like this file
import homes  # noqa: E402
import places  # noqa: E402
import store  # noqa: E402 - standard library only, like this file
import trigger  # noqa: E402
import watcher  # noqa: E402
# flow loads only in an orchestrator's session; every other tool call stays fast.

HARNESS_DB = places.STATE / "harness.sqlite3"  # Same as flow.DEFAULT_DB.
LIVE_DB = HARNESS_DB  # The watcher serves only this database, so a test's database never starts one.
HOMES = None if places.HOME else homes.ROOT  # The shared install routes to projects' own copies; a copy serves itself.
NATIVE_DB = places.T3_HOME / "userdata" / "statev2.sqlite"
REFERENCES = ROOT / "references"
ROLE_DOCS = {"orchestrator": ("orchestrator.md", "protocol.md")}
ACTIVE = ("starting", "running", "waiting")
TRACE = []  # Why this call decided what it did; written to state/hook-calls.jsonl.
READ_TOOLS = {"Read", "Grep", "Glob", "AskUserQuestion", "ToolSearch"}
T3_TOOLS = {"delegate_task", "task_cancel", "task_status", "task_list", "orchestrator_capabilities"}
NO_SESSIONS = {"t3_thread_launch"}  # The user's rule: no new sessions; workers are delegated tasks.
WAKE = re.compile(r"^Delegated task (\S+)(?: \((.*)\))? \[([A-Za-z_]+)\]:", re.MULTILINE)
NEXT = "t3-workflow '{\"op\":\"next\"}'"
RUNTIMES = ("claude", "codex")
# Codex has no Read, Grep or Glob tools, so a single plain read command is allowed too (in either runtime).
READ_COMMANDS = {"get-content", "gc", "cat", "type", "get-childitem", "gci", "ls", "dir", "rg", "select-string", "sls"}
SHELL_SYNTAX = re.compile(r"[;|&<>`$(){}@\r\n]")  # Chains, pipes, redirection, expansion, subexpressions, script blocks.
READS = "Get-Content, Get-ChildItem, rg or Select-String, one plain command without pipes or redirection"


def orchestrator_context(runtime):
    reads = f" Read files with {READS}." if runtime == "codex" else ""
    return ("T3 workflow orchestrator: you plan with the user and report to them; workers do the work in the background. "
            f"Follow {REFERENCES / 'orchestrator.md'}.{reads} On every wake run the shell command {NEXT}, make each call it "
            "returns in order, record what T3 returned with t3-workflow '{\"op\":\"done\",\"results\":[...]}', give the "
            "user its tell lines and put its asks to them, then end your turn. On the user's message, plan with queue_set "
            "or queue_edit. You edit nothing and run no other command.")


def bindings():
    if not Path(HARNESS_DB).exists():
        return []
    db = store.connect(HARNESS_DB, readonly=True)
    try:
        return [(json.loads(c), json.loads(s)) for c, s in db.execute("SELECT config, state FROM projects")]
    finally:
        db.close()


worktrees = homes.worktrees


def inside_workspace(cwd, rows):
    if not cwd:
        return False
    here = str(Path(cwd).resolve()).casefold()
    for config, _ in rows:
        for folder in worktrees(config["workspace"]):
            root = str(folder.resolve()).casefold()
            if here == root or here.startswith(root.rstrip("\\/") + ("\\" if "\\" in root else "/")):
                return True
    return False


class Native:
    """Read-only view of T3's projection database."""

    def __init__(self):
        self.db = sqlite3.connect(Path(NATIVE_DB).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)

    def close(self):
        self.db.close()

    def thread_for_session(self, session_id):
        # T3 records each provider's own session/thread ID as nativeThreadRef.nativeId.
        for thread_id, payload in self.db.execute(
                "SELECT thread_id, payload_json FROM orchestration_v2_projection_provider_threads WHERE payload_json LIKE ?",
                (f"%{session_id}%",)):
            if (json.loads(payload).get("nativeThreadRef") or {}).get("nativeId") == session_id:
                return thread_id
        return None

    def active_run(self, thread_id):
        row = self.db.execute(
            f"SELECT run_id FROM orchestration_v2_projection_runs WHERE thread_id=? AND status IN ({','.join('?' * len(ACTIVE))}) "
            "ORDER BY ordinal DESC LIMIT 1", (thread_id, *ACTIVE)).fetchone()
        return row[0] if row else None

    def run_messages(self, thread_id, run_id):
        """Every user-role message of the turn, oldest first: T3 adds a finished child's notice to a running turn."""
        return [json.loads(p) for (p,) in self.db.execute(
            "SELECT payload_json FROM orchestration_v2_projection_messages WHERE run_id=? AND thread_id=? AND role='user' "
            "ORDER BY created_at, message_id", (run_id, thread_id))]


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


def block(reason):
    return {"decision": "block", "reason": reason}


def tool_suffix(name):
    return name.split("__")[-1] if name.startswith("mcp__") else name


def unquoted(word):
    """A word PowerShell, cmd and Bash all read the same without quotes: Windows gives a spaced path its short form."""
    word = word.replace("\\", "/")
    if " " in word and os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(word, buffer, len(buffer)):
            word = buffer.value.replace("\\", "/")
    if re.search(r"[\s\"'`$;&|<>(){}@]", word):
        raise ValueError(f"The workflow's path needs quotes, which Codex's PowerShell cannot run: {word}. "
                         "Move Python or the workflow folder to a path without spaces.")
    return word


def harness_command(config, thread_id, request, runtime="claude"):
    """The harness command for this operation, as this binding's orchestrator whatever identity the request named."""
    request = {**request, "projectId": config["projectId"], "threadId": thread_id}
    args = [sys.executable, "-B", str(SCRIPTS / "harness.py"), "--db", str(HARNESS_DB)]
    encoded = base64.b64encode(json.dumps(request, ensure_ascii=False).encode("utf-8")).decode("ascii")
    if runtime == "codex":
        # PowerShell runs a bare path but treats a quoted first word as a string, and it decodes a program's output with
        # the console code page, so the answer comes back in ASCII.
        return " ".join(unquoted(a) for a in args) + " --ascii --op-base64 " + encoded
    if any(re.search(r"[\"'`$\r\n]", a) for a in args):
        raise ValueError("Unsafe workflow command argument")
    return " ".join('"' + a.replace("\\", "/") + '"' for a in args) + " --op-base64 " + encoded


def shell_input(tool_input):
    """The shell command and its key: Claude's Bash and Codex's exec_command name it differently."""
    for key in ("command", "cmd"):
        if isinstance(tool_input.get(key), str):
            return key, tool_input[key]
    return "command", None


def read_command(command):
    """One plain read, with nothing that could chain, pipe, redirect or expand into another command."""
    if not isinstance(command, str) or SHELL_SYNTAX.search(command):
        return False
    words = command.split()
    if not words or words[0].casefold() not in READ_COMMANDS:
        return False
    # rg starts a program for --pre and --hostname-bin, and decompressors for -z.
    return not (words[0].casefold() == "rg" and any(
        w.startswith(("--pre", "--hostname-bin")) or w == "--search-zip" or re.fullmatch(r"-[A-Za-z]*z[A-Za-z]*", w)
        for w in words[1:]))


def orchestrator_tool(config, thread_id, tool, tool_input, runtime="claude"):
    """The orchestrator reads, uses T3's thread and delegation tools, and runs the workflow; nothing else."""
    suffix = tool_suffix(tool)
    if tool in READ_TOOLS:
        return None
    if tool.startswith("mcp__") and suffix not in NO_SESSIONS and (suffix in T3_TOOLS or suffix.startswith("t3_")):
        return None
    if tool == "Bash":
        key, command = shell_input(tool_input)
        request = operation(command)
        if request is not None:
            updated = {**tool_input, key: harness_command(config, thread_id, request, runtime)}
            if runtime == "claude":
                updated["run_in_background"] = False
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": updated}}
        if re.match(r"^\s*t3-workflow\b", command or ""):
            return deny("T3_WORKFLOW_ORCHESTRATOR: invalid workflow command. Use exactly t3-workflow '<JSON>' or "
                        "t3-workflow <base64 JSON>, without shell syntax.")
        if read_command(command):
            return None
    return deny("T3_WORKFLOW_ORCHESTRATOR: the orchestrator plans and reports; workers do the work. It edits nothing and "
                f"runs no other command. Use t3-workflow '<JSON>' ({NEXT} on every wake) and make the calls next returns. "
                f"To read, use {READS}.")


def wakes(native, thread_id, prompt=None):
    """{T3 task id: status} for each "Delegated task …" notice in this turn (and the prompt being submitted)."""
    run_id = native.active_run(thread_id)
    texts = [m.get("text") or "" for m in (native.run_messages(thread_id, run_id) if run_id else [])]
    if isinstance(prompt, str):
        texts.append(prompt)
    return {match.group(1): match.group(3).lower() for text in texts for match in WAKE.finditer(text)}


def record_wakes(config, found):
    """Each notice becomes `ended` for its worker; returns how many were news."""
    if not found:
        return 0
    import flow
    engine = flow.Flow(HARNESS_DB)
    try:
        new = sum(1 for task_id, status in found.items()
                  if engine.ended(config["projectId"], {"taskId": task_id, "status": status}).get("new"))
    finally:
        engine.close()
    TRACE.append(f"wakes {sorted(found)}: {new} new")
    return new


def stop_check(native, thread_id, config, state, data):
    """The turn does not end with calls from next unrecorded, or over a worker's end that arrived during it. One
    reminder per turn end; never a loop."""
    late = record_wakes(config, wakes(native, thread_id))
    if data.get("stop_hook_active"):
        return None
    reasons = []
    if state["calls"]:
        reasons.append(f"These calls from next are not recorded: {', '.join(state['calls'])}. Make each one, then record "
                       "what T3 returned with t3-workflow '{\"op\":\"done\",\"results\":[...]}'.")
    if late:
        reasons.append(f"A worker finished during this turn. Run {NEXT} again and act on it.")
    if not reasons:
        return None
    TRACE.append("blocked: " + " ".join(reasons))
    return block("T3_WORKFLOW_UNFINISHED: " + " ".join(reasons))


def docs_note(thread_id, role):
    """A note when the role's docs changed since this session last saw them, once per change. A session seen for the first
    time only starts the record. Never changes what the hook decides: anything unreadable means no note."""
    try:
        docs = {name: hashlib.sha256((REFERENCES / name).read_bytes()).hexdigest() for name in ROLE_DOCS.get(role, ())}
        if not docs:
            return ""
        record = HARNESS_DB.parent / "docs-seen" / f"{hashlib.sha256(thread_id.encode()).hexdigest()[:32]}.json"
        seen = json.loads(record.read_text(encoding="utf-8")) if record.exists() else None
        if seen == docs:
            return ""
        record.parent.mkdir(parents=True, exist_ok=True)
        temporary = record.with_name(f"{record.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(docs), encoding="utf-8")
        os.replace(temporary, record)
        changed = sorted(name for name, digest in docs.items() if seen is not None and seen.get(name) != digest)
        return (" Workflow docs changed since this session last saw them. Read them again before you act: "
                + ", ".join(str(REFERENCES / name) for name in changed) + ".") if changed else ""
    except (OSError, ValueError, AttributeError):
        return ""


def ordinary_turn(native, thread_id):
    """The active run id of a turn of only the user's own words, none of them "use t3 workflow"; otherwise None.
    Such a turn is ordinary: no workflow context, guard or reminder (the user's rule, 2026-11-03)."""
    run_id = native.active_run(thread_id)
    if run_id and trigger.plain(native.run_messages(thread_id, run_id), trigger.older_turns(native.db, thread_id, run_id)):
        TRACE.append("ordinary turn: the user did not ask for the workflow")
        return run_id
    return None


def keep_watcher_alive(home=None):
    """A bound session restarts a dead watcher (its project's own, for a project with its own copy), which nothing else
    does between logons. This never changes a hook's result."""
    if Path(HARNESS_DB) != Path(LIVE_DB):
        return
    try:
        if home and watcher.ensure(Path(home) / "state", Path(home) / "scripts" / "watch.py"):
            TRACE.append(f"{Path(home).name}: its watcher restarted")
        if watcher.ensure(Path(HARNESS_DB).parent):  # The shared one, which keeps every project's own watcher alive.
            TRACE.append("watcher restarted")
    except Exception as error:
        TRACE.append(f"watcher not restarted: {type(error).__name__}: {error}")


def fence_for(cwd, workspace, home=None):
    """The fence of the project whose folder is `workspace` (its own copy is `home`, if it has one)."""
    others = [folder for h, data in homes.listing(HOMES) if h != home for folder in worktrees(data["workspace"])]
    others += [folder for config, _ in bindings() if config["workspace"] != workspace for folder in worktrees(config["workspace"])]
    return Fence(cwd, worktrees(workspace), others, code=[places.CODE],
                 data=[Path(HARNESS_DB).parent, places.SECRETS, *([HOMES] if HOMES else [])], home=home,
                 t3=Path(NATIVE_DB).parent)


def delegate(home, runtime, hook, data):
    """The project's own copy's hook decides, from its own code and data."""
    done = subprocess.run([sys.executable, "-B", str(Path(home) / "scripts" / "t3_hooks.py"), runtime, hook],
                          input=json.dumps(data).encode("utf-8"), capture_output=True, timeout=100)
    if done.returncode:
        raise RuntimeError(f"{Path(home).name}: its hook failed: {done.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    answer = done.stdout.decode("utf-8").strip()
    return json.loads(answer) if answer else None


def route(runtime, hook, data):
    """In the shared install: find the session's project, fence it, and let its own copy, if it has one, decide."""
    cwd = data.get("cwd")
    found = homes.find(cwd, HOMES) if HOMES else None
    shared = None if found else next((c for c, s in bindings() if inside_workspace(cwd, [(c, s)])), None)
    if not found and not shared:
        TRACE.append("outside bound workspaces")
        return None
    native = Native()
    try:
        thread_id = native.thread_for_session(data.get("session_id", ""))
    finally:
        native.close()
    if not thread_id:
        TRACE.append("session unknown to T3")
        return None  # Not a T3 session (for example a plain CLI session in the same folder).
    home = found[0] if found else None
    if hook == "pre_tool_use":
        reason = fence_for(cwd, found[1]["workspace"] if found else shared["workspace"], home).check(
            runtime, data.get("tool_name") or "", data.get("tool_input") or {})
        if reason:
            TRACE.append(f"fenced: {reason}")
            return deny(reason)
    if not home:
        return handle(runtime, hook, data)
    keep_watcher_alive(home)
    if not any(config.get("orchestrator") == thread_id for config, _ in homes.rows(home)):
        TRACE.append(f"thread={thread_id} is no orchestrator of {home.name}")
        return None
    TRACE.append(f"served by {home.name}")
    return delegate(home, runtime, hook, data)


def handle(runtime, hook, data):
    rows = bindings()
    if not inside_workspace(data.get("cwd"), rows):
        TRACE.append("outside bound workspaces")
        return None
    native = Native()
    try:
        thread_id = native.thread_for_session(data.get("session_id", ""))
        if not thread_id:
            TRACE.append("session unknown to T3")
            return None  # Not a T3 session (for example a plain CLI session in the same folder).
        bound = next(((c, s) for c, s in rows if c.get("orchestrator") == thread_id), None)
        if not bound:
            TRACE.append(f"thread={thread_id} is no orchestrator")
            return None  # A worker, or a session of the old workflow.
        config, state = bound
        TRACE.append(f"orchestrator {thread_id} of {config['projectId']}")
        keep_watcher_alive()
        if hook == "pre_tool_use":
            verdict = orchestrator_tool(config, thread_id, data.get("tool_name") or "", data.get("tool_input") or {}, runtime)
            # An ordinary turn runs any tool; workflow commands are still rewritten.
            denied = ((verdict or {}).get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
            return None if denied and ordinary_turn(native, thread_id) else verdict
        if hook == "stop":
            verdict = stop_check(native, thread_id, config, state, data)
            return None if verdict and ordinary_turn(native, thread_id) else verdict
        if hook == "user_prompt_submit":
            record_wakes(config, wakes(native, thread_id, data.get("prompt")))
            if ordinary_turn(native, thread_id):
                return None
            return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                           "additionalContext": orchestrator_context(runtime) + docs_note(thread_id, "orchestrator")}}
        return None
    finally:
        native.close()


def main(argv=None):
    runtime, hook = (sys.argv[1:] if argv is None else argv)[:2]
    if runtime not in RUNTIMES:
        return 0
    data = {}
    verdict = None
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        verdict = route(runtime, hook, data) if HOMES else handle(runtime, hook, data)
        health.ok(HARNESS_DB.parent, "hooks")
    except Exception as error:  # Fail closed: a crashing hook would otherwise let the action through.
        message = f"T3_WORKFLOW_GUARD_ERROR: {type(error).__name__}: {error}. Nothing was executed; read workflow status."
        try:
            in_workflow = inside_workspace(data.get("cwd"), bindings()) or bool(HOMES and homes.find(data.get("cwd"), HOMES))
        except Exception:
            in_workflow = True
        if in_workflow:
            health.failed(HARNESS_DB.parent, "hooks", f"{hook}: {type(error).__name__}: {error}")
        # A stop reminder is no guard: its error ends the turn instead of looping on a block.
        verdict = None if not in_workflow or hook == "stop" else (deny(message) if hook == "pre_tool_use" else block(message))
        TRACE.append(message)
    if TRACE[:1] != ["outside bound workspaces"]:
        log(runtime, hook, data, verdict)
    if verdict:
        sys.stdout.write(json.dumps(verdict))
    return 0


def log(runtime, hook, data, verdict):
    """One line per workflow-relevant call, so it is visible what the hooks decided."""
    try:
        line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "runtime": runtime, "hook": hook,
                "session": data.get("session_id"), "cwd": data.get("cwd"), "tool": data.get("tool_name"),
                "trace": TRACE, "decision": (verdict or {}).get("decision") or
                ((verdict or {}).get("hookSpecificOutput") or {}).get("permissionDecision") or "none"}
        with open(HARNESS_DB.parent / "hook-calls.jsonl", "a", encoding="utf-8") as out:
            out.write(json.dumps(line) + "\n")
    except OSError:
        pass  # Logging never changes a guard decision.


if __name__ == "__main__":
    sys.exit(main())
