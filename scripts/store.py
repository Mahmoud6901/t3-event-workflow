"""The harness database: one connect() for every process that opens it, and its schema versions.

The harness CLI, both hooks, capacity recovery and the watcher open the same SQLite file. Every connection
waits BUSY_SECONDS for a lock, and the file runs in WAL mode, so a reader never blocks a writer and two
writers wait for each other instead of failing with "database is locked".

The schema version is PRAGMA user_version. Each migration runs in one transaction; a database newer than
this code refuses to open. Standard library only, so the hooks can import it without loading the harness.
"""
import json
from pathlib import Path
import sqlite3

BUSY_SECONDS = 10
TOO_NEW = "This harness is older than its database; update the install."
CHUNK = 500  # Event ids per IN (...) query, well under SQLite's variable limit.


def packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def live_events(state):
    """The events a binding still works with, kept in its state: every event not done, every event of a working run
    or of the current queue, and the current event of a run that is not done (a stopped one may resume). The rest
    live only in the events table."""
    runs = [r for r in [state.get("run"), *(state.get("runs") or {}).values()] if r]
    working = {r["id"] for r in runs if r.get("status") == "working"}
    current = {r.get("eventId") for r in runs if r.get("status") != "done"}
    queue = (state.get("queue") or {}).get("id")
    return {i: e for i, e in state["events"].items()
            if e.get("claim") != "done" or e.get("featureId") in working or (queue and e.get("featureId") == queue) or i in current}


def save_events(db, project, events):
    """Insert or update the rows of these events whose content changed; returns the ids that had no row yet."""
    ids, known = list(events), {}
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        known.update(db.execute(f"SELECT event_id, data FROM events WHERE event_id IN ({','.join('?' * len(chunk))})", chunk))
    changed = [(i, project, e.get("trackId"), e.get("featureId"), e.get("claim"), data)
               for i, e in events.items() if known.get(i) != (data := packed(e))]
    db.executemany("INSERT INTO events (event_id, project, track, feature, claim, data) VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(event_id) DO UPDATE SET project=excluded.project, track=excluded.track, "
                   "feature=excluded.feature, claim=excluded.claim, data=excluded.data", changed)
    return [i for i in events if i not in known]


def v1(db):
    """The tables Harness.__init__ created before schema versions; an existing v1 database already has them."""
    db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, project TEXT, operation TEXT, version INTEGER, detail TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, project TEXT, state TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, project TEXT, feature TEXT, role TEXT, model TEXT, data TEXT)")
    # Timestamp every audit row so phase durations can be measured. Inserts never name this column.
    if "created_at" not in {row[1] for row in db.execute("PRAGMA table_info(audit)")}:
        db.execute("ALTER TABLE audit ADD COLUMN created_at TEXT")
    db.execute("CREATE TRIGGER IF NOT EXISTS audit_created_at AFTER INSERT ON audit BEGIN "
               "UPDATE audit SET created_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=NEW.id; END")
    # Written by t3_hooks.py when a recipient's receive hook validates a delivered event.
    db.execute("CREATE TABLE IF NOT EXISTS hook_receipts (event_id TEXT, thread_id TEXT, message_id TEXT, runtime TEXT, "
               "created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), PRIMARY KEY (event_id, thread_id))")
    # Provider usage limits the capacity watcher saw: a model is skipped until its reset time.
    db.execute("CREATE TABLE IF NOT EXISTS model_limits (model TEXT PRIMARY KEY, provider TEXT, reset_at TEXT NOT NULL, "
               "run_id TEXT, thread_id TEXT, created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")


def v2(db):
    """WAL itself is set before the transaction (SQLite cannot change the journal inside one); this records it."""


def v3(db):
    """Every event as a row. A binding's state keeps only its live events (live_events); before this, each operation
    rewrote every event ever emitted. The migration moves each binding's existing events into rows."""
    db.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, project TEXT NOT NULL, track TEXT, feature TEXT, claim TEXT, "
               "data TEXT NOT NULL, created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
    db.execute("CREATE INDEX events_project_claim ON events (project, claim)")
    for project, raw in db.execute("SELECT id, state FROM projects").fetchall():
        state = json.loads(raw)
        save_events(db, project, state.get("events") or {})
        if "events" in state:
            state["events"] = live_events(state)
        db.execute("UPDATE projects SET state=? WHERE id=?", (packed(state), project))


def v4(db):
    """Harness delivery (delivery.py): one row per event the watcher sends. status is waiting, sending, sent, failed or
    withdrawn; sender says who holds a `sending` row (the watcher, or an agent's send_ready fallback); alerted names
    the CONFIGURE_NEEDED notice already raised for it. Rows to one session go out in rowid order."""
    db.execute("CREATE TABLE deliveries (event_id TEXT PRIMARY KEY, project TEXT NOT NULL, track TEXT, thread_id TEXT NOT NULL, "
               "status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT, last_error TEXT, message_id TEXT, run_id TEXT, "
               "sender TEXT, alerted TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.execute("CREATE INDEX deliveries_status_next ON deliveries (status, next_at)")


def v5(db):
    """The delegated PR workflow: every binding of the old
    thread-per-role workflow moves, intact, to retired_bindings, and projects holds only new bindings. The other
    tables stay as history; only audit is written from now on."""
    db.execute("CREATE TABLE retired_bindings (id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL, "
               "retired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
    db.execute("INSERT INTO retired_bindings (id, config, state) SELECT id, config, state FROM projects")
    db.execute("DELETE FROM projects")


MIGRATIONS = {1: v1, 2: v2, 3: v3, 4: v4, 5: v5}
BEFORE = {2: "PRAGMA journal_mode=WAL"}  # Statements a migration needs outside its transaction; each is idempotent.
SCHEMA_VERSION = max(MIGRATIONS)


def version_of(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def migrate(db):
    """Bring the database to SCHEMA_VERSION, one transaction per version. Two processes may race here: each takes
    the write lock and re-reads the version before it migrates."""
    for target in range(version_of(db) + 1, SCHEMA_VERSION + 1):
        if target in BEFORE:
            db.execute(BEFORE[target])
        db.execute("BEGIN IMMEDIATE")
        try:
            if version_of(db) < target:
                MIGRATIONS[target](db)
                db.execute(f"PRAGMA user_version={target}")
            db.commit()
        finally:
            if db.in_transaction:
                db.rollback()


def connect(path, readonly=False):
    """A connection to the harness database. A writer's connection also migrates it to the latest version."""
    path = Path(path).resolve()
    if readonly:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=BUSY_SECONDS)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=BUSY_SECONDS)
    try:
        if version_of(db) > SCHEMA_VERSION:
            raise ValueError(TOO_NEW)
        if not readonly:
            migrate(db)
    except BaseException:
        db.close()
        raise
    return db
