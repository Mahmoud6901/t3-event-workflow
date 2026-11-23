"""Where the workflow keeps its data (bindings, logs, backups and keys).

A clone that already has a state/ folder keeps its data there: its owner's bindings are never moved. An installed
copy has none, so it keeps its data in ~/.t3-workflow and never writes into its own package. A project's own copy
(its home, see homes.py) has its own state/ and a home.json naming the shared install, whose secrets it uses. Standard
library only: the hooks import it on every call.
"""
import hashlib
import json
import os
from pathlib import Path
import re

CODE = Path(__file__).resolve().parent.parent
DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def segment(value):
    """A file or folder name every system accepts, for a value from outside such as a T3 project id; use it for every
    path part such a value makes. A value of letters, digits, '.', '_' and '-' stays as it is. Any other (Windows refuses
    : < > " / \\ | ? *, a trailing dot or space, and device names such as CON or LPT9.txt) keeps its letters, digits,
    '_' and '-', and gains a short hash of itself, so two values never share a name."""
    text = str(value)
    if (re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,79}", text) and not text.endswith(".")
            and text.split(".")[0].upper() not in DEVICES):
        return text
    readable = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:60] or "x"
    return f"{readable}-{hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()[:12]}"


def base(code=CODE, home=None):
    if (Path(code) / "state").is_dir():
        return Path(code)
    return Path(home or Path.home()) / ".t3-workflow"


def home_of(code=CODE):
    """A project's own copy's home.json (see homes.py), or None for the shared install."""
    try:
        return json.loads((Path(code) / "home.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


BASE = base()
STATE = BASE / "state"
HOME = home_of()
SHARED = Path(HOME["shared"]) if HOME else BASE  # The shared install's data folder: the Jev key is kept there.
SECRETS = SHARED / "secrets"
T3_HOME = Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3")
