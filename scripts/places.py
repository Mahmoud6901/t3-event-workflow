"""Where the workflow keeps its data (bindings, logs, backups and keys).

A clone that already has a state/ folder keeps its data there: its owner's bindings are never moved. An installed
copy has none, so it keeps its data in ~/.t3-workflow and never writes into its own package. Standard library
only: the hooks import it on every call.
"""
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent


def base(code=CODE, home=None):
    if (Path(code) / "state").is_dir():
        return Path(code)
    return Path(home or Path.home()) / ".t3-workflow"


BASE = base()
STATE = BASE / "state"
SECRETS = BASE / "secrets"
