"""Is the workflow watcher (watch.py) alive, and start it if not. Its Startup entry runs only at logon, so
hooks of bound sessions and the orchestrator's status use this. Standard library only and fast:
hooks call it on every tool call. A watcher started twice exits by its own lock.
"""
import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().with_name("watch.py")
LOCK = "watch.lock"  # Not the old capacity watcher's lock, so a leftover one cannot keep this from starting.


def hold(state_dir):
    """The watcher's lock, held for as long as the returned handle stays open; None when another holds it."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    handle = open(Path(state_dir) / LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def running(state_dir):
    handle = hold(state_dir)
    if handle is None:
        return True
    handle.close()  # Closing releases the lock, on Windows too.
    return False


def start(script=SCRIPT):
    """A detached, windowless watcher that outlives the hook and the session that started it."""
    command = [str(Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else sys.executable), str(script)]
    if os.name == "nt" and not Path(command[0]).exists():
        command[0] = sys.executable
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(Path(script).parent.parent)}
    if os.name != "nt":
        subprocess.Popen(command, start_new_session=True, **options)
        return
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options)
    except OSError:  # The session's job may forbid leaving it; a later hook restarts the watcher if the job ends it.
        subprocess.Popen(command, creationflags=flags, **options)


def ensure(state_dir, script=SCRIPT):
    """Start the watcher (`script`, of the copy whose data is state_dir) when nothing holds its lock; True when one was
    started. T3_WORKFLOW_NO_WATCHER=1 starts none: for tests, and for running without a background process."""
    if os.environ.get("T3_WORKFLOW_NO_WATCHER") == "1" or running(state_dir):
        return False
    start(script)
    return True
