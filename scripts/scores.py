"""The public coding scores Jev sees beside each approved model, kept in assets/scores.json.

Refresh them from the repository with `python scripts/scores.py` when a model is approved and before each release.
Both boards are public and need no key: Terminal-Bench 4.1 (Apache-2.0, run by its maintainers) and LMArena WebDev
(CC BY 4.0, from people's votes). A refresh fails, and writes nothing, when either board cannot be read or an approved
model is on neither.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

FILE = Path(__file__).resolve().parent.parent / "assets" / "scores.json"
AGENT = "t3-event-workflow scores refresh"
TERMINAL_BENCH = {"label": "Terminal-Bench 4.1", "url": "https://www.tbench.ai/",
                  "api": "https://api.harborframework.com/functions/v1/leaderboard-read",
                  "board": {"package": "terminal-bench/terminal-bench", "name": "4-1-0"}}
WEBDEV = {"label": "LMArena WebDev", "url": "https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset",
          "api": "https://datasets-server.huggingface.co/rows?dataset=lmarena-ai/leaderboard-dataset&config=webdev&split=latest"}
# Each approved model's name on each board. For a model no board lists, name a close stand-in and mark it borrowedFrom.
NAMES = {
    "gpt-6.2-sol": {"terminalBench": "GPT-6.2 Sol", "webdev": "GPT-6.2 Sol"},
    "gpt-6.2-astra": {"terminalBench": "GPT-6.2 Astra", "webdev": "GPT-6.2 Astra"},
    "gemini-4-neon": {"terminalBench": "Gemini 4 Neon", "webdev": "Gemini 4 Neon"},
    "claude-sonnet-5-5": {"terminalBench": "Sonnet 5.5", "webdev": "Claude Sonnet 5.5"},
    "claude-opus-5-5": {"terminalBench": "Opus 5.5", "webdev": "Claude Opus 5.5"},
}


def build(terminal_bench_rows, webdev_rows, names):
    """Each model's best Terminal-Bench accuracy (any effort) and its rating in LMArena's newest overall WebDev
    ranking (its best effort variant, named "<name> (<effort>)"). Raises ValueError for a model on neither board."""
    webdev_rows = [r for r in webdev_rows if r.get("category") == "webdev"]
    newest = max((r["leaderboard_publish_date"] for r in webdev_rows), default=None)
    found = {}
    for model, name in names.items():
        solved = [float(r["metrics"]["accuracy"]) for r in terminal_bench_rows
                  if r["metadata"]["model_display"]["label"] == name["terminalBench"]]
        rated = [float(r["rating"]) for r in webdev_rows if r["leaderboard_publish_date"] == newest
                 and (r["model_name"] == name["webdev"] or r["model_name"].startswith(name["webdev"] + " ("))]
        entry = {}
        if solved:
            entry["terminalBench"] = round(max(solved), 2)
        if rated:
            entry["webdev"] = round(max(rated), 1)
        if not entry:
            raise ValueError(f"Neither board lists {model}: check its names in scores.NAMES")
        if name.get("borrowedFrom"):
            entry["borrowedFrom"] = name["borrowedFrom"]
        found[model] = entry
    return found


def _read(request, attempts=3):
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except OSError:  # The Hugging Face rows API answers 502 now and then; a retry works.
            if attempt == attempts - 1:
                raise
            time.sleep(2)


def fetch_terminal_bench():
    rows, page = [], 1
    while True:
        body = json.dumps({**TERMINAL_BENCH["board"], "page": page, "page_size": 100}).encode()
        data = _read(Request(TERMINAL_BENCH["api"], data=body, method="POST",
                             headers={"Content-Type": "application/json", "User-Agent": AGENT}))
        rows += data["rows"]
        if page >= data["pagination"]["total_pages"]:
            return rows, str(data["leaderboard"].get("updated_at") or "")[:10]
        page += 1


def fetch_webdev():
    rows, offset = [], 0
    while True:
        data = _read(Request(f"{WEBDEV['api']}&offset={offset}&length=100", headers={"User-Agent": AGENT}))
        rows += [r["row"] for r in data["rows"]]
        if len(data["rows"]) < 100:
            return rows
        offset += 100


def main():
    terminal_bench_rows, board_updated = fetch_terminal_bench()
    webdev_rows = fetch_webdev()
    models = build(terminal_bench_rows, webdev_rows, NAMES)
    published = max(r["leaderboard_publish_date"] for r in webdev_rows if r.get("category") == "webdev")
    data = {"fetched": datetime.date.today().isoformat(),
            "sources": {"terminalBench": {"label": TERMINAL_BENCH["label"], "url": TERMINAL_BENCH["url"], "asOf": board_updated},
                        "webdev": {"label": WEBDEV["label"], "url": WEBDEV["url"], "asOf": published}},
            "models": models}
    FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    for model, entry in models.items():
        shown = ", ".join(f"{k} {v}" for k, v in entry.items())
        print(f"{model:36} {shown}")
    print(f"Wrote {FILE.name}: Terminal-Bench as of {board_updated}, WebDev as of {published}.")


if __name__ == "__main__":
    sys.exit(main())
