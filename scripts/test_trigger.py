"""The user's opt-in: which turns are ordinary. T3's own resume message is not the user's word."""
import unittest

import trigger

RESUME = {"createdBy": "user", "creationSource": "server", "text": "Continue where you left off."}
EVENT = {"createdBy": "agent", "creationSource": "mcp", "text": '{"action":"RESUME"}'}
USER = {"createdBy": "user", "creationSource": "web", "text": "What does the copy card do?"}
ASKED = {"createdBy": "user", "creationSource": "web", "text": "Use t3 workflow. Fix the typo."}


class ResumeTests(unittest.TestCase):
    def test_only_t3s_exact_resume_counts_not_a_person_saying_the_same_words(self):
        self.assertTrue(trigger.resumed(RESUME))
        # All seen in T3's store on 2026-11-04: nine of the first kind, after the user interrupted a turn.
        self.assertFalse(trigger.resumed({**RESUME, "creationSource": "web"}))
        self.assertFalse(trigger.resumed({**RESUME, "text": "yes"}))
        self.assertFalse(trigger.resumed({**RESUME, "createdBy": "agent"}))

    def test_a_resume_carries_on_a_workflow_turn(self):
        self.assertFalse(trigger.plain([RESUME], [[EVENT]]))
        self.assertFalse(trigger.plain([RESUME], [[ASKED]]))

    def test_a_resume_of_an_ordinary_turn_stays_ordinary(self):
        self.assertTrue(trigger.plain([RESUME], [[USER]]))

    def test_a_resume_of_a_resume_looks_further_back(self):
        self.assertFalse(trigger.plain([RESUME], [[RESUME], [EVENT], [USER]]))
        self.assertTrue(trigger.plain([RESUME], [[RESUME], [USER], [EVENT]]))
        self.assertFalse(trigger.plain([RESUME], [[], [EVENT]]))  # A turn with no message of its own says nothing.

    def test_a_resume_with_nothing_before_it_starts_nothing(self):
        self.assertTrue(trigger.plain([RESUME]))

    def test_older_turns_are_read_only_for_a_resume(self):
        def never():
            raise AssertionError("A hook runs this on every tool call")
            yield

        self.assertFalse(trigger.plain([EVENT], never()))
        self.assertTrue(trigger.plain([USER], never()))
        self.assertFalse(trigger.plain([ASKED], never()))
        self.assertFalse(trigger.plain([], never()))

    def test_a_person_writing_in_a_resumed_turn_decides_it(self):
        self.assertTrue(trigger.plain([RESUME, USER], [[EVENT]]))
        self.assertFalse(trigger.plain([RESUME, ASKED], [[USER]]))
        self.assertFalse(trigger.plain([RESUME, EVENT], [[USER]]))


class OptInTests(unittest.TestCase):
    def test_a_quoted_mention_of_the_phrase_asks_for_nothing(self):
        # 2026-11-11: a handoff pasted into the bound orchestrator quoted the rule itself, and the guard then denied git
        # in what was an ordinary turn. Quoting the phrase names it; it does not ask for the workflow.
        for text in ('the T3 workflow applies only when the user\'s message says "use t3 workflow".',
                     "Open a session and say “use t3 workflow: set up this project”.",
                     "Run `use t3 workflow: status` in that thread.",
                     "The phrase 'use the t3 workflow' starts it."):
            self.assertTrue(trigger.plain([{**USER, "text": text}]), text)

    def test_the_phrase_in_the_users_own_words_still_asks(self):
        for text in ("m12-login-plan.md use t3 workflow to implement this implementation doc plan",
                     'The rule says "use t3 workflow", so: use t3 workflow: status'):
            self.assertFalse(trigger.plain([{**USER, "text": text}]), text)


if __name__ == "__main__":
    unittest.main()
