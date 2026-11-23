"""Jev's advice for one new worker of the delegated PR workflow: its model."""
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
    def test_a_task_asks_only_for_its_implementers_model_among_the_approved_ones_and_never_its_effort(self):
        payload = jev.build_request(CONFIG, {"request": "Fix the login typo"}, "task")
        self.assertEqual(set(payload["questions"]), {"implementer"})
        self.assertNotIn("effort", json.dumps(payload["questions"]).lower())
        self.assertNotIn("claude-fable-5-2", payload["questions"]["implementer"]["criteria"], "it has no role yet")

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

    def test_each_option_shows_its_card_its_record_its_scores_and_its_route(self):
        config = {**CONFIG, "models": {
            "gpt-6.2-sol": {**flow.MODELS["gpt-6.2-sol"], "record": "small: 3 tasks, 3 approved first time"},
            "qwen/qwen4-max": {**flow.MODELS["qwen/qwen4-max"], "route": {"provider": "acpRegistry_goose", "paid": True}},
            "made-up/model": {"provider": "codex", "roles": ["implementer"]}}}
        with patch.object(jev, "_scores", return_value={"livebench": {
                "label": "LiveBench 2.0", "suites": {"coding": {"name": "Coding", "measures": "bug fixes"}},
                "models": {"gpt-6.2-sol": {"coding": 83.7}}}}):
            question = jev.build_request(config, {"request": "Fix the login typo"}, "task")["questions"]["implementer"]
        card = jev.cards()["gpt-6.2-sol"]
        self.assertEqual(question["criteria"]["gpt-6.2-sol"], (
            f"gpt-6.2-sol. Good at: {'; '.join(card['good_at'])}. Weak at: {'; '.join(card['weak_at'])}. "
            "Record in the owner's projects: small: 3 tasks, 3 approved first time. Scores: LiveBench 2.0 Coding 83.7. "
            "Subscription."))
        self.assertTrue(question["criteria"]["qwen/qwen4-max"].endswith(
            "Record in the owner's projects: no record yet. Scores: LiveBench 2.0 unknown. Paid per token."))
        self.assertEqual(question["criteria"]["made-up/model"], "made-up/model. Good at: unknown. Weak at: unknown. "
                         "Record in the owner's projects: no record yet. Scores: LiveBench 2.0 unknown. Subscription.")
        self.assertIn("LiveBench 2.0: Coding, bug fixes.", question["instructions"])

    def test_each_board_and_suite_in_the_scores_reaches_jev_and_a_board_without_a_model_shows_it_unknown(self):
        boards = {"livebench": {"label": "LiveBench 2.0", "scale": "0 to 100",
                                "suites": {"coding": {"name": "Coding", "measures": "real merged-PR bug fixes"},
                                           "browser": {"name": "Browser Use", "measures": "multi-step tasks in web apps"}},
                                "models": {"gpt-6.2-sol": {"coding": 83.7, "browser": 88.2},
                                           "gpt-6.2-astra": {"coding": 91.9, "borrowedFrom": "gpt-6.2"}}},
                  "other": {"label": "Other Board", "scale": "share solved, 0 to 1",
                            "suites": {"private": {"name": "Private", "measures": "unpublished programs"}},
                            "models": {"gpt-6.2-astra": {"private": 0.5}}}}
        with patch.object(jev, "_scores", return_value=boards):
            question = jev.build_request(CONFIG, {}, "task")["questions"]["implementer"]
        self.assertIn("Scores: LiveBench 2.0 Coding 83.7, Browser Use 88.2; Other Board unknown.",
                      question["criteria"]["gpt-6.2-sol"])
        self.assertIn("Scores: gpt-6.2's LiveBench 2.0 Coding 91.9; Other Board Private 0.5.",
                      question["criteria"]["gpt-6.2-astra"])
        self.assertIn("LiveBench 2.0, scored 0 to 100: Coding, real merged-PR bug fixes; Browser Use, multi-step tasks in "
                      "web apps.", question["instructions"])
        self.assertIn("Other Board, scored share solved, 0 to 1: Private, unpublished programs.", question["instructions"])
        self.assertIn("unknown there is not weaker", question["instructions"])

    def test_jev_weighs_how_strong_each_model_is_and_not_what_it_costs(self):
        questions = jev.build_request(CONFIG, {"request": "Fix the login typo"}, "task")["questions"]
        self.assertIn("right the first time", questions["implementer"]["instructions"])
        self.assertIn("Price is not a reason either way.", questions["implementer"]["instructions"])
        asked = json.dumps(questions).lower()
        self.assertNotIn("tier", asked)
        self.assertNotIn("cheap", asked)

    def test_jev_sizes_the_change_from_the_task_and_fits_strengths_and_weaknesses_to_it(self):
        instructions = jev.build_request(CONFIG, {"request": "Fix the login typo"}, "task")["questions"]["implementer"][
            "instructions"]
        self.assertIn("First size the change from the task itself, not from how long its brief is or the rules every "
                      "task of its plan repeats", instructions)
        self.assertIn("small and contained", instructions)
        self.assertIn("the option whose strengths fit this task's size and kind and whose weaknesses matter least",
                      instructions)
        self.assertIn("Once a record covers several tasks of this size, it outweighs published scores and makers' "
                      "claims.", instructions)
        self.assertIn("The scores are one piece of evidence, not the rule.", instructions)
        self.assertNotIn("strongest on", instructions, "no single score decides any more")

    def test_the_whole_request_stays_compact_with_every_option(self):
        config = {**CONFIG, "models": {m: {**item, "record": "small: 12 tasks, 10 approved first time, 1.2 review "
                                           "rounds on average, 1 blocked; medium: 4 tasks, 3 approved first time, 0.8 "
                                           "review rounds on average"} for m, item in flow.MODELS.items()}}
        payload = jev.build_request(config, {"request": "Fix the login typo", "brief": "x" * 2000, "plan": "M30"}, "task")
        self.assertEqual(len(payload["questions"]["implementer"]["criteria"]), len(flow.MODELS) - 1)
        self.assertLess(len(json.dumps(payload)), 14000)

    def test_a_valid_choice_is_kept_and_an_invalid_one_is_left_to_the_harness(self):
        payload = jev.build_request(CONFIG, {}, "task")
        answer = envelope(payload, {"implementer": "gpt-6.2-astra"})
        with patch.object(jev, "_fetch", return_value=(answer, None)):
            choice = flow.default_jev(CONFIG, {"request": "Fix the login typo"})
        self.assertEqual(choice, {"implementer": "gpt-6.2-astra"})
        answer = envelope({"questions": {"implementer": {"criteria": {"made-up/model": "?"}}}}, {"implementer": "made-up/model"})
        with patch.object(jev, "_fetch", return_value=(answer, None)):
            choice = flow.default_jev(CONFIG, {"request": "Fix the login typo"})
        self.assertEqual(choice, {})
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
