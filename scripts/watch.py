"""The workflow's one background process: it notices workers that went quiet.

While T3 runs, every INTERVAL seconds, it reads T3's database read-only for each working worker's latest activity. A
worker whose T3 run is still going but has not moved for its binding's quietMinutes gets the orchestrator one message per
quiet spell naming it. While the binding is active, that wake's `next` replaces the worker without the user, once per
piece of work; when the replacement goes quiet too, its task is blocked for the user (Flow.quiet). A paused or stopped
binding replaces nobody: the quiet worker is only told to the user.
A worker whose run ended is not watched: T3 wakes the orchestrator for that, and its hook records the end. An end the
hook missed (T3 reworded its notice, say) is recorded once its notice is LATE old, and the orchestrator is woken once
for it (Flow.late_wake). An orchestrator whose own turn stopped at its usage limit is woken once that limit resets,
when work waits for it (Flow.stall_wake). For a plan without DevOps it also wakes the orchestrator once per merge step
the harness can take (see Flow.merge_wake), and once when a quota some work waits for resets (Flow.reset_wake). Nothing
else is sent or changed.
Each project with its own copy (homes.py) has its own watcher, running that copy: the shared one keeps them alive.
A watcher whose code changed (an upgrade) ends, so the next start runs the new code.

    python watch.py          run until stopped (the installer starts it at logon)
    python watch.py --once   one pass, then exit
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow  # noqa: E402
import health  # noqa: E402
import homes  # noqa: E402
import notices  # noqa: E402
import places  # noqa: E402
import watcher  # noqa: E402

INTERVAL = 60
LATE = timedelta(minutes=10)  # Long enough for the orchestrator's own turn to read a notice first.
ACTIVE = ("starting", "running", "waiting")
T3_HOME = places.T3_HOME
NATIVE_DB = T3_HOME / "userdata" / "statev2.sqlite"
T3_CLI = T3_HOME / "bin" / ("t3.cmd" if os.name == "nt" else "t3")
DEFAULT_DB = flow.DEFAULT_DB
LOG = DEFAULT_DB.parent / "watch.log"
HOMES = None if places.HOME else homes.ROOT  # The shared watcher keeps each project's own watcher alive.


def code_version():
    try:
        return homes.fingerprint(places.CODE)
    except OSError:
        return None


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def activity(native, thread_id):
    """(whether a run of the thread is still going, when the thread last moved) from T3's projection."""
    running = native.execute(f"SELECT 1 FROM orchestration_v2_projection_runs WHERE thread_id=? AND status IN "
                             f"({','.join('?' * len(ACTIVE))}) LIMIT 1", (thread_id, *ACTIVE)).fetchone() is not None
    stamps = [row[0] for row in (
        native.execute("SELECT MAX(updated_at) FROM orchestration_v2_projection_turn_items WHERE thread_id=?", (thread_id,)).fetchone(),
        native.execute("SELECT MAX(updated_at) FROM orchestration_v2_projection_messages WHERE thread_id=?", (thread_id,)).fetchone(),
        native.execute("SELECT MAX(requested_at) FROM orchestration_v2_projection_runs WHERE thread_id=?", (thread_id,)).fetchone(),
    ) if row and row[0]]
    return running, max(stamps, key=instant) if stamps else None


def scan(engine, native, send, now=None):
    """One pass over every binding. Returns the workers whose quiet spell the orchestrator was just told about."""
    now = now or datetime.now(timezone.utc)
    told = []
    for project, config, state in engine.projects():
        minutes = config.get("quietMinutes", flow.QUIET_MINUTES)
        for worker in state["workers"].values():
            if worker["state"] != "working" or not worker.get("childThreadId"):
                continue
            running, since = activity(native, worker["childThreadId"])
            if not running or not since or now - instant(since) < timedelta(minutes=minutes):
                continue
            spell = worker.get("quiet") or {}
            if spell.get("since") == since and spell.get("sent"):
                continue
            result = engine.quiet(project, worker["id"], since, minutes)
            if not result["send"]:
                continue
            try:
                send(config["orchestrator"], result["message"])
            except Exception as error:  # The spell stays unsent, so the next pass tries again.
                health.failed(engine.path.parent, "send", f"{type(error).__name__}: {error}")
                continue
            health.ok(engine.path.parent, "send")
            engine.quiet_sent(project, worker["id"], since)
            told.append(worker["id"])
    return told


def notice(native, thread_id, task):
    """T3's notice on the orchestrator's thread that the run of `task` ended: (status, its text, when), or None."""
    for created, payload in native.execute(
            "SELECT created_at, payload_json FROM orchestration_v2_projection_messages WHERE thread_id=? AND role='user' "
            "AND instr(payload_json, ?) > 0 ORDER BY created_at", (thread_id, task)):
        found = notices.of(json.loads(payload)).get(task)
        if found:
            return (*found, created)
    return None


def repair(engine, native, now=None):
    """Each working worker whose latest run's end the hook did not record, though T3's notice of it has been on the
    orchestrator's thread for LATE, gets it recorded, marked late. Returns those workers."""
    now = now or datetime.now(timezone.utc)
    recorded = []
    for project, config, state in engine.projects():
        for worker in state["workers"].values():
            task = (worker.get("taskIds") or [None])[-1]
            if worker["state"] != "working" or not task or (worker.get("ended") or {}).get("taskId") == task \
                    or not config.get("orchestrator"):
                continue
            found = notice(native, config["orchestrator"], task)
            if not found or now - instant(found[2]) < LATE:
                continue
            status, words, _ = found
            if engine.ended(project, {"taskId": task, "status": status, "text": words, "late": True}).get("new"):
                recorded.append(worker["id"])
    return recorded


