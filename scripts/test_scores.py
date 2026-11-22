"""The LiveBench 2.0 scores Jev sees beside each approved model, and the refresh that keeps them current."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import flow
import scores

# LiveBench 2.0's table.csv for release 20261106 (2026-11-22), trimmed.
TABLE = """model,long_agentic,reasoning,coding,browser,data_analysis,research,design
gemini-4-neon,82.6,89.6,89.7,95.6,85.3,94.5,81.5
gpt-6.2-sol,63.6,90.6,83.7,88.2,81.1,90.1,80.0
claude-haiku-5.5,47.9,82.4,77.6,n/a,53.1,84.3,
"""
# The part of the site's bundle that holds its releases (2026-11-22).
BUNDLE = ('const tn={OpenAI:"#10A37F"};const en=["20261014","20261025","20261106"];'
          'throw new Error("Two releases share a date on the timeline (v1 RELEASES vs 2.0 RELEASE_LABELS)");')


class RefreshTests(unittest.TestCase):
    def test_each_model_gets_the_chosen_suites_and_one_it_was_not_scored_on_is_left_out(self):
        rows = scores.parse(TABLE)
        found = scores.build(rows, {"gemini-4-neon": "gemini-4-neon", "claude-haiku-5-5": "claude-haiku-5.5",
                                    "made-up/stand-in": "gpt-6.2-sol"}, ("coding", "browser", "design"), {"made-up/stand-in"})
        self.assertEqual(found["gemini-4-neon"], {"coding": 89.7, "browser": 95.6, "design": 81.5})
        self.assertEqual(found["claude-haiku-5-5"], {"coding": 77.6}, "n/a and blank are not scores")
        self.assertEqual(found["made-up/stand-in"], {"coding": 83.7, "browser": 88.2, "design": 80.0,
                                                     "borrowedFrom": "gpt-6.2-sol"})

    def test_a_model_with_none_of_the_chosen_scores_stops_the_refresh(self):
        with self.assertRaisesRegex(ValueError, "made-up/model"):
            scores.build(scores.parse(TABLE), {"made-up/model": "made-up"}, ("coding",))

    def test_the_releases_come_from_the_sites_bundle(self):
        self.assertEqual(scores.releases(BUNDLE), ["20261014", "20261025", "20261106"])
        with self.assertRaisesRegex(ValueError, "--release"):
            scores.releases("const en=[];")

    def test_the_suites_option_is_kept_by_later_refreshes_and_an_unknown_suite_is_refused(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        file = Path(folder.name) / "scores.json"
        names = {"gpt-6.2-sol": "gpt-6.2-sol", "gemini-4-neon": "gemini-4-neon"}
        fetched = ("20261106", scores.parse(TABLE), "2026-11-19")
        with patch.object(scores, "FILE", file), patch.object(scores, "NAMES", names), \
                patch.object(scores, "fetch", return_value=fetched) as fetch, contextlib.redirect_stdout(io.StringIO()):
            scores.main([])
            self.assertEqual(list(json.loads(file.read_text(encoding="utf-8"))["suites"]), ["coding", "long_agentic"])
            scores.main(["--suites", "coding,long_agentic,browser"])
            scores.main([])
            data = json.loads(file.read_text(encoding="utf-8"))
            self.assertEqual(list(data["suites"]), ["coding", "long_agentic", "browser"], "a plain refresh keeps the choice")
            self.assertEqual(data["suites"]["browser"]["name"], "Browser Use")
            self.assertEqual(data["models"]["gemini-4-neon"], {"coding": 89.7, "long_agentic": 82.6, "browser": 95.6})
            self.assertEqual(data["source"], {"label": "LiveBench 2.0", "url": "https://livebench.ai/",
                                              "release": "20261106", "updated": "2026-11-19"})
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                scores.main(["--suites", "coding,vibes"])
            self.assertEqual(json.loads(file.read_text(encoding="utf-8")), data, "a refused refresh writes nothing")
            scores.main(["--release", "20261025"])
        self.assertEqual([c.args for c in fetch.call_args_list], [(None,)] * 3 + [("20261025",)])

    def test_every_approved_model_has_its_board_name_and_its_shipped_scores(self):
        self.assertEqual(set(flow.MODELS) - set(scores.NAMES), set())
        shipped = json.loads((Path(flow.ROOT) / "assets" / "scores.json").read_text(encoding="utf-8"))
        self.assertEqual(set(flow.MODELS) - set(shipped["models"]), set())
        self.assertLessEqual(set(shipped["suites"]), set(scores.SUITES))


if __name__ == "__main__":
    unittest.main()
