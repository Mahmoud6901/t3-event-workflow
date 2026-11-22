"""The LiveBench 2.0 scores Jev sees beside each approved model, kept in assets/scores.json.

LiveBench's maintainers run every model themselves, on tasks they do not publish. Refresh from the repository with
`python scripts/scores.py` when a model is approved, before each release, and when LiveBench adds a release (about every
12 days). Jev sees Coding and Long Agentic until `--suites` names others, as in
`python scripts/scores.py --suites coding,long_agentic,browser`; later refreshes keep that choice. `--release YYYYMMDD`
reads one release, for when the newest cannot be found on the site. A refresh fails, and writes nothing, when the board
cannot be read or an approved model has none of the chosen scores. The data has no stated licence; robots.txt allows
every agent.
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
BOARD = {"label": "LiveBench 2.0", "url": "https://livebench.ai/"}
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
NAMES = {  # Each approved model's name on the board.
    "gpt-6.2-sol": "gpt-6.2-sol",
    "gpt-6.2-astra": "gpt-6.2-astra",
    "gemini-4-neon": "gemini-4-neon",
    "claude-sonnet-5-5": "claude-sonnet-5.5",
    "claude-opus-5-5": "claude-opus-5.5",
}
BORROWED = set()  # Approved models the board does not list, scored as the stand-in NAMES gives them, and marked so.


def parse(text):
    return list(csv.DictReader(io.StringIO(text)))


def build(rows, names, suites, borrowed=()):
    """Each model's scores in the chosen suites. A suite it was not scored on (n/a or blank) is left out; a model with
    none of them raises ValueError."""
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
            raise ValueError(f"LiveBench has none of the chosen scores for {model}: check its name in scores.NAMES")
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
        manifest = json.loads(_get(urljoin(BOARD["url"], "asset-manifest.json"))[0])
        release = releases(_get(urljoin(BOARD["url"], manifest["files"]["main.js"]))[0])[-1]
    text, modified = _get(urljoin(BOARD["url"], f"data/{release}/table.csv"))
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
    """The suites the shipped file already has, so a plain refresh keeps the user's choice."""
    try:
        return [name for name in json.loads(FILE.read_text(encoding="utf-8")).get("suites") or {} if name in SUITES]
    except (OSError, ValueError, AttributeError):
        return []


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scores.py", description="Refresh the LiveBench 2.0 scores Jev sees.")
    parser.add_argument("--suites", type=_suites, help=f"comma-separated suites Jev sees, from {', '.join(SUITES)}; "
                        "without it a refresh keeps the current ones (at first coding,long_agentic)")
    parser.add_argument("--release", type=_release, help="the release to read, YYYYMMDD; without it the newest")
    args = parser.parse_args(argv)
    chosen = args.suites or _kept() or list(DEFAULT_SUITES)
    release, rows, updated = fetch(args.release)
    models = build(rows, NAMES, chosen, BORROWED)
    data = {"fetched": datetime.date.today().isoformat(),
            "source": {**BOARD, "release": release, "updated": updated},
            "suites": {name: {"name": SUITES[name][0], "measures": SUITES[name][1]} for name in chosen},
            "models": models}
    FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    for model, entry in models.items():
        shown = ", ".join(f"{SUITES[name][0]} {entry[name]}" for name in chosen if name in entry)
        print(f"{model:20} {shown}" + (f" (as {entry['borrowedFrom']})" if "borrowedFrom" in entry else ""))
    print(f"Wrote {FILE.name}: {BOARD['label']} release {release}, files updated {updated or 'on an unknown date'}.")


if __name__ == "__main__":
    sys.exit(main())
