"""Where the workflow keeps its data (bindings, logs, backups and keys).

A clone that already has a state/ folder keeps its data there: its owner's bindings are never moved. An installed
copy has none, so it keeps its data in ~/.t3-workflow and never writes into its own package. A project's own copy
(its home, see homes.py) has its own state/ and a home.json naming the shared install, whose secrets it uses. Standard
library only: the hooks import it on every call.
"""
import json
import os
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent


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
