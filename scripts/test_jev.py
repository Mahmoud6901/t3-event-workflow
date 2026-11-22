"""Jev's advice for one new worker of the delegated PR workflow: its model, and for an implementer its effort."""
import json
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
    def test_a_task_asks_only_for_its_implementers_model_and_effort_among_the_approved_ones(self):
        payload = jev.build_request(CONFIG, {"request": "Fix the login typo"}, "task")
        self.assertEqual(set(payload["questions"]), {"implementer", "implementerEffort"})
        self.assertNotIn("claude-opus-5-5", payload["questions"]["implementer"]["criteria"])

    def test_another_role_is_asked_for_its_model_alone_among_its_own_approved_ones(self):
        reviewer = jev.build_request(CONFIG, {"request": "Fix the login typo"}, "task", "reviewer")["questions"]
        self.assertEqual(set(reviewer), {"reviewer"})
        self.assertIn("claude-opus-5-5", reviewer["reviewer"]["criteria"])
        self.assertNotIn("gpt-6.2-sol", reviewer["reviewer"]["criteria"])
        devops = jev.build_request(CONFIG, {}, "task", "devops")["questions"]
        self.assertEqual(set(devops), {"devops"})
        self.assertEqual(set(devops["devops"]["criteria"]),
                         {"claude-sonnet-5-5", "claude-opus-5-5", "gemini-4-neon"})
        with self.assertRaises(ValueError):
            jev.build_request(CONFIG, {}, "task", "orchestrator")

    def test_each_model_shows_its_public_coding_scores_and_jev_is_told_what_they_mean(self):
        config = {**CONFIG, "models": {**flow.MODELS, "made-up/model": {"provider": "codex", "roles": ["implementer"]}}}
        questions = jev.build_request(config, {"request": "Fix the login typo"}, "task")["questions"]
        criteria = questions["implementer"]["criteria"]
        self.assertRegex(criteria["gpt-6.2-sol"], r"^gpt-6\.2-sol \(Terminal-Bench \d+\.\d%, WebDev \d+; OpenAI via Codex\)$")
        self.assertEqual(criteria["made-up/model"], "made-up/model (no public coding score; OpenAI via Codex)")
        borrowed = {"gpt-6.2-astra": {"terminalBench": 62.12, "borrowedFrom": "GPT-6.2"}}
        with patch.object(jev, "_scores", return_value=borrowed):
            criteria = jev.build_request(CONFIG, {}, "task")["questions"]["implementer"]["criteria"]
        self.assertEqual(criteria["gpt-6.2-astra"], "gpt-6.2-astra (GPT-6.2's scores: Terminal-Bench 62.1%; OpenAI via Codex)")
        self.assertIn("Terminal-Bench 4.1", questions["implementer"]["instructions"])
        self.assertIn("LMArena WebDev", questions["implementer"]["instructions"])
        reviewer = jev.build_request(CONFIG, {}, "task", "reviewer")["questions"]["reviewer"]
        self.assertRegex(reviewer["criteria"]["gemini-4-neon"],
                         r"^gemini-4-neon \(Terminal-Bench \d+\.\d%, WebDev \d+; Google via Antigravity\)$")
        self.assertIn("Terminal-Bench 4.1", reviewer["instructions"])

    def test_jev_weighs_how_strong_each_model_is_and_not_what_it_costs(self):
        questions = jev.build_request(CONFIG, {"request": "Fix the login typo"}, "task")["questions"]
        self.assertIn("right the first time", questions["implementer"]["instructions"])
        self.assertIn("right the first time", questions["implementerEffort"]["instructions"])
        asked = json.dumps(questions).lower()
        self.assertNotIn("tier", asked)
        self.assertNotIn("cheap", asked)

    def test_valid_choices_are_kept_and_an_invalid_one_is_left_to_the_defaults(self):
        payload = jev.build_request(CONFIG, {}, "task")
        answer = envelope(payload, {"implementer": "gpt-6.2-astra", "implementerEffort": "xhigh"})
        with patch.object(jev, "_fetch", return_value=(answer, None)):
            choice = flow.default_jev(CONFIG, {"request": "Fix the login typo"})
        self.assertEqual(choice, {"implementer": "gpt-6.2-astra", "implementerEffort": "xhigh"})
        answer = envelope(payload, {"implementer": "gpt-6.2-astra", "implementerEffort": "extreme"})
        with patch.object(jev, "_fetch", return_value=(answer, None)):
            choice = flow.default_jev(CONFIG, {"request": "Fix the login typo"})
        self.assertEqual(choice, {"implementer": "gpt-6.2-astra"})
        payload = jev.build_request(CONFIG, {}, "task", "reviewer")
        with patch.object(jev, "_fetch", return_value=(envelope(payload, {"reviewer": "gemini-4-neon"}), None)):
            choice = flow.default_jev(CONFIG, {"request": "Fix the login typo"}, "reviewer")
        self.assertEqual(choice, {"reviewer": "gemini-4-neon"})

    def test_an_unreachable_service_is_reported_so_the_defaults_stand_with_a_health_problem(self):
        with patch.object(jev, "_fetch", return_value=(None, "connection_error")):
            with self.assertRaisesRegex(RuntimeError, "connection_error"):
                flow.default_jev(CONFIG, {})

    def test_another_purpose_is_refused(self):
        with self.assertRaises(ValueError):
            jev.build_request(CONFIG, {}, "route")


if __name__ == "__main__":
    unittest.main()
