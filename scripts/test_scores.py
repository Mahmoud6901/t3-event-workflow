"""The LiveBench 2.0 scores Jev sees beside each approved model, and the refresh that keeps them current."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import flow
import jev
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

    def test_the_releases_come_from_the_sites_bundle(self):
        self.assertEqual(scores.releases(BUNDLE), ["20261014", "20261025", "20261106"])
        with self.assertRaisesRegex(ValueError, "--release"):
            scores.releases("const en=[];")

    def refresh(self, *argv, names=None, boards=None):
        """Run the refresh against the trimmed LiveBench table, writing to a temporary file; its data."""
        names = names or {"livebench": {"gpt-6.2-sol": "gpt-6.2-sol", "gemini-4-neon": "gemini-4-neon"}}
        with patch.object(scores, "FILE", self.file), patch.object(scores, "NAMES", names), \
                patch.object(scores, "BOARDS", boards or scores.BOARDS), \
                patch.object(scores, "fetch", return_value=("20261106", scores.parse(TABLE), "2026-11-19")) as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            scores.main(list(argv))
        self.fetched = [*getattr(self, "fetched", []), *(c.args for c in fetch.call_args_list)]
        return json.loads(self.file.read_text(encoding="utf-8"))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "scores.json"

    def test_the_suites_option_is_kept_by_later_refreshes_and_an_unknown_suite_is_refused(self):
        self.assertEqual(list(self.refresh()["boards"]["livebench"]["suites"]), ["coding", "long_agentic"])
        self.refresh("--suites", "coding,long_agentic,browser")
        data = self.refresh()
        livebench = data["boards"]["livebench"]
        self.assertEqual(list(livebench["suites"]), ["coding", "long_agentic", "browser"], "a plain refresh keeps the choice")
        self.assertEqual(livebench["suites"]["browser"]["name"], "Browser Use")
        self.assertEqual(livebench["models"]["gemini-4-neon"], {"coding": 89.7, "long_agentic": 82.6, "browser": 95.6})
        self.assertEqual({k: livebench[k] for k in ("label", "url", "scale", "release", "updated")},
                         {"label": "LiveBench 2.0", "url": "https://livebench.ai/", "scale": "0 to 100",
                          "release": "20261106", "updated": "2026-11-19"})
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.refresh("--suites", "coding,vibes")
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8")), data, "a refused refresh writes nothing")
        self.refresh("--release", "20261025")
        self.assertEqual(self.fetched, [(None,)] * 3 + [("20261025",)])

    def test_another_board_joins_the_scores_and_a_model_it_has_not_scored_is_left_to_the_others(self):
        def other(args):
            return {"label": "Other Board", "url": "https://example.org/", "scale": "0 to 100", "release": "1",
                    "updated": "2026-11-20", "suites": {"private": {"name": "Private", "measures": "unpublished tasks"}},
                    "models": {"gpt-6.2-sol": {"private": 50.0}}}
        boards = {**scores.BOARDS, "other": other}
        names = {"livebench": {"gpt-6.2-sol": "gpt-6.2-sol", "gemini-4-neon": "gemini-4-neon"},
                 "other": {"gpt-6.2-sol": "Sol"}}
        data = self.refresh(names=names, boards=boards)
        self.assertEqual(list(data["boards"]), ["livebench", "other"])
        self.assertEqual(data["boards"]["other"]["models"], {"gpt-6.2-sol": {"private": 50.0}})
        self.assertIn("gemini-4-neon", data["boards"]["livebench"]["models"])

    def test_a_model_no_board_scores_stops_the_refresh_and_writes_nothing(self):
        names = {"livebench": {"gpt-6.2-sol": "gpt-6.2-sol", "made-up/model": "made-up"}}
        with self.assertRaisesRegex(ValueError, "made-up/model"):
            self.refresh(names=names)
        self.assertFalse(self.file.exists())

    def test_each_named_model_is_approved_and_has_shipped_scores_and_the_rest_stay_unknown(self):
        named = {m for names in scores.NAMES.values() for m in names}
        self.assertLessEqual(named, set(flow.MODELS))
        boards = json.loads((Path(flow.ROOT) / "assets" / "scores.json").read_text(encoding="utf-8"))["boards"]
        self.assertEqual(named - {m for board in boards.values() for m in board["models"]}, set())
        self.assertLessEqual({m for board in boards.values() for m in board["models"]}, set(flow.MODELS))
        self.assertLessEqual(set(boards["livebench"]["suites"]), set(scores.SUITES))
        request = json.dumps(jev.build_request({"models": {"z-ai/glm-5.3-prime": flow.MODELS["z-ai/glm-5.3-prime"]}}, {}))
        self.assertIn("Scores: LiveBench 2.0 unknown.", request, "a model no board lists")


if __name__ == "__main__":
    unittest.main()
