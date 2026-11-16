"""Jev's advice for one task of the delegated PR workflow: the implementer's and reviewer's model and effort."""
import unittest
from unittest.mock import patch

import flow
import jev

CONFIG = {"models": flow.MODELS, "defaults": flow.DEFAULTS, "jev": {"model": "jev-1.13.0"}}


def envelope(payload, choices):
    """Jev's answer to payload: each chosen option with full probability."""
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 20, "output_tokens": 10}, "answers": {
        key: {"type": "choice", "choice": choices[key], "confidence": .9,
              "probabilities": {option: float(option == choices[key]) for option in question["criteria"]}}
        for key, question in payload["questions"].items() if key in choices}}


class TaskAdviceTests(unittest.TestCase):
    def test_a_task_asks_only_for_its_workers_models_and_efforts_among_the_approved_ones(self):
        payload = jev.build_request(CONFIG, {"request": "Fix the login typo"}, "task")
        self.assertEqual(set(payload["questions"]), {"implementer", "implementerEffort", "reviewer", "reviewerEffort"})
        self.assertNotIn("claude-opus-5-5", payload["questions"]["implementer"]["criteria"])
        self.assertIn("claude-opus-5-5", payload["questions"]["reviewer"]["criteria"])

    def test_valid_choices_are_kept_and_an_invalid_one_is_left_to_the_defaults(self):
        payload = jev.build_request(CONFIG, {}, "task")
        answer = envelope(payload, {"implementer": "gpt-6-astra", "implementerEffort": "xhigh",
                                    "reviewer": "claude-opus-5-5", "reviewerEffort": "extreme"})
        with patch.object(jev, "_fetch", return_value=(answer, None)):
            choice = flow.default_jev(CONFIG, {"request": "Fix the login typo"})
        self.assertEqual(choice, {"implementer": "gpt-6-astra", "reviewer": "claude-opus-5-5", "implementerEffort": "xhigh"})

    def test_an_unreachable_service_is_reported_so_the_defaults_stand_with_a_health_problem(self):
        with patch.object(jev, "_fetch", return_value=(None, "connection_error")):
            with self.assertRaisesRegex(RuntimeError, "connection_error"):
                flow.default_jev(CONFIG, {})

    def test_another_purpose_is_refused(self):
        with self.assertRaises(ValueError):
            jev.build_request(CONFIG, {}, "route")


if __name__ == "__main__":
    unittest.main()
