"""The t3 workflow's command line. Standard library only; every call is short-lived and prints one JSON result.

The orchestrator (its hook rewrites `t3-workflow '<JSON>'` to the --op-base64 form, with its own identity):
    python harness.py [--db DB] [--ascii] [--input FILE | --op-base64 B64]   JSON operation on stdin, in FILE, or as base64
A worker (the command its brief gives):
    python harness.py [--db DB] worker <command> --worker ID --token TOKEN [arguments]
A refused or failed call prints {"ok": false, "error": ...} and exits 1.
"""
import argparse
import base64
import binascii
import json
from pathlib import Path
import sys

import flow
import github

DEFAULT_DB = flow.DEFAULT_DB


def parser():
    top = argparse.ArgumentParser(prog="harness.py")
    top.add_argument("--db", default=str(DEFAULT_DB))
    top.add_argument("--input")
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


def run(argv, stdin_text=None, hub=None, jev=None):
    """One call: the parsed result, {"ok": true, ...} or {"ok": false, "error": ...}."""
    try:
        args = parser().parse_args(argv)
    except SystemExit:
        return {"ok": False, "error": "Invalid arguments; see harness.py --help"}
    engine = None
    try:
        engine = flow.Flow(Path(args.db), hub=hub, jev=jev)
        if args.mode == "worker":
            result = engine.worker(args.command, {k: v for k, v in vars(args).items()
                                                  if k not in {"db", "input", "op_base64", "ascii", "mode"}})
        else:
            raw = (base64.b64decode(args.op_base64, validate=True).decode("utf-8") if args.op_base64
                   else Path(args.input).read_text(encoding="utf-8") if args.input else stdin_text)
            request = json.loads(raw or "")
            if not isinstance(request, dict):
                raise flow.Refused("The operation must be one JSON object")
            result = engine.orchestrator(request)
        return {"ok": True, **result}
    except (flow.Refused, github.Failed, ValueError, binascii.Error) as error:
        return {"ok": False, "error": str(error)}
    finally:
        if engine:
            engine.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    stdin_text = None
    if argv is None:
        argv = sys.argv[1:]
    if "worker" not in argv and "--input" not in argv and "--op-base64" not in argv:
        stdin_text = sys.stdin.read()
    result = run(argv, stdin_text)
    print(json.dumps(result, ensure_ascii="--ascii" in argv, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
