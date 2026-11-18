"""The t3 workflow's command line. Standard library only; every call is short-lived and prints one JSON result.

The orchestrator (its hook saves the operation of `t3-workflow '<JSON>'`, with its own identity, and rewrites the
command to the --request form: a short command whatever the operation's size, run once):
    python harness.py [--db DB] [--ascii] [--request TOKEN | --input FILE | --op-base64 B64]
    The JSON operation the hook saved for TOKEN, on stdin, in FILE, or as base64
A worker (the command its brief gives):
    python harness.py [--db DB] worker <command> --worker ID --token TOKEN [arguments]
A refused or failed call prints {"ok": false, "error": ...} and exits 1.

In the shared install, a project with its own copy (homes.py) is served by that copy: setup's `init` makes it, and a
worker whose brief names this file is passed on to the copy that holds its id and token.
"""
import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import time

import flow
import github
import homes
import places

DEFAULT_DB = flow.DEFAULT_DB
REQUESTS = "requests"  # Beside the database: operations the orchestrator's hook saved, each for one run.
UNRUN_SECONDS = 24 * 3600  # A saved operation no command ran in a day is dropped.


def save_request(db, request):
    """Keep an operation for one `--request <token>` run, and drop ones no command ran. Claude Code runs a command
    inside a script of capped size that a session's saved environment grows, so the command carries a token, not the
    operation. The fence keeps every session out of this folder."""
    folder = Path(db).parent / REQUESTS
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        try:
            if old.stat().st_mtime < time.time() - UNRUN_SECONDS:
                old.unlink()
        except OSError:
            pass
    token = secrets.token_hex(16)
    (folder / f"{token}.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return token


def take_request(db, token):
    """The operation saved for this token, claimed so a second run of the same command finds nothing."""
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise flow.Refused("Invalid request token")
    path = Path(db).parent / REQUESTS / f"{token}.json"
    claimed = path.with_suffix(".taken")
    try:
        os.replace(path, claimed)  # Atomic: of two runs at once, one claims it.
    except FileNotFoundError:
        raise flow.Refused("This workflow command ran already: run t3-workflow '<JSON>' again") from None
    try:
        return claimed.read_text(encoding="utf-8")
    finally:
        claimed.unlink(missing_ok=True)


def parser():
    top = argparse.ArgumentParser(prog="harness.py")
    top.add_argument("--db", default=str(DEFAULT_DB))
    top.add_argument("--input")
    top.add_argument("--request", help="The token of an operation the orchestrator's hook saved: the form it writes")
    top.add_argument("--op-base64", help="The operation as base64 JSON: the form the orchestrator's hook writes")
    top.add_argument("--ascii", action="store_true",
                     help="Escape non-ASCII in the answer, for PowerShell, which decodes it with the console code page")
    sub = top.add_subparsers(dest="mode")
    worker = sub.add_parser("worker")
    worker.add_argument("command", choices=["pr", "stuck", "review", "take", "update", "merge", "failed", "pipeline"])
    worker.add_argument("--worker", required=True)
    worker.add_argument("--token", required=True)
    worker.add_argument("--pr", type=int)
    worker.add_argument("--why")
    worker.add_argument("--verdict", choices=["approve", "changes"])
    worker.add_argument("--sha")
    worker.add_argument("--cause", choices=["task", "pipeline"])
    worker.add_argument("--detail")
    worker.add_argument("--path", help="The worker's checkout; the folder the command runs in by default")
    return top


def run(argv, stdin_text=None, hub=None, jev=None, homes_root=None):
    """One call: the parsed result, {"ok": true, ...} or {"ok": false, "error": ...}. `homes_root` is where projects'
    own copies are; the shared install's default database has them beside it."""
    try:
        args = parser().parse_args(argv)
    except SystemExit:
        return {"ok": False, "error": "Invalid arguments; see harness.py --help"}
    if homes_root is None and not places.HOME and Path(args.db).resolve() == Path(DEFAULT_DB).resolve():
        homes_root = homes.ROOT
    engine = None
    try:
        request = None
        if args.mode != "worker":
            raw = (take_request(args.db, args.request) if args.request
                   else base64.b64decode(args.op_base64, validate=True).decode("utf-8") if args.op_base64
                   else Path(args.input).read_text(encoding="utf-8") if args.input else stdin_text)
            request = json.loads(raw or "")
            if not isinstance(request, dict):
                raise flow.Refused("The operation must be one JSON object")
        if homes_root:
            passed = forward(args, request, Path(homes_root))
            if passed is not None:
                return passed
        engine = flow.Flow(Path(args.db), hub=hub, jev=jev)
        if args.mode == "worker":
            result = engine.worker(args.command, {k: v for k, v in vars(args).items()
                                                  if k not in {"db", "input", "request", "op_base64", "ascii", "mode"}})
        else:
            result = engine.orchestrator(request)
        return {"ok": True, **result}
    except (flow.Refused, github.Failed, homes.Failed, ValueError, binascii.Error) as error:
        return {"ok": False, "error": str(error)}
    finally:
        if engine:
            engine.close()


def forward(args, request, root):
    """The answer of the project's own copy when it has one (setup's `init` makes it), else None."""
    ascii_flag = ["--ascii"] if args.ascii else []
    if args.mode == "worker":
        home = homes.holder(args.worker, args.token, root)
        if not home:
            return None
        argv = [*ascii_flag, "worker", args.command]
        for key, value in vars(args).items():
            if key not in {"db", "input", "request", "op_base64", "ascii", "mode", "command"} and value is not None:
                argv.append(f"--{key.replace('_', '-')}={value}")  # One word, so a value starting with - stays a value.
        return homes.run(home, argv)
    project = request.get("projectId")
    home = homes.for_project(project, root) if isinstance(project, str) and project else None
    made = False
    if not home and request.get("op") == "init" and isinstance(request.get("workspace"), str) and request["workspace"]:
        home, made = homes.create(project, request["workspace"], root, shared=root.parent.parent), True
    if not home:
        return None
    encoded = base64.b64encode(json.dumps(request, ensure_ascii=False).encode("utf-8")).decode("ascii")
    result = homes.run(home, [*ascii_flag, "--op-base64", encoded])
    if made and not result.get("ok"):
        shutil.rmtree(home, ignore_errors=True)  # A refused setup leaves no folder behind.
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    stdin_text = None
    if argv is None:
        argv = sys.argv[1:]
    if not {"worker", "--input", "--request", "--op-base64"} & set(argv):
        stdin_text = sys.stdin.read()
    result = run(argv, stdin_text)
    print(json.dumps(result, ensure_ascii="--ascii" in argv, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
