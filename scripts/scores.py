"""The board scores Jev sees beside each approved model, kept in assets/scores.json.

Each board in BOARDS has its maintainers run every model themselves on tasks they do not publish; today that is
LiveBench 2.0 alone. Refresh from the repository with `python scripts/scores.py` when a model is approved, before each
release, and when a board adds results (LiveBench about every 12 days).

LiveBench's options: Jev sees its Coding and Long Agentic suites until `--suites` names others, as in
`python scripts/scores.py --suites coding,long_agentic,browser`, a choice later refreshes keep. `--release YYYYMMDD`
reads one release, for when the newest cannot be found on its site. Its data has no stated licence; robots.txt allows
every agent.

A refresh fails, and writes nothing, when a board cannot be read or no board scores an approved model.
"""

from __future__ import annotations

import argparse
import csv
import datetime
from email.utils import parsedate_to_datetime
import io
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin
from urllib.request import Request, urlopen

FILE = Path(__file__).resolve().parent.parent / "assets" / "scores.json"
AGENT = "t3-event-workflow scores refresh"
LIVEBENCH = {"label": "LiveBench 2.0", "url": "https://livebench.ai/", "scale": "0 to 100"}
# Each suite's column in the board's table.csv, its name on the board, and what it measures, in the board's own terms.
SUITES = {
    "coding": ("Coding", "real merged-PR bug fixes on a frozen codebase, a fix that breaks other behaviour scoring 0"),
    "long_agentic": ("Long Agentic", "long-running work: business apps extended over several turns of changing "
                     "requirements, operations on a codebase over simulated months, and extended coding and optimisation"),
    "browser": ("Browser Use", "multi-step tasks in working web apps, done through a real browser"),
    "design": ("Design", "design and layout of presentations, PDFs and documents, checked visually and structurally"),
    "research": ("Research", "open-ended research reports graded against a rubric"),
    "data_analysis": ("Data Analysis", "short data-analysis questions on small tables, with a single correct answer"),
    "reasoning": ("Reasoning", "short logic, spatial and theory-of-mind questions, with a single correct answer"),
}
DEFAULT_SUITES = ("coding", "long_agentic")
NAMES = {  # Per board, each approved model's name there.
    "livebench": {
        "gpt-6.2-sol": "gpt-6.2-sol",
        "gpt-6.2-astra": "gpt-6.2-astra",
        "gemini-4-neon": "gemini-4-neon",
        "claude-sonnet-5-5": "claude-sonnet-5.5",
        "claude-opus-5-5": "claude-opus-5.5",
    },
}
BORROWED = {}  # Per board, approved models it does not list, scored as the stand-in NAMES gives them, and marked so.


def parse(text):
    return list(csv.DictReader(io.StringIO(text)))


def build(rows, names, suites, borrowed=()):
    """Each model's scores in the chosen suites. A suite it was not scored on (n/a or blank) is left out, and so is a
    model with none of them."""
    columns = set(rows[0]) if rows else set()
    for suite in suites:
        if suite not in columns:
            raise ValueError(f"LiveBench's table has no {suite} column")
    by_name = {row["model"]: row for row in rows}
    found = {}
    for model, name in names.items():
        entry = {}
        for suite in suites:
            try:
                entry[suite] = round(float((by_name.get(name) or {}).get(suite) or ""), 1)
            except ValueError:
                continue
        if not entry:
            continue
        if model in borrowed:
            entry["borrowedFrom"] = name
        found[model] = entry
    return found


def releases(bundle):
    """The board's releases, oldest first. The site keeps them only in its script bundle, as one list of dates."""
    found = sorted({day for group in re.findall(r'\[((?:"\d{8}",?)+)\]', bundle) for day in re.findall(r"\d{8}", group)})
    if not found:
        raise ValueError("Cannot find LiveBench's releases on its site: name one with --release YYYYMMDD")
    return found


