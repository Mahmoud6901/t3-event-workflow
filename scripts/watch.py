"""The workflow's one background process: it notices workers that went quiet.

While T3 runs, every INTERVAL seconds, it reads T3's database read-only for each working worker's latest activity. A
worker whose T3 run is still going but has not moved for its binding's quietMinutes is told to the user through `next`,
and the orchestrator gets one message per quiet spell naming it. The orchestrator restarts it only on the user's word.
A worker whose run ended is not watched: T3 wakes the orchestrator for that. For a plan without DevOps it also wakes
the orchestrator once per merge step the harness can take (see Flow.merge_wake). Nothing else is sent or changed.
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
import places  # noqa: E402
import watcher  # noqa: E402

INTERVAL = 60
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


def merges(engine, send):
    """For plans without DevOps: the orchestrator is woken when the harness has a merge step to take (a confirmed PR,
    or CI finished on the PR it holds), so its `next` takes it. Returns the projects woken."""
    woken = []
    for project, config, state in engine.projects():
        try:
            result = engine.merge_wake(project)
        except Exception as error:  # GitHub could not say; the next pass asks again.
            health.failed(engine.path.parent, "github", f"{type(error).__name__}: {error}")
            continue
        if not result["send"]:
            continue
        try:
            send(config["orchestrator"], result["message"])
        except Exception as error:  # Not recorded as sent, so the next pass tries again.
            health.failed(engine.path.parent, "send", f"{type(error).__name__}: {error}")
            continue
        health.ok(engine.path.parent, "send")
        engine.merge_woken(project, result["key"])
        woken.append(project)
    return woken


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
                        for project in merges(engine, t3_send):
                            log(f"woke {project}'s orchestrator for a merge step")
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
