"""Each bound project's own copy of the workflow: its own code and its own data, apart from every other project's.

A home is a folder under the data folder's state/homes/: a copy of the workflow's code (no tests) with its own state/
(its database, logs, health and watcher) and a home.json naming its project. The project's hooks, workers and watcher
run its home's copy, so nothing one project's sessions run or change reaches another. Setup's `init` makes the home;
`t3-workflow upgrade` moves a binding of an older version into one and brings each home's code to the installed version.
Standard library only: the hooks use it on every call.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

import places
import store

ROOT = places.STATE / "homes"
INFO = "home.json"
CODE = ("install.py", "SKILL.md", "README.md", "LICENSE", "scripts/*.py", "references/*.md", "agents/*.yaml", "assets/*.json")
TABLES = (("projects", "id"), ("audit", "project"), ("events", "project"), ("runs", "project"), ("usage", "project"),
          ("deliveries", "project"), ("retired_bindings", "id"))  # A binding's rows; the rest are not one project's.


class Failed(Exception):
    pass


def root_for(db):
    """The homes beside a shared database."""
    return Path(db).resolve().parent / "homes"


def code_files(code):
    found = set()
    for pattern in CODE:
        found.update(p.relative_to(code) for p in Path(code).glob(pattern) if p.is_file() and not p.name.startswith("test_"))
    return sorted(found)


def fingerprint(code):
    digest = hashlib.sha256()
    for rel in code_files(code):
        digest.update(rel.as_posix().encode() + b"\0" + (Path(code) / rel).read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def copy_code(code, home):
    """The home's code becomes `code`'s: files added or replaced, and code files `code` no longer has removed.
    Its state/ is never touched."""
    wanted = set(code_files(code))
    for rel in code_files(home):
        if rel not in wanted:
            (Path(home) / rel).unlink()
    for rel in wanted:
        target = Path(home) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")  # A hook starting meanwhile reads whole files.
        shutil.copy2(Path(code) / rel, temporary)
        os.replace(temporary, target)


def info(home):
    return json.loads((Path(home) / INFO).read_text(encoding="utf-8"))


def write_info(home, data):
    temporary = Path(home) / f"{INFO}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temporary, Path(home) / INFO)


def listing(root=ROOT):
    """(home, its home.json) for every home, by folder name. A folder without a whole home.json is no home: one being
    made, or one an interruption left behind."""
    found = []
    for home in sorted(Path(root).iterdir()) if Path(root).is_dir() else ():
        try:
            data = info(home)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k] for k in ("project", "workspace")):
            found.append((home, data))
    return found


def database(home):
    return Path(home) / "state" / "harness.sqlite3"


def for_project(project, root=ROOT):
    return next((home for home, data in listing(root) if data.get("project") == project), None)


def create(project, workspace, root=ROOT, code=places.CODE, shared=places.BASE, publish=True):
    """A new home for the project: the code copied and its own empty state/. home.json, which makes it the project's
    home for the hooks and the harness, is written now, or by publish() once its data is in place."""
    if for_project(project, root):
        raise Failed(f"Project {project} already has its own workflow folder")
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(workspace).name).strip("-")[:40] or "project"
    home = Path(root) / f"{name}-{re.sub(r'[^A-Za-z0-9]', '', project)[:8] or 'x'}"
    base, suffix = home.name, 1
    while home.exists():
        if not (home / INFO).exists():  # Left unfinished by an interruption: its rows are still in the shared data.
            shutil.rmtree(home)
            break
        suffix += 1
        home = home.with_name(f"{base}-{suffix}")
    (home / "state").mkdir(parents=True)
    copy_code(code, home)
    if publish:
        write_info(home, {"project": project, "workspace": str(workspace), "shared": str(shared), "code": fingerprint(code)})
    return home


def upgrade(home, code=places.CODE):
    """Bring the home's code to `code`'s version; True when it changed."""
    data = info(home)
    version = fingerprint(code)
    if data.get("code") == version and fingerprint(home) == version:
        return False
    copy_code(code, home)
    write_info(home, {**data, "code": version})
    return True


def worktrees(workspace):
    """The bound folder plus its git worktrees (T3 moves a session into one), read from .git without running git."""
    roots = [Path(workspace)]
    for gitdir in (Path(workspace) / ".git" / "worktrees").glob("*/gitdir"):
        try:
            roots.append(Path(gitdir.read_text(encoding="utf-8").strip()).parent)
        except OSError:
            pass
    return roots


def inside(path, root):
    here, there = (os.path.normcase(os.path.realpath(p)) for p in (path, root))
    return here == there or here.startswith(there.rstrip("\\/") + os.sep)


def find(cwd, root=ROOT):
    """(home, its home.json) of the project whose folder or worktree holds cwd, or None."""
    if not cwd:
        return None
    for home, data in listing(root):
        if any(inside(cwd, folder) for folder in worktrees(data["workspace"])):
            return home, data
    return None


def rows(home):
    """The home's (config, state) pairs, read-only."""
    if not database(home).exists():
        return []
    db = store.connect(database(home), readonly=True)
    try:
        return [(json.loads(c), json.loads(s)) for c, s in db.execute("SELECT config, state FROM projects")]
    finally:
        db.close()


