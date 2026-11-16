"""The health of the workflow's background parts: scan, send, hooks, jev, github, worktree and workspace. A part is a
problem after three failures in a row, and its next success clears it; a healthy part takes no space in status. Each
part writes only its own file under state/health, so writers never collide. Standard library only, and reporting never
raises: hooks and the watcher call it.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

FAILURES_BEFORE_PROBLEM = 3
SCAN_QUIET_SECONDS = 600  # Ten of the watcher's waits between scans (watch.INTERVAL, 60 s).
ERROR_CHARS = 300


def record(state_dir, part):
    return Path(state_dir) / "health" / f"{part}.json"


def read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write(path, value):
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def stamp(now=None):
    return (now or datetime.now(timezone.utc)).isoformat()


def failed(state_dir, part, error, now=None):
    try:
        path = record(state_dir, part)
        last = read(path)
        write(path, {**last, "failures": last.get("failures", 0) + 1, "since": last.get("since") or stamp(now),
                     "lastError": str(error)[:ERROR_CHARS]})
    except OSError:
        pass


def ok(state_dir, part, now=None, beat=False):
    """A success clears the count. beat also records when, for a part expected to keep running; other parts write nothing
    unless a failure is on record, since a hook succeeds on every tool call."""
    try:
        path = record(state_dir, part)
        last = read(path)
        if beat or last.get("failures"):
            write(path, {k: v for k, v in {**last, "lastOk": stamp(now) if beat else last.get("lastOk")}.items()
                         if k not in {"failures", "since", "lastError"} and v})
    except OSError:
        pass


def problems(state_dir, scanning=False, now=None):
    """What needs a person. scanning says the watcher should be scanning now, so a scan long overdue counts too."""
    found = []
    folder = Path(state_dir) / "health"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        entry = read(path)
        if entry.get("failures", 0) >= FAILURES_BEFORE_PROBLEM:
            found.append({"part": path.stem, **{k: entry.get(k) for k in ("failures", "since", "lastError")}})
    if scanning and not any(p["part"] == "scan" for p in found):
        last = read(record(state_dir, "scan")).get("lastOk")
        try:
            quiet = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(last) if last else None
        except ValueError:
            quiet = None
        if quiet and quiet.total_seconds() > SCAN_QUIET_SECONDS:
            found.append({"part": "scan", "failures": 0, "since": last,
                          "lastError": f"The watcher holds its lock but has not finished a scan for {int(quiet.total_seconds() // 60)} minutes"})
    return found