def _get(url, attempts=3):
    """The response's text and its Last-Modified header."""
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={"User-Agent": AGENT}), timeout=30) as response:
                return response.read().decode("utf-8"), response.headers.get("Last-Modified")
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(2)


def fetch(release=None):
    """(release, table rows, the date its files were last changed) for the given release, else the newest."""
    if release is None:
        manifest = json.loads(_get(urljoin(LIVEBENCH["url"], "asset-manifest.json"))[0])
        release = releases(_get(urljoin(LIVEBENCH["url"], manifest["files"]["main.js"]))[0])[-1]
    text, modified = _get(urljoin(LIVEBENCH["url"], f"data/{release}/table.csv"))
    updated = parsedate_to_datetime(modified).date().isoformat() if modified else ""
    return release, parse(text), updated


def _suites(text):
    chosen = list(dict.fromkeys(name.strip() for name in text.split(",") if name.strip()))
    unknown = [name for name in chosen if name not in SUITES]
    if unknown or not chosen:
        raise argparse.ArgumentTypeError(f"choose suites from {', '.join(SUITES)}" + (f", not {', '.join(unknown)}" if unknown else ""))
    return chosen


def _release(text):
    if not re.fullmatch(r"\d{8}", text):
        raise argparse.ArgumentTypeError("a release is its date, YYYYMMDD")
    return text


def _kept():
    """The LiveBench suites the shipped file already has, so a plain refresh keeps the user's choice."""
    try:
        board = (json.loads(FILE.read_text(encoding="utf-8")).get("boards") or {}).get("livebench") or {}
        return [name for name in board.get("suites") or {} if name in SUITES]
    except (OSError, ValueError, AttributeError):
        return []


def read_livebench(args):
    chosen = args.suites or _kept() or list(DEFAULT_SUITES)
    release, rows, updated = fetch(args.release)
    return {**LIVEBENCH, "release": release, "updated": updated,
            "suites": {name: {"name": SUITES[name][0], "measures": SUITES[name][1]} for name in chosen},
            "models": build(rows, NAMES["livebench"], chosen, BORROWED.get("livebench", ()))}


# The boards Jev's scores come from, each read by its own function into {"label", "url", "scale", "release", "updated",
# "suites": {key: {"name", "measures"}}, "models": {model: {suite key: score}}}. A board joins only when its
# maintainers run every model themselves on tasks they do not publish, and its data can be read without a key under
# terms that allow it. To add one, write its reader, give NAMES its names for the approved models, and list it here.
BOARDS = {"livebench": read_livebench}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scores.py", description="Refresh the board scores Jev sees.")
    parser.add_argument("--suites", type=_suites, help=f"comma-separated LiveBench suites Jev sees, from {', '.join(SUITES)}; "
                        "without it a refresh keeps the current ones (at first coding,long_agentic)")
    parser.add_argument("--release", type=_release, help="the LiveBench release to read, YYYYMMDD; without it the newest")
    args = parser.parse_args(argv)
    boards = {key: read(args) for key, read in BOARDS.items()}
    approved = dict.fromkeys(model for names in NAMES.values() for model in names)
    unscored = [model for model in approved if not any(model in board["models"] for board in boards.values())]
    if unscored:
        raise ValueError(f"No board scores {', '.join(unscored)}: check their names in scores.NAMES")
    data = {"fetched": datetime.date.today().isoformat(), "boards": boards}
    FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    for board in boards.values():
        print(f"{board['label']}, release {board['release']}, files updated {board['updated'] or 'on an unknown date'}:")
        for model, entry in board["models"].items():
            shown = ", ".join(f"{suite['name']} {entry[key]}" for key, suite in board["suites"].items() if key in entry)
            print(f"  {model:20} {shown}" + (f" (as {entry['borrowedFrom']})" if "borrowedFrom" in entry else ""))
    print(f"Wrote {FILE.name}.")


if __name__ == "__main__":
    sys.exit(main())