def holder(worker_id, token, root=ROOT):
    """The home whose project has this worker with this token, or None."""
    for home, _ in listing(root):
        try:
            for _, state in rows(home):
                worker = state["workers"].get(worker_id)
                if worker and hmac.compare_digest(worker["token"], token):
                    return home
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            continue  # One unreadable home does not stop another project's workers.
    return None


def run(home, argv, stdin_text=None):
    """One harness call in the home's own copy; its JSON answer."""
    done = subprocess.run([sys.executable, "-B", str(Path(home) / "scripts" / "harness.py"), *argv], input=stdin_text,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"ok": False, "error": f"The project's own workflow did not answer: {(done.stderr or done.stdout).strip()[-400:]}"}


def move(db_path, project, root=ROOT, code=places.CODE, shared=places.BASE):
    """Move a binding out of the shared database into a new home: its rows are copied, compared and only then deleted,
    all while the shared database is locked for writing, so no change in between is lost. Its check logs and the
    orchestrator's docs record follow. The home becomes the project's (home.json) only once its copy is complete, and the
    shared rows go in the same breath: an interruption before leaves them shared, and one after leaves a stale shared
    copy that the next move removes. Returns the home."""
    state_dir = Path(db_path).resolve().parent
    source = store.connect(db_path)
    home = None
    try:
        source.execute("BEGIN IMMEDIATE")
        row = source.execute("SELECT config FROM projects WHERE id=?", (project,)).fetchone()
        if not row:
            raise Failed(f"Project {project} has no binding in {db_path}")
        config = json.loads(row[0])
        earlier = for_project(project, root)
        if earlier:
            if not any(c.get("projectId") == project for c, _ in rows(earlier)):
                raise Failed(f"{earlier} is named as this project's own folder but holds no binding; nothing was moved")
            for table, key in TABLES:  # Moved before; its home has served it since.
                source.execute(f"DELETE FROM {table} WHERE {key}=?", (project,))
            source.commit()
            return earlier
        home = create(project, config["workspace"], root, code, shared, publish=False)
        target = store.connect(database(home))
        try:
            with target:
                for table, key in TABLES:
                    found = source.execute(f"SELECT * FROM {table} WHERE {key}=?", (project,))
                    columns = [d[0] for d in found.description]
                    values = found.fetchall()
                    if values:
                        target.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                                           values)
                    if values and table == "audit":  # Its trigger stamps each insert with now; keep the original times.
                        stamp, key_at = columns.index("created_at"), columns.index("id")
                        target.executemany("UPDATE audit SET created_at=? WHERE id=?", [(v[stamp], v[key_at]) for v in values])
            for table, key in TABLES:
                mine, theirs = (sorted(db.execute(f"SELECT * FROM {table} WHERE {key}=?", (project,)).fetchall(), key=repr)
                                for db in (source, target))
                if mine != theirs:
                    raise Failed(f"The copy of {table} does not match; nothing was moved")
        finally:
            target.close()
        record = f"{hashlib.sha256(config['orchestrator'].encode()).hexdigest()[:32]}.json"
        if (state_dir / "docs-seen" / record).exists():
            (home / "state" / "docs-seen").mkdir(parents=True, exist_ok=True)
            shutil.copy2(state_dir / "docs-seen" / record, home / "state" / "docs-seen" / record)
        for logs in artifacts(state_dir, project):
            shutil.copytree(logs, home / "state" / "harness-artifacts" / places.segment(project), dirs_exist_ok=True)
        write_info(home, {"project": project, "workspace": config["workspace"], "shared": str(shared), "code": fingerprint(code)})
        for table, key in TABLES:
            source.execute(f"DELETE FROM {table} WHERE {key}=?", (project,))
        source.commit()
    except BaseException:
        if source.in_transaction:
            source.rollback()
        if home:
            shutil.rmtree(home, ignore_errors=True)
        raise
    finally:
        source.close()
    for logs in artifacts(state_dir, project):
        shutil.rmtree(logs, ignore_errors=True)
    (state_dir / "docs-seen" / record).unlink(missing_ok=True)
    return home


def artifacts(state_dir, project):
    """The project's check-log folders in a shared data folder: under its safe name, and under its id as an older
    version named it, where the system allows that name."""
    found = []
    for name in dict.fromkeys((places.segment(project), project)):
        try:
            if (Path(state_dir) / "harness-artifacts" / name).is_dir():
                found.append(Path(state_dir) / "harness-artifacts" / name)
        except (OSError, ValueError):  # A name this system cannot hold was never written here.
            pass
    return found