def lates(engine, send):
    """The orchestrator is woken once for the ends `repair` recorded, so its `next` acts on them. Returns the projects
    woken."""
    return wake(engine, send, engine.late_wake, engine.late_woken, "scan")


def stopped_turn(native, thread_id, now):
    """The orchestrator's latest turn, when it failed at a usage limit that has since reset: its run id, else None.
    Its own model can share a quota with workers (one Claude account, say), so it stops when they do."""
    run = native.execute("SELECT run_id, status, requested_at FROM orchestration_v2_projection_runs WHERE thread_id=? "
                         "ORDER BY requested_at DESC LIMIT 1", (thread_id,)).fetchone()
    if not run or run[1] != "failed" or not run[2]:
        return None
    last = native.execute("SELECT created_at, payload_json FROM orchestration_v2_projection_messages WHERE thread_id=? "
                          "AND role='assistant' AND created_at>=? ORDER BY created_at DESC LIMIT 1", (thread_id, run[2])).fetchone()
    until = flow.quota_until(json.loads(last[1]).get("text"), at=instant(last[0])) if last else None
    return run[0] if until and instant(until) <= now else None


def stalls(engine, native, send, now=None):
    """An orchestrator whose own turn stopped at its usage limit is woken once that limit resets, when work waits for
    its `next`. Returns the projects woken."""
    now = now or datetime.now(timezone.utc)
    runs = {project: stopped_turn(native, config["orchestrator"], now)
            for project, config, _ in engine.projects() if config.get("orchestrator")}
    return wake(engine, send, lambda project: engine.stall_wake(project, runs.get(project)), engine.stall_woken, "scan")


def merges(engine, send):
    """For plans without DevOps: the orchestrator is woken when the harness has a merge step to take (a confirmed PR,
    or CI finished on the PR it holds), so its `next` takes it. Returns the projects woken."""
    return wake(engine, send, engine.merge_wake, engine.merge_woken, "github")


def resets(engine, send):
    """Work that waits for a quota a worker's run stopped at: the orchestrator is woken once that quota resets, so its
    `next` starts it. Returns the projects woken."""
    return wake(engine, send, engine.reset_wake, engine.reset_woken, "scan")


def wake(engine, send, check, woken, problem):
    """Each project whose `check` says so gets its orchestrator woken, and `woken` records it so it happens once."""
    done = []
    for project, config, state in engine.projects():
        try:
            result = check(project)
        except Exception as error:  # It could not say (GitHub, for merges); the next pass asks again.
            health.failed(engine.path.parent, problem, f"{type(error).__name__}: {error}")
            continue
        if not result["send"]:
            continue
        try:
            send(config["orchestrator"], result["message"])
        except Exception as error:  # Not recorded as sent, so the next pass tries again.
            health.failed(engine.path.parent, "send", f"{type(error).__name__}: {error}")
            continue
        health.ok(engine.path.parent, "send")
        woken(project, result["key"])
        done.append(project)
    return done


def t3_send(thread_id, text):
    """Queue the message on the orchestrator's thread through T3's own CLI; a busy thread gets it as its next turn."""
    result = subprocess.run([str(T3_CLI), "thread", "send", thread_id, "-", "--json"], input=text, capture_output=True,
                            text=True, encoding="utf-8", timeout=120)
    if result.returncode:
        raise OSError((result.stderr or result.stdout).strip()[:300] or "t3 thread send failed")
    return json.loads(result.stdout)


def t3_running():
    """T3's server records its pid; the watcher acts only while that process lives."""
    try:
        pid = json.loads((T3_HOME / "userdata" / "server-runtime.json").read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError):
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes  # os.kill on Windows would terminate the process, so ask for its exit code instead.
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def log(line):
    try:
        with open(LOG, "a", encoding="utf-8") as out:
            out.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    lock = watcher.hold(DEFAULT_DB.parent)  # A second watcher exits.
    if not lock:
        return 0
    log("watcher started")
    version = code_version()
    while True:
        try:  # A bad pass must not end the watcher; it is logged, counted and tried again.
            if t3_running() and NATIVE_DB.exists() and DEFAULT_DB.exists():
                engine = flow.Flow(DEFAULT_DB)
                try:
                    native = sqlite3.connect(Path(NATIVE_DB).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
                    try:
                        for worker in scan(engine, native, t3_send):
                            log(f"told the orchestrator {worker} is quiet")
                        for worker in repair(engine, native):
                            log(f"recorded the end of {worker}'s run, which the hook missed")
                        for project in lates(engine, t3_send):
                            log(f"woke {project}'s orchestrator for workers' ends recorded late")
                        for project in stalls(engine, native, t3_send):
                            log(f"woke {project}'s orchestrator: the usage limit its own turn stopped at has reset")
                        for project in merges(engine, t3_send):
                            log(f"woke {project}'s orchestrator for a merge step")
                        for project in resets(engine, t3_send):
                            log(f"woke {project}'s orchestrator: a usage limit its work waits for has reset")
                    finally:
                        native.close()
                finally:
                    engine.close()
                health.ok(DEFAULT_DB.parent, "scan", beat=True)
            if HOMES and t3_running():
                for home, _ in homes.listing(HOMES):
                    if watcher.ensure(home / "state", home / "scripts" / "watch.py"):
                        log(f"started {home.name}'s watcher")
        except Exception as error:
            log(f"scan error: {type(error).__name__}: {error}")
            health.failed(DEFAULT_DB.parent, "scan", f"{type(error).__name__}: {error}")
        if args.once:
            break
        if code_version() != version:
            log("code changed: this watcher ends so the new code starts")
            break
        time.sleep(INTERVAL)
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
