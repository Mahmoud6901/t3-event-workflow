"""How each model did in the owner's projects: counts by model, role and size, which Jev reads on each option.

The record is one SQLite file in the shared data folder that every bound project on this computer uses
(places.RECORD), never inside a repository. It holds counts only: no task names, briefs, repositories, project ids,
paths or dates. Each project's harness adds to it as each task merges or is dropped; SQLite's lock and one statement
per row keep several projects' writes whole. Reading or writing it may fail; the harness then reports the "record" part
through health and carries on. Standard library only.
"""
from pathlib import Path
import sqlite3

import places

FILE = places.RECORD
BUSY_SECONDS = 10
SIZES = ("small", "medium", "large", "unknown")
SMALL = (3, 100)  # At most this many changed files and changed lines (added plus deleted) is small.
LARGE = (12, 600)  # More than this many files, or more than this many lines, is large; between is medium.
COUNTS = ("tasks", "merged", "dropped", "first_approved", "rounds", "blocked", "quiet", "quota")


def size(files, lines):
    """A PR's size from its changed files and its added plus deleted lines; "unknown" without both."""
    if not isinstance(files, int) or not isinstance(lines, int) or isinstance(files, bool) or isinstance(lines, bool):
        return "unknown"
    if files <= SMALL[0] and lines <= SMALL[1]:
        return "small"
    return "large" if files > LARGE[0] or lines > LARGE[1] else "medium"


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=BUSY_SECONDS)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS record (model TEXT NOT NULL, role TEXT NOT NULL, size TEXT NOT NULL, "
               + ", ".join(f"{name} INTEGER NOT NULL DEFAULT 0" for name in COUNTS)
               + ", PRIMARY KEY (model, role, size))")
    return db


def add(path, rows):
    """Add rows of counts: each (model, role, size, {count: n}). One transaction; a row's counts add to what is there."""
    rows = [row for row in rows if any(row[3].values())]
    if not rows:
        return
    db = connect(path)
    try:
        with db:
            for model, role, bucket, counts in rows:
                values = [int(counts.get(name, 0)) for name in COUNTS]
                db.execute(f"INSERT INTO record (model, role, size, {', '.join(COUNTS)}) VALUES (?, ?, ?, "
                           f"{', '.join('?' * len(COUNTS))}) ON CONFLICT(model, role, size) DO UPDATE SET "
                           + ", ".join(f"{name} = {name} + excluded.{name}" for name in COUNTS),
                           (model, role, bucket, *values))
    finally:
        db.close()


def counts(path, role):
    """{model: {size: {count: n}}} for the role; {} when there is no record yet."""
    if not Path(path).exists():
        return {}
    db = connect(path)
    try:
        found = {}
        for model, bucket, *values in db.execute(f"SELECT model, size, {', '.join(COUNTS)} FROM record WHERE role=?",
                                                 (role,)):
            found.setdefault(model, {})[bucket] = dict(zip(COUNTS, values))
        return found
    finally:
        db.close()


def phrase(bucket, n, role):
    """One size's counts in a few words, such as "small: 12 tasks, 10 approved first time, 1.2 review rounds on
    average, 1 blocked"."""
    parts = []
    if n["tasks"]:
        parts.append(f"{n['tasks']} task{'s' if n['tasks'] != 1 else ''}")
        if role in ("implementer", "devops", "docs"):
            parts += [f"{n['first_approved']} approved first time", f"{n['rounds'] / n['tasks']:.1f} review rounds on average"]
        else:
            parts.append(f"{n['rounds'] / n['tasks']:.1f} change requests on average")
    parts += [f"{n[key]} {words}" for key, words in (("dropped", "dropped"), ("blocked", "blocked"),
                                                      ("quiet", "replaced for being quiet"), ("quota", "stopped at a quota"))
              if n[key]]
    return f"{bucket}: {', '.join(parts)}" if parts else ""


def lines(path, role):
    """{model: its record for the role, one phrase per size with data}, for Jev's options."""
    return {model: "; ".join(p for p in (phrase(b, sizes[b], role) for b in SIZES if b in sizes) if p)
            for model, sizes in counts(path, role).items()}
