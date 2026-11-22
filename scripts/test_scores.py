"""The public coding scores Jev sees beside each approved model, and the refresh that keeps them current."""
import json
import unittest
from pathlib import Path

import flow
import scores

# Terminal-Bench 4.1 rows as the Harbor read API returns them (2026-11-22), trimmed to what the refresh reads.
TERMINAL_BENCH = [
    {"metadata": {"model_display": {"label": "GPT-6.2 Sol"}, "reasoning_effort": "max"}, "metrics": {"accuracy": 61.52}},
    {"metadata": {"model_display": {"label": "GPT-6.2 Sol"}, "reasoning_effort": "high"}, "metrics": {"accuracy": 58.0}},
    {"metadata": {"model_display": {"label": "GLM-5.3"}, "reasoning_effort": "max"}, "metrics": {"accuracy": 41.21}},
    {"metadata": {"model_display": {"label": "GLM-5.3-Flash"}, "reasoning_effort": "max"}, "metrics": {"accuracy": 36.97}},
]
# LMArena WebDev rows as the Hugging Face rows API returns them (2026-11-22), trimmed.
WEBDEV = [
    {"model_name": "GPT-6.2 Sol (Max)", "rating": 1754.0, "category": "webdev", "leaderboard_publish_date": "2026-11-20"},
    {"model_name": "GPT-6.2 Sol (Max)", "rating": 1819.7, "category": "webdev-html", "leaderboard_publish_date": "2026-11-20"},
    {"model_name": "GPT-6.2 Sol (Max)", "rating": 1700.0, "category": "webdev", "leaderboard_publish_date": "2026-11-19"},
    {"model_name": "GLM-5.3 (Max)", "rating": 1622.7, "category": "webdev", "leaderboard_publish_date": "2026-11-20"},
    {"model_name": "GLM-5.3-Flash", "rating": 1610.5, "category": "webdev", "leaderboard_publish_date": "2026-11-20"},
    {"model_name": "Gemini 4 Neon (High)", "rating": 1764.9, "category": "webdev", "leaderboard_publish_date": "2026-11-20"},
]
NAMES = {
    "gpt-6.2-sol": {"terminalBench": "GPT-6.2 Sol", "webdev": "GPT-6.2 Sol"},
    "z-ai/glm-5.3-prime": {"terminalBench": "GLM-5.3", "webdev": "GLM-5.3", "borrowedFrom": "GLM-5.3"},
    "gemini-4-neon": {"terminalBench": "Gemini 4 Neon", "webdev": "Gemini 4 Neon"},
}


class RefreshTests(unittest.TestCase):
    def test_each_model_gets_its_best_score_from_each_source_and_a_stand_in_is_marked_borrowed(self):
        found = scores.build(TERMINAL_BENCH, WEBDEV, NAMES)
        self.assertEqual(found["gpt-6.2-sol"], {"terminalBench": 61.52, "webdev": 1754.0},
                         "its best effort on Terminal-Bench; LMArena's newest overall WebDev rating")
        self.assertEqual(found["z-ai/glm-5.3-prime"], {"terminalBench": 41.21, "webdev": 1622.7, "borrowedFrom": "GLM-5.3"})
        self.assertEqual(found["gemini-4-neon"], {"webdev": 1764.9}, "a source that has not scored it leaves it out")

    def test_a_model_neither_source_scores_stops_the_refresh(self):
        with self.assertRaisesRegex(ValueError, "made-up/model"):
            scores.build(TERMINAL_BENCH, WEBDEV, {**NAMES, "made-up/model": {"terminalBench": "Made Up", "webdev": "Made Up"}})

    def test_every_approved_model_has_its_names_and_its_shipped_scores(self):
        self.assertEqual(set(flow.MODELS) - set(scores.NAMES), set())
        shipped = json.loads((Path(flow.ROOT) / "assets" / "scores.json").read_text(encoding="utf-8"))
        self.assertEqual(set(flow.MODELS) - set(shipped["models"]), set())


if __name__ == "__main__":
    unittest.main()
