"""The harness command line (seam 1): a task's whole life in the delegated PR workflow, against a temporary database,
real git repositories and a fake GitHub."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zoneinfo

sys.path.insert(0, str(Path(__file__).parent))
from unittest.mock import patch  # noqa: E402

import flow  # noqa: E402
import github  # noqa: E402
import harness  # noqa: E402
import health  # noqa: E402
import jev  # noqa: E402
import record  # noqa: E402
import store  # noqa: E402

def levels(*ids):
    return [{"id": i, "label": i.title()} for i in ids]


# The shape of T3's ~/.t3/caches entries (2026-11-17): each effort option lists its levels.
GOOSE_LIST = [
    {"slug": "qwen/qwen3.8-flash", "name": "qwen/qwen3.8-flash",
     "capabilities": {"optionDescriptors": [{"id": "provider"}, {"id": "mode"}]}},
    {"slug": "anthropic/claude-sonnet-4", "name": "anthropic/claude-sonnet-4",
     "capabilities": {"optionDescriptors": [{"id": "provider"}, {"id": "mode"},
                                            {"id": "thinking_effort", "options": levels("off", "low", "medium", "high", "max")}]}},
    {"slug": "qwen/qwen3.8-max", "name": "qwen/qwen3.8-max", "capabilities": {"optionDescriptors": [{"id": "provider"}]}},
]
# T3's Antigravity list (2026-11-22): its effort option is `thought_level`, and it also lists Claude models.
ANTIGRAVITY_LIST = [
    {"slug": "gemini-4-neon", "name": "Gemini 4 Neon", "capabilities": {"optionDescriptors": [
        {"id": "thought_level", "options": levels("low", "medium", "high", "xhigh")}]}},
    {"slug": "claude-opus-5-5", "name": "Claude Opus 5.5", "capabilities": {"optionDescriptors": [
        {"id": "thought_level", "options": levels("low", "medium", "high")}]}},
]
# The paid routes of approved models, as T3's goose list names them on OpenRouter.
PAID_LIST = [{"slug": s, "name": s, "capabilities": {"optionDescriptors": [
    {"id": "provider"}, {"id": "thinking_effort", "options": levels("off", "low", "medium", "high", "max")}]}}
             for s in ("anthropic/claude-opus-5.5", "openai/gpt-6.2-astra", "google/gemini-4-neon", "google/gemini-4-carbon",
                       "qwen/qwen4-max")]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultrathink"]
CLAUDE_LIST = [{"slug": s, "capabilities": {"optionDescriptors": [{"id": "effort", "options": levels(*CLAUDE_EFFORTS)}]}}
               for s in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")]
CODEX_LIST = [{"slug": s, "capabilities": {"optionDescriptors": [
    {"id": "reasoningEffort", "options": levels("low", "medium", "high", "xhigh", "max", "ultra")}]}}
              for s in ("gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra")]

SPENT = [{"id": "five_hour", "kind": "session", "usedPercent": 100, "resetsAt": "2999-01-01T00:00:00.000Z"}]
# The end of a run stopped by its model's quota, as T3's wake notice gives it (2026-11-23).
QUOTA_STOP = ("Antigravity stopped this turn because of an error.\n\nLast assistant message before the run failed:\n\n\n"
              "⚠ Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 4h11m25s.\n")
CLAUDE_STOP = ("Claude API rate limit reached. Try again later.\n\nLast assistant message before the run failed:\n\n"
               "You've hit your 5-hour limit · resets 1:50am (Europe/Berlin)")
# Scores as scores.py writes them. Astra leads on Coding, but Neon has the best average and Sol the worst.
SCORES = {"fetched": "2026-11-22", "boards": {"livebench": {
    "label": "LiveBench 2.0", "scale": "0 to 100",
    "suites": {"coding": {"name": "Coding", "measures": "bug fixes"}, "long_agentic": {"name": "Long Agentic", "measures": "long work"}},
    "models": {"gpt-6.2-sol": {"coding": 70.0, "long_agentic": 60.0}, "gpt-6.2-astra": {"coding": 90.0, "long_agentic": 70.0},
               "gemini-4-neon": {"coding": 85.0, "long_agentic": 85.0}}}}}

ORCH = "thread-orch"
PROJECT = "proj-1"
REPO = "owner/app"


def git(cwd, *args, check=True):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if not check:
        return done.returncode, done.stdout.strip()
    if done.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


class FakeHub:
    """GitHub as the flow sees it: PRs over the real origin repository, statuses and checks held here."""

    def __init__(self, origin):
        self.origin = origin
        self.prs = {}
        self.statuses = {}  # (sha, context) -> state
        self.checks_on = {}  # sha -> {name: state}
        self.posted = []
        self.merged = []
        self.protection = [{"context": "t3/review"}, {"context": "acceptance", "app_id": 15368}]  # main's required checks

    def open(self, number, branch, files, body="Fixes it.\n\n## Decisions\nNone", lines=2):
        head = git(self.origin, "rev-parse", f"refs/heads/{branch}")
        body = self.prs[number]["body"] if number in self.prs else body  # A push keeps the PR's body.
        self.prs[number] = {"number": number, "state": "open", "merged": False, "head": head, "branch": branch,
                            "base": "main", "url": f"https://github.com/{REPO}/pull/{number}", "mergeCommit": None,
                            "body": body, "files": files, "changedFiles": len(files), "additions": lines - lines // 2,
                            "deletions": lines // 2}

    def pull(self, number):
        return dict(self.prs[number])

    def checks(self, sha, names):
        found = {**self.checks_on.get(sha, {}), **{c: s for (h, c), s in self.statuses.items() if h == sha}}
        return {n: found.get(n, "missing") for n in names}

    def approve(self, sha, description):
        self.posted.append((sha, "t3/review", "success"))
        self.statuses[(sha, "t3/review")] = "success"

    def setup(self, branch, required):
        self.setups = getattr(self, "setups", []) + [(branch, list(required))]
        return ["delete_branch_on_merge", f"protection of {branch}"] if len(self.setups) == 1 else []

    def drop_ci_checks(self, branch):
        removed = [c for c in self.protection if c["context"] != "t3/review"]
        self.protection = [c for c in self.protection if c["context"] == "t3/review"]
        return ([f"required checks of {branch}"] if removed else []), removed

    def restore_ci_checks(self, branch, checks):
        back = [c for c in checks if c["context"] not in {p["context"] for p in self.protection}]
        self.protection = self.protection + back
        return [f"required checks of {branch}"] if back else []

    def helper(self):
        """A clone that plays GitHub's side: merging into main and updating PR branches."""
        clone = self.origin.parent / "github-side"
        if not clone.exists():
            git(self.origin.parent, "clone", str(self.origin), str(clone))
            git(clone, "config", "user.email", "gh@example.com")
            git(clone, "config", "user.name", "GitHub")
        git(clone, "fetch", "origin")
        return clone

    def identity(self, number):
        clone = self.helper()
        return github.fingerprint(git(clone, "diff", "origin/main...origin/" + self.prs[number]["branch"]))

    def update(self, number, head, base="main"):
        pr, clone = self.prs[number], self.helper()
        assert pr["head"] == head
        if git(clone, "merge-base", "--is-ancestor", "origin/main", "origin/" + pr["branch"], check=False)[0] == 0:
            return "current", head
        git(clone, "checkout", "-B", pr["branch"], "origin/" + pr["branch"])
        if git(clone, "merge", "--no-edit", "origin/main", check=False)[0] != 0:
            git(clone, "merge", "--abort")
            return "conflict", head
        git(clone, "push", "origin", pr["branch"])
        pr["head"] = git(clone, "rev-parse", "HEAD")
        return "updated", pr["head"]

    def merge(self, number, sha, title):
        pr, clone = self.prs[number], self.helper()
        assert pr["head"] == sha, "head moved"
        git(clone, "checkout", "-B", "main", "origin/main")
        git(clone, "merge", "--squash", "origin/" + pr["branch"])
        git(clone, "commit", "-m", title)
        git(clone, "push", "origin", "main")
        pr.update(state="closed", merged=True, mergeCommit=git(clone, "rev-parse", "HEAD"))
        self.merged.append(number)
        return pr["mergeCommit"]


def worker_command(call):
    """The worker id and token a brief carries."""
    text = call["arguments"]["task"] if call["tool"] == "delegate_task" else call["arguments"]["message"]
    worker = text.split("--worker ")[1].split()[0]
    token = text.split("--token ")[1].split()[0]
    return worker, token


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        git(root, "init", "--bare", "-b", "main", str(self.origin))
        self.workspace = root / "app"
        git(root, "clone", str(self.origin), str(self.workspace))
        git(self.workspace, "config", "user.email", "t@example.com")
        git(self.workspace, "config", "user.name", "Test")
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "login.ts").write_text("export const a = 1;\n")
        git(self.workspace, "add", ".")
        git(self.workspace, "commit", "-m", "start")
        git(self.workspace, "push", "origin", "main")
        self.db = root / "state" / "harness.sqlite3"
        self.hub = FakeHub(self.origin)
        self.jev_answer = {"implementer": "gpt-6.2-astra", "implementerEffort": "medium",
                           "reviewer": "claude-sonnet-5-5", "reviewerEffort": "high"}
        self.caches = root / "t3-caches"  # T3's model lists; missing until a test writes them, so nothing is checked.
        patcher = patch.object(flow, "T3_CACHES", self.caches)
        patcher.start()
        self.addCleanup(patcher.stop)
        scores = root / "scores.json"
        scores.write_text(json.dumps(SCORES), encoding="utf-8")
        patcher = patch.object(jev, "SCORES_FILE", scores)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, request):
        return harness.run(["--db", str(self.db)], json.dumps(request), hub=lambda repo: self.hub, jev=self.advise)

    def advise(self, config, context, role):
        """Jev, as flow.default_jev answers: the role's model. An implementer's effort in jev_answer stands for one an
        older version's answer kept in a task's choice, which no worker may run at."""
        self.jev_asked = getattr(self, "jev_asked", 0) + 1
        self.jev_roles = [*getattr(self, "jev_roles", []), role]
        self.jev_models = sorted(config["models"])
        self.jev_records = {m: item.get("record") for m, item in config["models"].items()}
        return {key: self.jev_answer[key] for key in (role, *([role + "Effort"] if role == "implementer" else []))
                if key in self.jev_answer}

    def orchestrator(self, op, **fields):
        return self.call({"op": op, "projectId": PROJECT, "threadId": ORCH, **fields})

    def bind(self, checks=None, **fields):
        result = self.orchestrator("init", workspace=str(self.workspace), repo=REPO, requiredChecks=["acceptance"],
                                   checks=checks or [], noChecksReason=None if checks else "Test project", **fields)
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.orchestrator("control", action="activate", authorization="Activate the workflow.")["ok"])

    def test_a_queued_task_starts_its_implementer_in_its_own_worktree_on_jevs_model_at_xhigh(self):
        self.bind()
        queued = self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [
            {"task": "Fix the login typo", "brief": "Rename nmae to name in src/login.ts."}]}])
        self.assertTrue(queued["ok"], queued)
        step = self.orchestrator("next")
        self.assertTrue(step["ok"], step)
        (call,) = step["calls"]
        self.assertEqual(call["tool"], "delegate_task")
        arguments = call["arguments"]
        self.assertEqual((arguments["isolation"], arguments["role"], arguments["mode"]), ("worktree", "implementation", "async"))
        self.assertEqual(arguments["target"], {"providerInstanceId": "codex", "model": "gpt-6.2-astra",
                                               "options": {"reasoningEffort": "xhigh", "serviceTier": "default"}},
                         "an effort in Jev's answer, as a task stored before 0.8.1 keeps, is not used")
        self.assertEqual(arguments["clientRequestId"], call["call"])
        self.assertIn("Rename nmae to name in src/login.ts.", arguments["task"])
        self.assertIn("worker pr --worker", arguments["task"])
        self.assertIn(f"Plan M30 started: 1 task.", step["tell"])
        again = self.orchestrator("next")  # Not confirmed yet: the same call again, never a second worker.
        self.assertEqual([c["call"] for c in again["calls"]], [call["call"]])
        self.assertEqual(again["calls"][0]["arguments"]["clientRequestId"], call["call"])

    def t3_caches(self, goose=GOOSE_LIST, claude=CLAUDE_LIST, codex=CODEX_LIST, antigravity=None, usage=None, **goose_top):
        """T3's provider caches as T3 writes them; None leaves that provider's out, as when it is not installed. `usage`
        gives a provider's usage windows, as T3 shows its limits."""
        self.caches.mkdir(exist_ok=True)
        for provider, models, top in (("claudeAgent", claude, {}), ("codex", codex, {}), ("acpRegistry_goose", goose, goose_top),
                                      ("antigravity", antigravity, {})):
            path = self.caches / f"{provider}.json"
            if models is None:
                path.unlink(missing_ok=True)
                continue
            limits = {"usageLimits": {"windows": usage[provider]}} if provider in (usage or {}) else {}
            path.write_text(json.dumps({"enabled": True, "installed": True, "status": "ready",
                                        "auth": {"status": "authenticated"}, "models": models, **limits, **top}))

    def test_setup_searches_every_model_t3_can_run_by_name_before_the_project_is_bound(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        found = self.orchestrator("models", search="QWEN")
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["models"], [{"model": "qwen/qwen3.8-flash", "provider": "goose", "efforts": []},
                                           {"model": "qwen/qwen3.8-max", "provider": "goose", "efforts": []}])
        self.assertEqual(self.orchestrator("models", search="sonnet")["models"],
                         [{"model": "claude-sonnet-5-5", "provider": "Claude", "efforts": CLAUDE_EFFORTS},
                          {"model": "anthropic/claude-sonnet-4", "provider": "goose", "efforts": ["low", "medium", "high", "max"]}])
        self.assertEqual(found["providers"], {"Claude": "ready", "Codex": "ready", "goose": "ready", "Antigravity": "ready"})

    def test_setup_is_told_why_a_provider_cannot_run_and_without_t3_nothing_is_checked(self):
        self.assertFalse(self.orchestrator("models")["known"])
        self.t3_caches(goose=None)
        self.assertIn("not installed", self.orchestrator("models")["providers"]["goose"])
        self.t3_caches(enabled=False)
        self.assertIn("not enabled", self.orchestrator("models")["providers"]["goose"])
        self.t3_caches(goose=[])
        self.assertEqual(self.orchestrator("models")["providers"]["goose"], "T3's goose list is empty")
        self.t3_caches(auth={"status": "unauthenticated"})
        self.assertIn("not signed in", self.orchestrator("models")["providers"]["goose"])

    def test_without_codex_setup_needs_a_pick_for_the_reviewer_and_the_implementer_falls_back_to_opus(self):
        self.t3_caches(codex=None)
        found = self.orchestrator("models")
        self.assertEqual(found["defaults"]["implementer"],
                         {"model": None, "fallback": "claude-opus-5-5", "effort": "xhigh", "runs": True})
        self.assertEqual(found["defaults"]["reviewer"], {"model": "gpt-6.2-astra", "effort": "xhigh", "runs": False})
        refused = self.orchestrator("init", workspace=str(self.workspace), repo=REPO, requiredChecks=["acceptance"], checks=[],
                                    noChecksReason="Test project")
        self.assertIn("T3 cannot run gpt-6.2-astra, the reviewer's default", refused.get("error", ""))
        self.bind(picks={"reviewer": "claude-opus-5-5"})
        self.jev_answer = {}
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        (call,) = self.orchestrator("next")["calls"]
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "claudeAgent", "model": "claude-opus-5-5",
                                                       "options": {"effort": "xhigh"}})

    def test_jev_chooses_the_implementer_and_without_its_answer_or_opus_the_best_scored_model_t3_runs_starts(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.assertEqual(self.orchestrator("models")["defaults"]["implementer"],
                         {"model": None, "fallback": "claude-opus-5-5", "effort": "xhigh", "runs": True})
        self.bind()
        self.assertEqual(self.status()["models"]["implementer"],
                         {"model": None, "modelSource": "jev", "effort": "xhigh", "effortSource": "default"})
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"claudeAgent": SPENT})
        self.jev_answer = {}  # No answer: no Jev key, Jev unreachable, or an invalid reply.
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["model"], "claude-opus-5-5", "through Antigravity, its next route")
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"claudeAgent": SPENT, "antigravity": SPENT})
        self.assertTrue(self.orchestrator("queue_edit", plan="M30", tasks=[{"task": "Add a footer", "brief": "Add it."}])["ok"])
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["model"], "gpt-6.2-astra", "the best scored, while Opus is at its limits")

    def test_jev_advises_only_among_the_models_t3_can_run(self):
        self.t3_caches(goose=None)
        self.bind()
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        self.orchestrator("next")
        self.assertIn("gpt-6.2-sol", self.jev_models)
        self.assertFalse([m for m in self.jev_models if "/" in m], self.jev_models)

    def test_a_model_approved_after_a_project_was_bound_reaches_jev_once_t3_lists_it(self):
        self.t3_caches()  # T3 has no Antigravity list yet, so Jev leaves Gemini 4 Neon out.
        self.bind()
        self.queue("Fix the login typo")
        self.start()
        self.assertNotIn("gemini-4-neon", self.jev_models)
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.jev_answer = {"implementer": "gemini-4-neon"}
        self.assertTrue(self.orchestrator("queue_edit", plan="M30", tasks=[{"task": "Add a footer", "brief": "Add it."}])["ok"])
        (call,) = self.start()
        self.assertIn("gemini-4-neon", self.jev_models)
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "antigravity", "model": "gemini-4-neon",
                                                       "options": {"thought_level": "xhigh"}})

    def test_a_retired_model_leaves_a_bound_projects_list_and_an_older_bindings_defaults_give_way(self):
        retired = ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra")
        self.bind()
        self.stored_config(models={**flow.MODELS, **{m: {"provider": "codex", "tier": tier, "roles": ["implementer"]}
                                                     for tier, m in enumerate(retired, 1)}})
        self.stored_defaults(implementer="gpt-6.2-sol", devops="claude-haiku-5-5")  # As a binding before 0.8.0 keeps them.
        models = self.status()["models"]
        self.assertEqual(models["implementer"]["modelSource"], "jev", "the implementer has no default model now")
        self.assertEqual(models["devops"]["model"], "claude-sonnet-5-5", "a retired default moves to the current one")
        self.jev_answer = {}  # No answer, so the fallback starts, not the old default.
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["model"], "claude-opus-5-5")
        self.assertIn("gpt-6.2-astra", self.jev_models)
        self.assertFalse(set(retired) & set(self.jev_models), self.jev_models)

    def stored_config(self, **changes):
        """The binding's config as an older version stored it."""
        db = store.connect(self.db)
        try:
            with db:
                config = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (PROJECT,)).fetchone()[0])
                config.update(changes)
                db.execute("UPDATE projects SET config=? WHERE id=?", (store.packed(config), PROJECT))
        finally:
            db.close()

    def loaded_models(self):
        engine = flow.Flow(self.db)
        try:
            return engine.load(PROJECT)[0]["models"]
        finally:
            engine.close()

    def test_only_a_model_with_a_card_is_approved_each_in_its_roles(self):
        self.assertEqual(set(jev.cards()), set(flow.MODELS), "every shipped model has a card, and every card a model")
        self.assertEqual(set(flow.approved()), set(flow.MODELS))
        roles = {role: sorted(m for m, item in flow.approved().items() if role in item["roles"]) for role in flow.ROLES}
        self.assertEqual(roles["implementer"], sorted(set(flow.MODELS) - {"claude-fable-5-2"}))
        self.assertEqual(roles["reviewer"], sorted(["gpt-6.2-astra", "claude-opus-5-5", "claude-sonnet-5-5", "gemini-4-neon",
                                                    "google/gemini-4-carbon"]))
        self.assertEqual(roles["devops"], sorted(["claude-sonnet-5-5", "claude-opus-5-5", "gemini-4-neon"]))
        for model, card in jev.cards().items():
            with self.subTest(model):
                self.assertTrue(2 <= len(card["good_at"]) <= 4 and 2 <= len(card["weak_at"]) <= 4, card)
                self.assertTrue(card["maker"] and card["read"] and card["sources"])
        with patch.object(jev, "cards", return_value={m: c for m, c in jev.cards().items() if m != "qwen/qwen4-max"}):
            self.assertNotIn("qwen/qwen4-max", flow.approved(), "a model without a card is never offered")

    def test_an_older_binding_picks_up_the_approved_list_with_the_models_that_came_back(self):
        self.bind()
        self.stored_config(models={"gpt-6.2-sol": {"provider": "codex", "roles": ["implementer"]},
                                   "made-up/model": {"provider": "codex", "roles": ["implementer", "reviewer"]}},
                           defaults={**flow.DEFAULTS, "reviewer": "made-up/model"})
        models = self.loaded_models()
        self.assertEqual(set(models), set(flow.MODELS))
        for model in ("deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-prime", "moonshotai/kimi-k3.1-pro"):
            self.assertIn("implementer", models[model]["roles"])
        self.assertEqual(self.status()["models"]["reviewer"]["model"], "gpt-6.2-astra", "a default without a card gives way")

    def test_a_model_whose_first_route_is_at_its_limit_starts_on_its_next_at_the_effort_that_route_takes(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"claudeAgent": SPENT})
        self.jev_answer = {"implementer": "claude-opus-5-5"}
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertIn("claude-opus-5-5", self.jev_models, "its Antigravity route has capacity")
        self.assertNotIn("claude-sonnet-5-5", self.jev_models, "this T3 runs Sonnet only through Claude")
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "antigravity", "model": "claude-opus-5-5",
                                                       "options": {"thought_level": "high"}}, "Antigravity offers no xhigh")
        self.assertEqual(self.last_done["tell"], ["Implementer for Fix the login typo (M30) started on claude-opus-5-5 "
                                                  "through Antigravity at high (Jev's choice)."])

    def test_a_quota_one_route_stopped_at_leaves_the_models_other_routes(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.jev_answer = {"implementer": "claude-opus-5-5"}
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["providerInstanceId"], "claudeAgent")
        self.end(call, "failed", CLAUDE_STOP)
        (fresh,) = self.start()
        self.assertIn("claude-opus-5-5", self.jev_models)
        self.assertEqual((fresh["arguments"]["target"]["providerInstanceId"], fresh["arguments"]["target"]["model"]),
                         ("antigravity", "claude-opus-5-5"))

    def test_paid_routes_are_offered_only_while_no_subscription_route_has_capacity(self):
        self.t3_caches(goose=GOOSE_LIST + PAID_LIST, antigravity=ANTIGRAVITY_LIST, usage={"codex": SPENT})
        self.bind()
        self.jev_answer = {"implementer": "gemini-4-neon"}
        self.queue("Fix the login typo")
        self.start()
        self.assertEqual(set(self.jev_models), {"claude-opus-5-5", "claude-sonnet-5-5", "gemini-4-neon"},
                         "no paid route while a subscription has capacity")
        everything = {"codex": SPENT, "claudeAgent": SPENT, "antigravity": SPENT}
        self.t3_caches(goose=GOOSE_LIST + PAID_LIST, antigravity=ANTIGRAVITY_LIST, usage=everything)
        self.jev_answer = {"implementer": "qwen/qwen4-max"}
        self.assertTrue(self.orchestrator("queue_edit", plan="M30", tasks=[{"task": "Add a footer", "brief": "Add it."}])["ok"])
        (call,) = self.start()
        self.assertEqual(set(self.jev_models), {"claude-opus-5-5", "gpt-6.2-astra", "gemini-4-neon", "google/gemini-4-carbon",
                                                "qwen/qwen4-max"})
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "acpRegistry_goose", "model": "qwen/qwen4-max",
                                                       "options": {"provider": "openrouter", "thinking_effort": "high"}})
        self.assertEqual(self.last_done["tell"], [
            "Implementer for Add a footer (M30) started on qwen/qwen4-max at high (Jev's choice; paid through OpenRouter, "
            "as the subscription models are at their limits)."])
        self.jev_answer = {"implementer": "claude-opus-5-5"}
        self.assertTrue(self.orchestrator("queue_edit", plan="M30", tasks=[{"task": "Add a header", "brief": "Add it."}])["ok"])
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["model"], "anthropic/claude-opus-5.5")
        self.assertEqual(self.last_done["tell"], [
            "Implementer for Add a header (M30) started on claude-opus-5-5 through goose at high (Jev's choice; paid "
            "through OpenRouter, as the subscription models are at their limits)."])

    def test_the_default_reviewers_paid_route_comes_before_a_stand_in_only_once_every_subscription_is_spent(self):
        self.t3_caches(goose=GOOSE_LIST + PAID_LIST, antigravity=ANTIGRAVITY_LIST, usage={"codex": SPENT})
        self.jev_answer = {"implementer": "gpt-6.2-sol", "reviewer": "gemini-4-neon"}  # Sol is not an option: Codex is spent.
        _, review, _ = self.to_review()
        self.assertEqual(review["arguments"]["target"]["model"], "gemini-4-neon")
        self.assertIn("gpt-6.2-astra is at its usage limit, so a new reviewer runs on gemini-4-neon until it resets.",
                      self.last_step["tell"])
        self.t3_caches(goose=GOOSE_LIST + PAID_LIST, antigravity=ANTIGRAVITY_LIST,
                       usage={"codex": SPENT, "claudeAgent": SPENT, "antigravity": SPENT})
        self.assertTrue(self.orchestrator("restart", task="Fix the login typo", authorization="Restart the review.")["ok"])
        fresh = [c for c in self.start() if c["tool"] == "delegate_task"]
        self.assertEqual([c["arguments"]["target"]["model"] for c in fresh], ["openai/gpt-6.2-astra"])
        self.assertEqual(self.last_done["tell"], [
            "Reviewer for Fix the login typo (M30) started on gpt-6.2-astra through goose at high (the default; paid "
            "through OpenRouter, as the subscription models are at their limits)."])

    def test_a_picked_paid_model_runs_as_picked_without_the_paid_note(self):
        self.t3_caches(goose=GOOSE_LIST + PAID_LIST)
        self.bind(picks={"implementer": "qwen/qwen4-max"})
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["model"], "qwen/qwen4-max")
        self.assertEqual(self.last_done["tell"], ["Implementer for Fix the login typo (M30) started on qwen/qwen4-max at "
                                                  "xhigh (your choice)."])

    def test_without_jevs_answer_the_implementer_runs_on_opus_and_else_on_the_best_scored_option(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.jev_answer = {}
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "claudeAgent", "model": "claude-opus-5-5",
                                                       "options": {"effort": "xhigh"}})
        self.assertEqual(self.last_done["tell"], ["Implementer for Fix the login typo (M30) started on claude-opus-5-5 at "
                                                  "xhigh (the fallback, as Jev gave no answer)."])
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"claudeAgent": SPENT, "antigravity": SPENT})
        self.jev_answer = {"implementer": "made-up/model"}  # An option Jev was not offered counts as no answer.
        self.assertTrue(self.orchestrator("queue_edit", plan="M30", tasks=[{"task": "Add a footer", "brief": "Add it."}])["ok"])
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["model"], "gpt-6.2-astra", "the best scored of the options")
        self.assertEqual(self.last_done["tell"], ["Implementer for Add a footer (M30) started on gpt-6.2-astra at xhigh "
                                                  "(the best-scored model that could start, as Jev gave no answer)."])

    def test_a_merged_task_adds_its_counts_to_the_record_jev_reads_next(self):
        call, devops, _ = self.to_devops()
        job = self.worker("take", *devops)
        self.worker("update", *devops)
        self.green(job["head"])
        self.assertTrue(self.worker("merge", *devops)["ok"])
        kept = self.db.parent / "record.sqlite3"
        self.assertEqual(record.lines(kept, "implementer"),
                         {"gpt-6.2-astra": "small: 1 task, 1 approved first time, 0.0 review rounds on average"})
        self.assertEqual(record.lines(kept, "reviewer"), {"gemini-4-neon": "small: 1 task, 0.0 change requests on average"})
        self.assertTrue(self.orchestrator("queue_edit", plan="M30", tasks=[{"task": "Add a footer", "brief": "Add it."}])["ok"])
        self.start()
        self.assertEqual(self.jev_records["gpt-6.2-astra"],
                         "small: 1 task, 1 approved first time, 0.0 review rounds on average")
        self.assertIsNone(self.jev_records["claude-opus-5-5"], "no record yet")

    def test_a_dropped_task_counts_under_its_prs_last_size_and_a_quiet_worker_counts_too(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        worker, token, tree = self.implement(call, "export const name = 1;\n", 7, files=[f"src/{n}.ts" for n in range(13)])
        self.assertTrue(self.worker("pr", worker, token, "--pr", "7", path=tree)["ok"])
        self.assertTrue(self.orchestrator("queue_edit", drop="Fix the login typo")["ok"])
        self.assertEqual(record.lines(self.db.parent / "record.sqlite3", "implementer"),
                         {"gpt-6.2-astra": "large: 1 task, 0 approved first time, 0.0 review rounds on average, 1 dropped"})

    def test_a_record_that_cannot_be_written_fails_nothing_and_health_reports_it(self):
        with patch.object(record, "add", side_effect=OSError("disk full")):
            self.bind()
            self.queue("Fix the login typo")
            self.start()
            self.assertTrue(self.orchestrator("queue_edit", drop="Fix the login typo")["ok"])
        self.assertEqual(self.status()["tasks"][0]["state"], "dropped")
        kept = health.read(health.record(self.db.parent, "record"))
        self.assertEqual(kept["failures"], 1, "three in a row make it a problem in status")
        self.assertIn("disk full", kept["lastError"])
        engine = flow.Flow(self.db)
        try:
            self.assertEqual(engine.record, self.db.parent.resolve() / "record.sqlite3", "never the shared one in a test")
        finally:
            engine.close()

    def bound_before_0_10(self):
        """The binding as a version before GPT-6.2 Astra reviewed stored it: Opus 5.5 the reviewer's default, and Astra
        approved for the implementer alone."""
        self.stored_defaults(reviewer="claude-opus-5-5")
        self.stored_config(models={"gpt-6.2-astra": {"provider": "codex", "roles": ["implementer"]},
                                   "claude-opus-5-5": {"provider": "claudeAgent", "roles": ["reviewer", "devops"]}})

    def test_gpt_6_2_astra_is_the_default_reviewer_and_an_older_bindings_shipped_default_moves_to_it(self):
        self.t3_caches()
        self.assertEqual(self.orchestrator("models")["defaults"]["reviewer"],
                         {"model": "gpt-6.2-astra", "effort": "xhigh", "runs": True})
        self.bind()
        self.bound_before_0_10()
        self.assertEqual(self.status()["models"]["reviewer"],
                         {"model": "gpt-6.2-astra", "modelSource": "default", "effort": "xhigh", "effortSource": "default"})
        self.t3_caches(codex=None)
        self.assertEqual(self.status()["models"]["reviewer"]["model"], "claude-opus-5-5",
                         "it stays while T3 cannot run the new default")
        self.t3_caches()
        say = {"authorization": "Use Opus for the reviewer."}
        self.assertTrue(self.orchestrator("pick", role="reviewer", model="claude-opus-5-5", **say)["ok"])
        self.assertEqual(self.status()["models"]["reviewer"]["modelSource"], "pick", "the user's pick stays")

    def test_a_reviewer_of_the_implementers_family_runs_on_the_best_scored_reviewer_of_another_family(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        call, review, _ = self.to_review(picks={"reviewer": {"model": "gpt-6.2-sol", "effort": "ultra"}})
        self.assertEqual(call["arguments"]["target"]["model"], "gpt-6.2-astra")
        self.assertEqual(review["arguments"]["target"], {"providerInstanceId": "antigravity", "model": "gemini-4-neon",
                                                         "options": {"thought_level": "xhigh"}},
                         "the user's pick gives way, and so does the effort only that model has")
        self.assertEqual(self.last_done["tell"], ["Reviewer for Fix the login typo (M30) started on gemini-4-neon at xhigh "
                                                  "(a different family than the implementer's gpt-6.2-astra)."])

    def test_with_no_reviewer_of_another_family_that_can_run_the_reviewer_runs_as_it_would_have(self):
        self.t3_caches(usage={"claudeAgent": SPENT})  # No Antigravity, and every Claude model is at its limit.
        _, review, _ = self.to_review()
        self.assertEqual(review["arguments"]["target"]["model"], "gpt-6.2-astra")
        self.assertEqual(self.last_done["tell"],
                         ["Reviewer for Fix the login typo (M30) started on gpt-6.2-astra at xhigh (the default)."])

    def test_models_belong_to_their_makers_family_whichever_provider_runs_them(self):
        self.assertEqual([flow.family(m) for m in ("claude-opus-5-5", "anthropic/claude-sonnet-4", "gpt-6.2-astra",
                                                   "openai/gpt-x", "gemini-4-neon", "google/gemini-4-neon",
                                                   "qwen/qwen3.8-max", "made-up")],
                         ["Anthropic", "Anthropic", "OpenAI", "OpenAI", "Google", "Google", "qwen", "made-up"])

    def test_gemini_4_neon_runs_through_antigravity_in_its_effort_terms_and_no_longer_through_goose(self):
        goose_neon = {"slug": "google/gemini-4-neon", "capabilities": {"optionDescriptors": [{"id": "provider"}]}}
        self.t3_caches(goose=GOOSE_LIST + [goose_neon], antigravity=ANTIGRAVITY_LIST)
        self.jev_answer = {"implementer": "gemini-4-neon"}
        self.bind(picks={"implementer": {"effort": "max"}})
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertIn("gemini-4-neon", self.jev_models)
        self.assertNotIn("google/gemini-4-neon", self.jev_models)
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "antigravity", "model": "gemini-4-neon",
                                                       "options": {"thought_level": "xhigh"}}, "it has no max, so max goes as xhigh")

    def test_a_model_both_claude_and_antigravity_list_is_picked_through_claude(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.bind(picks={"implementer": "claude-opus-5-5"})
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["providerInstanceId"], "claudeAgent")

    def test_a_role_picked_at_setup_runs_on_that_model_whatever_jev_advises(self):
        self.t3_caches()
        self.bind(picks={"implementer": {"model": "anthropic/claude-sonnet-4", "effort": "medium"},
                         "reviewer": {"model": "qwen/qwen3.8-flash"}})
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        (call,) = self.orchestrator("next")["calls"]
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "acpRegistry_goose", "model": "anthropic/claude-sonnet-4",
                                                       "options": {"provider": "openrouter", "thinking_effort": "medium"}})

    def test_jev_is_not_asked_when_the_implementers_model_is_picked(self):
        self.t3_caches()
        self.bind(picks={"implementer": "qwen/qwen3.8-flash"})
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        self.assertTrue(self.orchestrator("next")["calls"])
        self.assertEqual(getattr(self, "jev_asked", 0), 0)

    def test_jev_is_asked_about_the_implementer_alone_so_the_reviewer_and_devops_run_on_their_own_models(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.jev_answer.update(devops="gemini-4-neon")  # The reviewer's answer stays Sonnet 5.5, not its default.
        _, review, tree = self.to_review()
        self.assertEqual(review["arguments"]["target"]["model"], "gemini-4-neon", "the implementer's Astra is OpenAI's")
        head = git(tree, "rev-parse", "HEAD")
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", head)["ok"])
        (devops,) = self.start()
        self.assertEqual(devops["arguments"]["target"]["model"], "claude-sonnet-5-5")
        self.assertEqual(self.jev_roles, ["implementer"])

    def test_a_reviewer_whose_model_is_at_its_usage_limit_runs_on_jevs_stand_in_from_another_provider(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.bind(picks={"reviewer": "claude-opus-5-5"})
        self.queue("Fix the login typo")
        (call,) = self.start()
        worker, token, tree = self.implement(call, "export const name = 1;\n", 7)
        self.assertTrue(self.worker("pr", worker, token, "--pr", "7", path=tree)["ok"])
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"claudeAgent": SPENT})
        self.jev_answer["reviewer"] = "gemini-4-neon"
        (review,) = self.start()
        self.assertEqual(review["arguments"]["target"], {"providerInstanceId": "antigravity", "model": "gemini-4-neon",
                                                         "options": {"thought_level": "xhigh"}})
        self.assertEqual(self.jev_roles, ["implementer", "reviewer"])
        self.assertFalse([m for m in self.jev_models if m.startswith("claude-")], "no Claude model has capacity")
        self.assertIn("claude-opus-5-5 is at its usage limit, so a new reviewer runs on gemini-4-neon until it resets.",
                      self.last_step["tell"])
        self.assertEqual(self.status()["models"]["reviewer"]["model"], "claude-opus-5-5", "the role's own model stays")

    def test_a_limit_that_has_reset_and_one_for_another_model_family_change_nothing(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"claudeAgent": [
            {"id": "five_hour", "usedPercent": 100, "resetsAt": "2020-01-01T00:00:00.000Z"},
            {"id": "seven_day", "usedPercent": 99, "resetsAt": "2999-01-01T00:00:00.000Z"},
            {"id": "seven_day_fable", "usedPercent": 100, "resetsAt": "2999-01-01T00:00:00.000Z"}]})
        self.assertEqual(flow.t3_limits(), {"claudeAgent": {"fable"}})
        self.jev_answer["reviewer"] = "gemini-4-neon"
        _, review, _ = self.to_review(picks={"reviewer": "claude-opus-5-5"})
        self.assertEqual(review["arguments"]["target"]["model"], "claude-opus-5-5")
        self.assertEqual(self.jev_roles, ["implementer"])

    def test_a_model_the_user_picked_is_stood_in_for_at_its_limit_and_runs_again_once_it_resets(self):
        spent = {"codex": [{"id": "primary", "usedPercent": 100, "resetsAt": "2999-01-01T00:00:00.000Z"}]}
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage=spent)
        self.bind(picks={"implementer": "gpt-6.2-astra"})
        self.jev_answer["implementer"] = "gemini-4-neon"
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "antigravity", "model": "gemini-4-neon",
                                                       "options": {"thought_level": "xhigh"}})
        self.assertFalse([m for m in self.jev_models if m.startswith("gpt-")], "no Codex model has capacity")
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.assertTrue(self.orchestrator("restart", task="Fix the login typo", authorization="Restart it.")["ok"])
        fresh = [c for c in self.start() if c["tool"] == "delegate_task"]
        self.assertEqual([c["arguments"]["target"]["model"] for c in fresh], ["gpt-6.2-astra"])

    def test_a_stand_in_runs_at_xhigh_when_the_users_effort_is_a_level_only_their_own_model_has(self):
        spent = {"codex": [{"id": "primary", "usedPercent": 100, "resetsAt": "2999-01-01T00:00:00.000Z"}]}
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.bind(picks={"implementer": {"model": "gpt-6.2-astra", "effort": "ultra"}})
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage=spent)
        self.jev_answer["implementer"] = "gemini-4-neon"
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "antigravity", "model": "gemini-4-neon",
                                                       "options": {"thought_level": "xhigh"}})

    def test_a_task_restarted_after_the_limit_reset_is_advised_afresh_and_leaves_its_stand_in(self):
        spent = {"codex": [{"id": "primary", "usedPercent": 100, "resetsAt": "2999-01-01T00:00:00.000Z"}]}
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage=spent)
        self.bind(picks={"reviewer": "claude-opus-5-5"})
        self.jev_answer["implementer"] = "gemini-4-neon"
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["arguments"]["target"]["model"], "gemini-4-neon")
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.jev_answer["implementer"] = "gpt-6.2-astra"
        self.assertTrue(self.orchestrator("restart", task="Fix the login typo", authorization="Restart it.")["ok"])
        fresh = [c for c in self.start() if c["tool"] == "delegate_task"]
        self.assertEqual([c["arguments"]["target"]["model"] for c in fresh], ["gpt-6.2-astra"])
        self.assertEqual(self.jev_roles, ["implementer", "implementer"])

    def test_an_implementer_stopped_by_its_models_quota_is_replaced_at_once_on_jevs_choice_among_models_with_capacity(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.jev_answer["implementer"] = "gemini-4-neon"
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.jev_answer["implementer"] = "gpt-6.2-astra"
        self.end(call, "failed", QUOTA_STOP)
        (fresh,) = self.start()
        self.assertEqual(fresh["tool"], "delegate_task", "no follow-up: it would stop at the same quota")
        self.assertNotEqual(fresh["worker"], call["worker"])
        self.assertEqual(fresh["arguments"]["target"]["model"], "gpt-6.2-astra")
        self.assertNotIn("gemini-4-neon", self.jev_models, "Jev chooses among the models with capacity")
        self.assertTrue(any("Fix the login typo" in line and "gemini-4-neon" in line for line in self.last_step["tell"]),
                        self.last_step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["implementing"])

    def test_an_implementer_stopped_by_its_quota_mid_fix_tells_its_successor_why_the_pr_came_back(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.jev_answer["implementer"] = "gemini-4-neon"
        _, review, tree = self.to_review()
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "changes",
                                    "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
        (fix,) = self.start()
        self.jev_answer["implementer"] = "gpt-6.2-astra"
        self.end(fix, "failed", QUOTA_STOP)
        (fresh,) = self.start()
        brief = fresh["arguments"]["task"]
        self.assertEqual(fresh["arguments"]["target"]["model"], "gpt-6.2-astra")
        self.assertIn("PR #7 already holds this task's work", brief)
        self.assertIn("It came back because: The reviewer asked for changes on PR #7.", brief)
        self.assertIn("gh pr view 7 --comments", brief)

    def test_a_reviewer_stopped_by_its_quota_moves_without_jevs_answer_to_the_best_scored_model_with_capacity(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        _, review, _ = self.to_review(picks={"reviewer": "claude-opus-5-5"})
        self.assertEqual(review["arguments"]["target"]["model"], "claude-opus-5-5")
        self.jev_answer = {}  # No Jev key: Jev never answers.
        self.end(review, "failed", CLAUDE_STOP)
        (fresh,) = self.start()
        self.assertEqual((fresh["tool"], fresh["arguments"]["role"]), ("delegate_task", "review"))
        self.assertEqual(fresh["arguments"]["target"]["model"], "gemini-4-neon", "the best average with capacity")
        self.assertIn("claude-opus-5-5 is at its usage limit, so a new reviewer runs on gemini-4-neon until it resets.",
                      self.last_step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["reviewing"])

    def test_a_devops_worker_stopped_by_its_quota_is_replaced_and_the_task_it_held_waits_for_the_new_one(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        _, review, tree = self.to_review()
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
        (devops,) = self.start()
        self.assertEqual(devops["arguments"]["target"]["model"], "claude-sonnet-5-5")
        self.assertEqual(self.worker("take", *worker_command(devops))["pr"], 7)
        self.jev_answer["devops"] = "gemini-4-neon"
        self.end(devops, "failed", CLAUDE_STOP)
        (fresh,) = self.start()
        self.assertEqual((fresh["tool"], fresh["arguments"]["role"]), ("delegate_task", "general"))
        self.assertNotEqual(fresh["worker"], devops["worker"])
        self.assertEqual(fresh["arguments"]["target"]["model"], "gemini-4-neon")
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["confirmed"], "the new DevOps worker takes it")

    def reset_comes(self):
        """The time a quota a run stopped at resets comes: its reset, and every wait for it, are behind us now."""
        past = "2020-01-01T00:00:00+00:00"
        db = store.connect(self.db)
        try:
            with db:
                state = json.loads(db.execute("SELECT state FROM projects WHERE id=?", (PROJECT,)).fetchone()[0])
                for limit in (state.get("limits") or {}).values():
                    limit["until"] = past
                for item in [*state["tasks"].values(), *state["devops"].values()]:
                    if item.get("held"):
                        item["held"] = past
                db.execute("UPDATE projects SET state=? WHERE id=?", (store.packed(state), PROJECT))
        finally:
            db.close()

    def reset_wakes(self):
        """One watcher pass over the work that waits for a quota to reset. Returns the messages it sent."""
        import watch
        sent = []
        engine = flow.Flow(self.db, hub=lambda repo: self.hub)
        try:
            watch.resets(engine, lambda thread, text: sent.append((thread, text)))
        finally:
            engine.close()
        return sent

    def test_with_no_model_left_with_capacity_the_work_waits_for_the_reset_and_the_watcher_wakes_it_then(self):
        # Neon is the only implementer with capacity.
        self.t3_caches(antigravity=ANTIGRAVITY_LIST[:1], usage={"codex": SPENT, "claudeAgent": SPENT})
        self.bind()
        self.jev_answer["implementer"] = "gemini-4-neon"
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.end(call, "failed", QUOTA_STOP)
        self.assertEqual(self.start(), [], "no worker starts on a model at its limit")
        self.assertTrue(any(line.startswith("Fix the login typo (M30) waits until") for line in self.last_step["tell"]),
                        self.last_step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["waiting"])
        self.assertEqual(self.start(), [])
        self.assertEqual(self.last_step["tell"], [], "the wait is told once")
        self.assertEqual(self.reset_wakes(), [], "the quota has not reset")
        self.reset_comes()
        (wake,) = self.reset_wakes()
        self.assertEqual(wake[0], ORCH)
        self.assertTrue(wake[1].startswith("use t3 workflow:"), wake)
        self.assertEqual(self.reset_wakes(), [], "one wake per reset")
        (fresh,) = self.start()
        self.assertEqual((fresh["tool"], fresh["arguments"]["target"]["model"]), ("delegate_task", "gemini-4-neon"))
        self.assertEqual(self.reset_wakes(), [], "nothing waits now")

    def test_a_task_dropped_while_it_waits_for_a_reset_wakes_nobody(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST[:1], usage={"codex": SPENT, "claudeAgent": SPENT})
        self.bind()
        self.jev_answer["implementer"] = "gemini-4-neon"
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.end(call, "failed", QUOTA_STOP)
        self.start()
        self.assertTrue(self.orchestrator("queue_edit", drop="Fix the login typo")["ok"])
        self.reset_comes()
        self.assertEqual(self.reset_wakes(), [])

    def test_a_restart_during_a_limit_starts_the_new_worker_on_a_stand_in(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        _, review, _ = self.to_review(picks={"reviewer": "claude-opus-5-5"})
        self.assertEqual(review["arguments"]["target"]["model"], "claude-opus-5-5")
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"claudeAgent": SPENT})
        self.jev_answer["reviewer"] = "gemini-4-neon"
        self.assertTrue(self.orchestrator("restart", task="Fix the login typo", authorization="It is stuck at its limit.")["ok"])
        fresh = [c for c in self.start() if c["tool"] == "delegate_task"]
        self.assertEqual([c["arguments"]["target"] for c in fresh], [{"providerInstanceId": "antigravity", "model": "gemini-4-neon",
                                                                     "options": {"thought_level": "xhigh"}}])

    def test_with_no_other_provider_to_run_on_the_worker_keeps_its_model_and_jev_is_not_asked(self):
        self.t3_caches(goose=None, codex=None, usage={"claudeAgent": SPENT})
        _, review, _ = self.to_review(picks={"implementer": "claude-sonnet-5-5", "reviewer": "claude-opus-5-5"})
        self.assertEqual(review["arguments"]["target"]["model"], "claude-opus-5-5")
        self.assertEqual(getattr(self, "jev_roles", []), [])

    def test_devops_at_its_usage_limit_runs_on_a_stand_in(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        _, review, tree = self.to_review()
        head = git(tree, "rev-parse", "HEAD")
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", head)["ok"])
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"claudeAgent": SPENT})
        self.jev_answer["devops"] = "gemini-4-neon"
        (devops,) = self.start()
        self.assertEqual(devops["arguments"]["target"], {"providerInstanceId": "antigravity", "model": "gemini-4-neon",
                                                         "options": {"thought_level": "high"}})
        self.assertIn("claude-sonnet-5-5 is at its usage limit, so a new DevOps runs on gemini-4-neon until it resets.",
                      self.last_step["tell"])

    def test_an_effort_the_user_sets_alone_holds_and_jev_still_picks_the_model(self):
        self.jev_answer["implementer"] = "gpt-6.2-sol"
        self.bind(picks={"implementer": {"effort": "max"}})
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        (call,) = self.orchestrator("next")["calls"]
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "codex", "model": "gpt-6.2-sol",
                                                       "options": {"reasoningEffort": "max", "serviceTier": "default"}})

    def test_a_pick_t3_does_not_list_is_refused_and_nothing_is_bound(self):
        self.t3_caches()
        refused = self.orchestrator("init", workspace=str(self.workspace), repo=REPO, requiredChecks=["acceptance"], checks=[],
                                    noChecksReason="Test project", picks={"reviewer": "made-up/model"})
        self.assertFalse(refused["ok"])
        self.assertIn("T3 cannot run made-up/model", refused["error"])
        self.assertFalse(self.orchestrator("status")["ok"], "the refused setup bound nothing")

    def test_the_user_changes_a_roles_model_later_and_a_started_worker_keeps_its_own(self):
        self.t3_caches()
        self.bind()
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        self.orchestrator("next")
        self.assertFalse(self.orchestrator("pick", role="implementer", model="qwen/qwen3.8-flash")["ok"], "it needs the user's words")
        changed = self.orchestrator("pick", role="implementer", model="qwen/qwen3.8-flash", authorization="Use qwen for the implementer.")
        self.assertTrue(changed["ok"], changed)
        (call,) = self.orchestrator("next")["calls"]
        self.assertEqual(call["arguments"]["target"]["model"], "gpt-6.2-astra", "the started implementer keeps Jev's model")
        self.assertEqual(self.orchestrator("status")["models"]["implementer"],
                         {"model": "qwen/qwen3.8-flash", "modelSource": "pick", "effort": "xhigh", "effortSource": "default"})
        self.assertTrue(self.orchestrator("pick", role="implementer", model=None, authorization="Back to the default.")["ok"])
        self.assertEqual(self.orchestrator("status")["models"]["implementer"],
                         {"model": None, "modelSource": "jev", "effort": "xhigh", "effortSource": "default"})

    def test_the_user_sets_a_roles_model_and_effort_apart_and_puts_each_back(self):
        self.t3_caches()
        self.bind()
        say = {"authorization": "Use max for the reviewer."}
        reviewer = lambda: self.status()["models"]["reviewer"]  # noqa: E731
        self.assertEqual(self.orchestrator("pick", role="reviewer", effort="max", **say)["effort"], "max")
        self.assertEqual(reviewer(), {"model": "gpt-6.2-astra", "modelSource": "default", "effort": "max", "effortSource": "pick"})
        self.assertTrue(self.orchestrator("pick", role="reviewer", model="claude-sonnet-5-5", **say)["ok"])
        self.assertEqual(reviewer(), {"model": "claude-sonnet-5-5", "modelSource": "pick", "effort": "max", "effortSource": "pick"})
        self.assertTrue(self.orchestrator("pick", role="reviewer", model="default", **say)["ok"])
        self.assertEqual(reviewer(), {"model": "gpt-6.2-astra", "modelSource": "default", "effort": "max", "effortSource": "pick"})
        self.assertTrue(self.orchestrator("pick", role="reviewer", effort="default", **say)["ok"])
        self.assertEqual(reviewer()["effortSource"], "default")
        self.assertFalse(self.orchestrator("pick", role="reviewer", **say)["ok"], "a change names a model or an effort")

    def test_an_effort_is_one_its_model_offers_and_one_set_alone_is_one_every_model_has(self):
        self.t3_caches()
        self.bind(picks={"implementer": {"model": "gpt-6.1-sol", "effort": "ultra"}})
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        (call,) = self.orchestrator("next")["calls"]
        self.assertEqual(call["arguments"]["target"]["options"], {"reasoningEffort": "ultra", "serviceTier": "default"})
        say = {"authorization": "Change the efforts."}
        alone = self.orchestrator("pick", role="reviewer", effort="ultra", **say)
        self.assertIn("pick the model with it", alone.get("error", ""))
        kept = self.orchestrator("pick", role="implementer", model="claude-sonnet-5-5", **say)  # its ultra is Codex's alone
        self.assertIn("claude-sonnet-5-5 takes the effort low, medium, high, xhigh, max, ultrathink, not ultra",
                      kept.get("error", ""))
        goose = self.orchestrator("pick", role="implementer", model="anthropic/claude-sonnet-4", effort="xhigh", **say)
        self.assertIn("takes the effort low, medium, high, max, not xhigh", goose.get("error", ""))
        self.assertEqual(self.status()["models"]["implementer"]["effort"], "ultra", "a refused change changes nothing")

    def test_an_openrouter_worker_gets_only_the_options_its_model_declares(self):
        # T3's goose list (2026-11-16): most OpenRouter models declare `provider` and `mode`, not `thinking_effort`,
        # so the effort has no option to go in.
        self.t3_caches()
        self.bind(picks={"implementer": "qwen/qwen3.8-max"})
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        (call,) = self.orchestrator("next")["calls"]
        self.assertEqual(call["arguments"]["target"], {"providerInstanceId": "acpRegistry_goose",
                                                       "model": "qwen/qwen3.8-max", "options": {"provider": "openrouter"}})

    def test_an_openrouter_model_that_declares_thinking_gets_the_effort_in_its_own_terms(self):
        # Its choices are off, low, medium, high and max: there is no xhigh.
        self.t3_caches()
        self.bind(picks={"implementer": "anthropic/claude-sonnet-4"})  # At the implementer's default, xhigh.
        self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo"}]}])
        (call,) = self.orchestrator("next")["calls"]
        self.assertEqual(call["arguments"]["target"]["options"], {"provider": "openrouter", "thinking_effort": "high"})

    # Helpers that play the workers and T3.

    def queue(self, *tasks, owns=("src/",)):
        """The orchestrator queues plan M30; more than one task carries the user's words to skip the spec."""
        self.assertTrue(self.orchestrator("queue_set", plans=[{"plan": "M30", "owns": list(owns), "tasks": [
            {"task": t, "brief": f"Do {t}."} if isinstance(t, str) else t for t in tasks]}],
            **({"noSpec": "No spec for these."} if len(tasks) > 1 else {}))["ok"])

    def start(self, tool="delegate_task"):
        """next, then T3 runs each call: the orchestrator confirms them with done. Returns the calls."""
        step = self.orchestrator("next")
        self.assertTrue(step["ok"], step)
        results = []
        self.runs = getattr(self, "runs", {})
        for call in step["calls"]:
            self.t3_task = getattr(self, "t3_task", 0) + 1
            self.runs[call["call"]] = f"task-{self.t3_task}"
            results.append({"call": call["call"], "taskId": f"task-{self.t3_task}", "childThreadId": f"child-{call['worker']}"}
                           if call["tool"] in {"delegate_task", "t3_thread_send"} else {"call": call["call"]})
        self.last_done = None
        if results:
            done = self.orchestrator("done", results=results)
            self.assertTrue(done["ok"], done)
            self.last_done = done
        self.last_step = step
        return step["calls"]

    def implement(self, call, content, number, files=("src/login.ts",)):
        """The implementer's work in its own worktree: commit, push, open the PR. Returns the worktree."""
        worker, token = worker_command(call)
        tree = Path(self.tmp.name) / f"wt-{worker}"
        if not tree.exists():
            git(self.workspace, "worktree", "add", "-b", f"t3/{worker}", str(tree), "origin/main")
        (tree / "src" / "login.ts").write_text(content)
        git(tree, "commit", "-am", f"work {number}")
        git(tree, "push", "-u", "origin", "HEAD")
        self.hub.open(number, f"t3/{worker}", list(files))
        return worker, token, tree

    def worker(self, command, worker, token, *args, path=None):
        argv = ["--db", str(self.db), "worker", command, "--worker", worker, "--token", token, *args]
        if path:
            argv += ["--path", str(path)]
        return harness.run(argv, hub=lambda repo: self.hub, jev=self.advise)

    def status(self):
        result = self.orchestrator("status")
        self.assertTrue(result["ok"], result)
        return result

    def stored_defaults(self, **defaults):
        """The binding's defaults as an older version stored them."""
        db = store.connect(self.db)
        try:
            with db:
                config = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (PROJECT,)).fetchone()[0])
                config["defaults"].update(defaults)
                db.execute("UPDATE projects SET config=? WHERE id=?", (store.packed(config), PROJECT))
        finally:
            db.close()

    def test_each_confirmed_start_names_its_role_task_model_effort_and_why_once(self):
        call, review, tree = self.to_review()
        self.assertEqual(self.last_done["tell"],
                         ["Reviewer for Fix the login typo (M30) started on gemini-4-neon at xhigh (a different "
                          "family than the implementer's gpt-6.2-astra)."])
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
        self.start()
        self.assertEqual(self.last_done["tell"], ["DevOps for plan M30 started on claude-sonnet-5-5 at high (the default)."])
        again = self.orchestrator("done", results=[{"call": review["call"], "taskId": "task-9", "childThreadId": "c"}])
        self.assertEqual((again["tell"], again["unknown"]), ([], [review["call"]]), "a start is told once")

    def test_the_implementers_start_says_whose_choice_its_model_was(self):
        self.bind()
        self.queue("Fix the login typo")
        step = self.orchestrator("next")
        self.assertFalse(any("started on" in line for line in step["tell"]), "nothing is told before T3 confirms the start")
        self.start()
        self.assertEqual(self.last_done["tell"],
                         ["Implementer for Fix the login typo (M30) started on gpt-6.2-astra at xhigh (Jev's choice)."])
        self.jev_answer = {}  # Jev gives no answer.
        self.assertTrue(self.orchestrator("queue_edit", plan="M30", tasks=[{"task": "Add a footer", "brief": "Add it."}])["ok"])
        self.start()
        self.assertEqual(self.last_done["tell"], ["Implementer for Add a footer (M30) started on claude-opus-5-5 at xhigh "
                                                  "(the fallback, as Jev gave no answer)."])

    def test_a_picked_model_and_a_stand_in_say_so_when_they_start(self):
        self.t3_caches(antigravity=ANTIGRAVITY_LIST)
        self.bind(picks={"implementer": {"model": "gpt-6.2-sol", "effort": "high"}})
        self.queue("Fix the login typo")
        self.start()
        self.assertEqual(self.last_done["tell"],
                         ["Implementer for Fix the login typo (M30) started on gpt-6.2-sol at high (your choice)."])
        self.t3_caches(antigravity=ANTIGRAVITY_LIST, usage={"codex": SPENT})
        self.jev_answer["implementer"] = "gemini-4-neon"
        self.assertTrue(self.orchestrator("restart", task="Fix the login typo", authorization="Restart it.")["ok"])
        self.start()
        self.assertIn("gpt-6.2-sol is at its usage limit, so a new implementer runs on gemini-4-neon until it resets.",
                      self.last_step["tell"])
        self.assertEqual(self.last_done["tell"], ["Implementer for Fix the login typo (M30) started on gemini-4-neon at "
                                                  "high (a stand-in for gpt-6.2-sol)."])

    def test_a_confirmed_start_is_not_handed_out_again(self):
        self.bind()
        self.queue("Fix the login typo")
        self.start()
        self.assertEqual(self.orchestrator("next")["calls"], [])

    def test_a_pull_request_whose_checks_pass_goes_to_a_fresh_reviewer_in_the_implementers_checkout(self):
        self.bind(checks=[{"name": "lint", "argv": [sys.executable, "-c", "print('lint ok')"]}])
        self.queue("Fix the login typo")
        (call,) = self.start()
        worker, token, tree = self.implement(call, "export const name = 1;\n", 7)
        result = self.worker("pr", worker, token, "--pr", "7", path=tree)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "review_needed")
        (review,) = self.start()
        arguments = review["arguments"]
        self.assertEqual((arguments["isolation"], arguments["role"]), ("shared", "review"))
        self.assertEqual(arguments["target"]["model"], "gemini-4-neon", "not the family of the implementer's Astra")
        head = git(tree, "rev-parse", "HEAD")
        self.assertIn(head, arguments["task"])
        self.assertIn(str(tree.resolve()).replace("\\", "/"), arguments["task"])
        self.assertIn("lint: passed", arguments["task"])
        self.assertIn(f"run it in {str(tree.resolve()).replace(chr(92), '/')} and try what the task changed, as a user "
                      f"would. Something that fails when tried is a blocking finding.", arguments["task"])
        self.assertIn("say what you tried and what happened, or why you could not run it", arguments["task"])

    def test_a_project_whose_t3_id_a_folder_name_cannot_hold_still_runs_its_checks(self):
        # T3 gave a project an id of the form mcp:<uuid> (2026-11-23), and worker pr crashed making its log folder.
        with patch.object(sys.modules[__name__], "PROJECT", "mcp:00000000-1111-4222-8333-444444444444"):
            self.bind(checks=[{"name": "lint", "argv": [sys.executable, "-c", "print('lint ok')"]}])
            self.queue("Fix the login typo")
            (call,) = self.start()
            worker, token, tree = self.implement(call, "export const name = 1;\n", 7)
            result = self.worker("pr", worker, token, "--pr", "7", path=tree)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "review_needed")
        (folder,) = (self.db.parent / "harness-artifacts").iterdir()
        self.assertRegex(folder.name, r"^[A-Za-z0-9._-]+$", "only characters every system takes in a folder name")

    def test_a_pull_request_whose_checks_fail_is_refused_with_the_output(self):
        self.bind(checks=[{"name": "unit", "argv": [sys.executable, "-c", "import sys; print('2 failed'); sys.exit(1)"]}])
        self.queue("Fix the login typo")
        (call,) = self.start()
        worker, token, tree = self.implement(call, "export const name = 1;\n", 7)
        result = self.worker("pr", worker, token, "--pr", "7", path=tree)
        self.assertFalse(result["ok"])
        self.assertIn("unit", result["error"])
        self.assertIn("2 failed", result["error"])
        self.assertEqual(self.orchestrator("next")["calls"], [])

    def test_a_pull_request_without_a_decisions_section_is_refused_with_how_to_add_one(self):
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        brief = call["arguments"]["task"]
        self.assertIn("and a Decisions section", brief)
        self.assertIn("\"## Decisions\" section: each call this brief left open", brief)
        worker, token, tree = self.implement(call, "export const name = 1;\n", 7)
        for body in ("Renames nmae to name.", "Decisions were easy.", "## Decisions\n\n## Tests\nRan them."):
            self.hub.prs[7]["body"] = body
            result = self.worker("pr", worker, token, "--pr", "7", path=tree)
            self.assertFalse(result["ok"], body)
            self.assertIn("no Decisions section", result["error"])
            self.assertIn("gh pr edit 7 --body-file", result["error"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["implementing"])
        self.hub.prs[7]["body"] = "Renames nmae to name.\r\n\r\n**Decisions:**\r\n- Kept the old export too, for callers."
        result = self.worker("pr", worker, token, "--pr", "7", path=tree)
        self.assertTrue(result["ok"], result)
        (review,) = self.start()
        self.assertIn("Check each decision in the PR body's Decisions section", review["arguments"]["task"])

    def test_a_decisions_section_reads_in_the_forms_a_pr_body_writes_it(self):
        for body in ("## Decisions\nNone", "Decisions: None", "### decisions\n\n- Chose v2: the brief names it.",
                     "**Decisions**\nNone"):
            self.assertTrue(flow.has_decisions(body), body)
        for body in ("", None, "## Decisions", "## Decisions\n## Testing\nok", "Decisions were easy."):
            self.assertFalse(flow.has_decisions(body), body)

    def test_a_worker_command_with_a_wrong_token_is_refused(self):
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        worker, _, tree = self.implement(call, "export const name = 1;\n", 7)
        result = self.worker("pr", worker, "0" * 32, "--pr", "7", path=tree)
        self.assertFalse(result["ok"])
        self.assertIn("token", result["error"])

    def test_a_worker_reports_when_an_earlier_project_has_a_worker_with_the_same_id(self):
        # Worker ids are counters per project, so two bound projects share them; the token tells them apart.
        other = Path(self.tmp.name) / "other"
        git(Path(self.tmp.name), "clone", str(self.origin), str(other))
        first = {"projectId": "proj-0", "threadId": "thread-other"}
        self.assertTrue(self.call({"op": "init", **first, "workspace": str(other), "repo": "owner/other",
                                   "requiredChecks": ["acceptance"], "checks": [], "noChecksReason": "Test project"})["ok"])
        self.assertTrue(self.call({"op": "control", **first, "action": "activate",
                                   "authorization": "Activate the workflow."})["ok"])
        self.assertTrue(self.call({"op": "queue_set", **first, "plans": [{"plan": "M1", "owns": ["src/"], "tasks": [
            {"task": "Another task", "brief": "Do another task."}]}]})["ok"])
        (theirs,) = self.call({"op": "next", **first})["calls"]
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertEqual(call["worker"], theirs["worker"])
        worker, token, tree = self.implement(call, "export const name = 1;\n", 7)
        result = self.worker("pr", worker, token, "--pr", "7", path=tree)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "review_needed")
        wrong = self.worker("pr", worker, "0" * 32, "--pr", "7", path=tree)
        self.assertFalse(wrong["ok"])
        self.assertIn("token", wrong["error"])

    def to_review(self, content="export const name = 1;\n", number=7, files=("src/login.ts",), owns=("src/",), devops=None,
                  **binding):
        """One task queued, implemented and in review. Returns (implementer call, reviewer call, worktree)."""
        self.bind(**({} if devops is None else {"devops": devops}), **binding)
        self.queue("Fix the login typo", owns=owns)
        (call,) = self.start()
        worker, token, tree = self.implement(call, content, number, files)
        self.assertTrue(self.worker("pr", worker, token, "--pr", str(number), path=tree)["ok"])
        (review,) = self.start()
        return call, review, tree

    def test_an_approval_posts_t3_review_on_that_commit_and_the_rule_confirms_it_for_devops(self):
        _, review, tree = self.to_review()
        head = git(tree, "rev-parse", "HEAD")
        result = self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", head)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.hub.posted, [(head, "t3/review", "success")])
        (devops,) = self.start()
        arguments = devops["arguments"]
        self.assertEqual((arguments["isolation"], arguments["role"]), ("worktree", "general"))
        self.assertEqual(arguments["target"], {"providerInstanceId": "claudeAgent", "model": "claude-sonnet-5-5",
                                               "options": {"effort": "high"}})
        self.assertIn("worker take", arguments["task"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["confirmed"])

    def test_an_approval_of_a_commit_that_is_no_longer_the_head_is_refused(self):
        _, review, _ = self.to_review()
        result = self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", "0" * 40)
        self.assertFalse(result["ok"])
        self.assertEqual(self.hub.posted, [])

    def test_requested_changes_go_back_to_the_same_implementer_and_its_next_push_gets_a_fresh_reviewer(self):
        call, review, tree = self.to_review()
        head = git(tree, "rev-parse", "HEAD")
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "changes", "--sha", head)["ok"])
        self.assertEqual(self.hub.posted, [])
        (fix,) = self.start()
        implementer = worker_command(call)[0]
        self.assertEqual(fix["tool"], "t3_thread_send")
        self.assertEqual(fix["arguments"]["threadId"], f"child-{implementer}")
        self.assertIn("asked for changes", fix["arguments"]["message"])
        self.assertIn("worker pr", fix["arguments"]["message"])
        worker, token, tree = self.implement(call, "export const name = 'fixed';\n", 7)
        self.assertTrue(self.worker("pr", worker, token, "--pr", "7", path=tree)["ok"])
        (second,) = self.start()
        self.assertEqual(second["tool"], "delegate_task")
        self.assertNotEqual(worker_command(second)[0], worker_command(review)[0])

    def test_a_second_review_starts_from_the_first_one(self):
        call, review, tree = self.to_review()
        first = git(tree, "rev-parse", "HEAD")
        self.assertNotIn("reviewed before", review["arguments"]["task"])
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "changes", "--sha", first)["ok"])
        self.start()
        worker, token, tree = self.implement(call, "export const name = 'fixed';\n", 7)
        self.assertTrue(self.worker("pr", worker, token, "--pr", "7", path=tree)["ok"])
        (second,) = self.start()
        brief = second["arguments"]["task"]
        self.assertIn(f"This PR was reviewed before: changes requested on {first[:12]}.", brief)
        self.assertIn("It came back because: The reviewer asked for changes on PR #7.", brief)
        self.assertIn(f"gh pr view 7 --repo {REPO} --comments", brief)
        self.assertIn(f"diff {first} {git(tree, 'rev-parse', 'HEAD')}", brief)

    def to_devops(self, **kwargs):
        """One task approved and confirmed, DevOps started. Returns (implementer call, devops worker and token, worktree)."""
        call, review, tree = self.to_review(**kwargs)
        head = git(tree, "rev-parse", "HEAD")
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", head)["ok"])
        (devops,) = self.start()
        return call, worker_command(devops), tree

    def green(self, sha):
        self.hub.checks_on[sha] = {"acceptance": "success"}

    def test_devops_takes_the_confirmed_task_merges_it_after_ci_and_the_project_folder_moves_forward(self):
        _, devops, tree = self.to_devops()
        job = self.worker("take", *devops)
        self.assertTrue(job["ok"], job)
        self.assertEqual((job["job"], job["pr"]), ("merge", 7))
        self.assertEqual(self.worker("update", *devops)["result"], "current")
        self.green(job["head"])
        merged = self.worker("merge", *devops)
        self.assertTrue(merged["ok"], merged)
        self.assertEqual(self.hub.merged, [7])
        self.assertEqual(git(self.workspace, "rev-parse", "main"), git(self.origin, "rev-parse", "main"))
        self.assertEqual((self.workspace / "src" / "login.ts").read_text(), "export const name = 1;\n")
        step = self.orchestrator("next")
        self.assertIn("Merged PR #7: Fix the login typo (M30).", step["tell"])
        self.assertIn("Plan M30 done: 1 merged.", step["tell"])
        self.assertEqual(self.status()["plans"], {"M30": "done"})

    def test_a_merge_waits_for_every_required_check_and_the_review_on_the_head(self):
        _, devops, _ = self.to_devops()
        job = self.worker("take", *devops)
        self.hub.checks_on[job["head"]] = {"acceptance": "pending"}
        refused = self.worker("merge", *devops)
        self.assertFalse(refused["ok"])
        self.assertIn("acceptance", refused["error"])
        self.assertEqual(self.hub.merged, [])

    def test_with_nothing_confirmed_take_ends_the_turn_and_new_work_wakes_devops_with_a_follow_up(self):
        self.bind()
        self.queue("Fix the login typo", {"task": "Add a footer", "brief": "Add it.", "after": []})
        first, second = self.start()
        w1, t1, tree1 = self.implement(first, "export const name = 1;\n", 7)
        self.assertTrue(self.worker("pr", w1, t1, "--pr", "7", path=tree1)["ok"])
        (review,) = self.start()
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree1, "rev-parse", "HEAD"))["ok"])
        (devops_call,) = self.start()
        devops = worker_command(devops_call)
        self.worker("take", *devops)
        self.green(git(tree1, "rev-parse", "HEAD"))
        self.assertTrue(self.worker("merge", *devops)["ok"])
        nothing = self.worker("take", *devops)
        self.assertIsNone(nothing["job"])
        self.assertEqual(self.orchestrator("next")["calls"], [])
        w2, t2, tree2 = self.implement(second, "export const footer = 1;\n", 8)
        self.assertTrue(self.worker("pr", w2, t2, "--pr", "8", path=tree2)["ok"])
        (review2,) = self.start()
        self.assertTrue(self.worker("review", *worker_command(review2), "--verdict", "approve",
                                    "--sha", git(tree2, "rev-parse", "HEAD"))["ok"])
        (wake,) = self.start()
        self.assertEqual((wake["tool"], wake["arguments"]["threadId"]), ("t3_thread_send", f"child-{devops[0]}"))
        self.assertIn("worker take", wake["arguments"]["message"])

    def push_main(self, path, content):
        """Someone else's change lands on origin's main."""
        clone = self.hub.helper()
        git(clone, "checkout", "-B", "main", "origin/main")
        (clone / path).parent.mkdir(parents=True, exist_ok=True)
        (clone / path).write_text(content)
        git(clone, "add", ".")
        git(clone, "commit", "-m", f"main: {path}")
        git(clone, "push", "origin", "main")

    def test_when_main_moved_on_other_files_update_carries_the_approval_and_the_checkout_follows(self):
        _, devops, tree = self.to_devops()
        self.push_main("src/other.ts", "export const other = 1;\n")
        job = self.worker("take", *devops)
        updated = self.worker("update", *devops)
        self.assertEqual(updated["result"], "updated", updated)
        head = updated["head"]
        self.assertNotEqual(head, job["head"])
        self.assertIn((head, "t3/review", "success"), self.hub.posted)
        self.assertEqual(git(tree, "rev-parse", "HEAD"), head)
        self.green(head)
        self.assertTrue(self.worker("merge", *devops)["ok"])
        self.assertTrue((self.workspace / "src" / "other.ts").exists())

    def test_when_the_change_reads_differently_after_the_update_it_goes_back_for_another_look(self):
        call, devops, _ = self.to_devops()
        self.push_main("src/login.ts", "export const name = 1;\n")  # main already holds the same change
        self.worker("take", *devops)
        self.assertEqual(self.worker("update", *devops)["result"], "updated")
        self.assertEqual(len(self.hub.posted), 1, "the approval is not carried")
        (fix,) = self.start()
        self.assertEqual(fix["arguments"]["threadId"], f"child-{worker_command(call)[0]}")
        self.assertIn("git pull", fix["arguments"]["message"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["implementing"])

    def test_a_conflict_goes_back_to_the_implementer_and_its_resolution_gets_a_fresh_review(self):
        call, devops, tree = self.to_devops()
        self.push_main("src/login.ts", "export const a = 2;\n")
        self.worker("take", *devops)
        self.assertEqual(self.worker("update", *devops)["result"], "conflict")
        (fix,) = self.start()
        self.assertIn("git merge origin/main", fix["arguments"]["message"])
        git(tree, "fetch", "origin")
        git(tree, "merge", "origin/main", check=False)
        (tree / "src" / "login.ts").write_text("export const name = 2;\n")
        git(tree, "commit", "-am", "resolve")
        git(tree, "push")
        self.hub.open(7, f"t3/{worker_command(call)[0]}", ["src/login.ts"])
        self.assertTrue(self.worker("pr", *worker_command(call), "--pr", "7", path=tree)["ok"])
        (review,) = self.start()
        self.assertIn(git(tree, "rev-parse", "HEAD"), review["arguments"]["task"])
        self.assertIn("This PR was reviewed before: approved on", review["arguments"]["task"])
        self.assertIn("It came back because: PR #7 conflicts with main.", review["arguments"]["task"])

    def test_a_ci_failure_in_the_tasks_code_goes_back_to_its_implementer(self):
        call, devops, _ = self.to_devops()
        job = self.worker("take", *devops)
        self.worker("update", *devops)
        self.hub.checks_on[job["head"]] = {"acceptance": "failure"}
        refused = self.worker("merge", *devops)
        self.assertIn("acceptance is failure", refused["error"])
        self.assertTrue(self.worker("failed", *devops, "--cause", "task", "--detail", "The login test fails")["ok"])
        (fix,) = self.start()
        self.assertEqual(fix["arguments"]["threadId"], f"child-{worker_command(call)[0]}")
        self.assertIn("The login test fails", fix["arguments"]["message"])
        self.assertIn("gh run view", fix["arguments"]["message"])

    def test_a_failure_seen_before_devops_takes_the_task_is_handed_over_for_triage(self):
        _, review, tree = self.to_review()
        head = git(tree, "rev-parse", "HEAD")
        self.hub.checks_on[head] = {"acceptance": "failure"}
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", head)["ok"])
        (devops_call,) = self.start()
        job = self.worker("take", *worker_command(devops_call))
        self.assertEqual((job["job"], job["failed"]), ("triage", ["acceptance"]))

    # CI off: when GitHub cannot run CI (its billing stopped Actions), a PR merges on its local checks and review.

    def test_with_ci_off_a_pr_whose_ci_never_ran_merges_on_its_review(self):
        _, review, tree = self.to_review()
        head = git(tree, "rev-parse", "HEAD")
        self.hub.checks_on[head] = {"acceptance": "failure"}  # The job never started: GitHub billing.
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", head)["ok"])
        (devops_call,) = self.start()
        devops = worker_command(devops_call)
        self.assertFalse(self.orchestrator("ci", on=False)["ok"], "it needs the user's words")
        off = self.orchestrator("ci", on=False, authorization="Turn CI off: GitHub billing stopped Actions.")
        self.assertTrue(off["ok"], off)
        (task,) = self.status()["tasks"]
        self.assertEqual((off["ci"], off["changed"], off["freed"]), (False, ["required checks of main"], [task["id"]]))
        self.assertEqual(self.hub.protection, [{"context": "t3/review"}])
        self.assertFalse(self.status()["ci"])
        self.assertEqual(self.worker("take", *devops)["job"], "merge")
        self.assertIn("CI is off", self.worker("update", *devops)["next"])
        merged = self.worker("merge", *devops)
        self.assertTrue(merged["ok"], merged)
        self.assertEqual(self.hub.merged, [7])

    def test_with_ci_off_a_task_held_for_a_pipeline_fix_merges_and_devops_cannot_hold_another(self):
        _, devops, _ = self.to_devops()
        job = self.worker("take", *devops)
        self.hub.checks_on[job["head"]] = {"acceptance": "failure"}
        held = self.worker("failed", *devops, "--cause", "pipeline", "--detail", "GitHub billing: the job never started")
        self.assertEqual(held["state"], "pipeline_wait")
        off = self.orchestrator("ci", on=False, authorization="Turn CI off.")
        self.assertEqual(off["freed"], [job["task"]])
        self.assertEqual(self.worker("take", *devops)["job"], "merge")
        refused = self.worker("failed", *devops, "--cause", "pipeline", "--detail", "Still red")
        self.assertIn("CI is off", refused["error"])
        self.worker("update", *devops)
        self.assertTrue(self.worker("merge", *devops)["ok"])
        self.assertEqual(self.hub.merged, [7])

    def test_without_devops_ci_off_merges_once_the_review_is_on_the_head(self):
        _, review, tree = self.to_review(devops=False)
        self.assertTrue(self.orchestrator("ci", on=False, authorization="Turn CI off.")["ok"])
        self.approve(review, tree)
        self.start()
        self.start()
        self.assertEqual(self.hub.merged, [7])

    def test_turning_ci_back_on_requires_the_ci_checks_again(self):
        _, devops, _ = self.to_devops()
        self.assertTrue(self.orchestrator("ci", on=False, authorization="Turn CI off.")["ok"])
        on = self.orchestrator("ci", on=True, authorization="Billing is fixed: turn CI on.")
        self.assertEqual((on["ci"], on["freed"]), (True, []))
        self.assertEqual(self.hub.protection, [{"context": "t3/review"}, {"context": "acceptance", "app_id": 15368}])
        self.assertTrue(self.status()["ci"])
        self.worker("take", *devops)
        self.worker("update", *devops)
        refused = self.worker("merge", *devops)
        self.assertIn("acceptance is missing", refused["error"])
        self.assertEqual(self.hub.merged, [])

    def test_ci_off_drops_a_required_check_the_binding_does_not_name_and_on_brings_back_all_of_them(self):
        self.bind()
        # As 0.6.0 left one project: its CI off took only acceptance off main, and postgres-stores still blocked merges.
        self.hub.protection = [{"context": "t3/review"}, {"context": "postgres-stores", "app_id": 15368}]
        self.assertEqual(self.orchestrator("ci", on=False, authorization="Turn CI off.")["changed"], ["required checks of main"])
        self.assertEqual(self.hub.protection, [{"context": "t3/review"}])
        self.assertEqual(self.orchestrator("ci", on=False, authorization="Turn CI off again.")["changed"], [])
        on = self.orchestrator("ci", on=True, authorization="Turn CI on.")
        self.assertEqual(on["changed"], ["required checks of main"])
        self.assertEqual(self.hub.protection, [{"context": "t3/review"}, {"context": "postgres-stores", "app_id": 15368},
                                               {"context": "acceptance"}])

    def test_setup_github_leaves_the_ci_checks_out_while_ci_is_off(self):
        self.bind()
        self.assertTrue(self.orchestrator("ci", on=False, authorization="Turn CI off.")["ok"])
        self.assertTrue(self.orchestrator("setup_github", authorization="Set up GitHub.")["ok"])
        self.assertEqual(self.hub.setups, [("main", [])])

    # DevOps off: the harness merges, and the watcher wakes the orchestrator when there is a step to take.

    def approve(self, review, tree):
        head = git(tree, "rev-parse", "HEAD")
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", head)["ok"])
        return head

    def wakes(self):
        """One watcher pass over the merges the harness runs. Returns the messages it sent."""
        import watch
        sent = []
        engine = flow.Flow(self.db, hub=lambda repo: self.hub)
        try:
            watch.merges(engine, lambda thread, text: sent.append((thread, text)))
        finally:
            engine.close()
        return sent

    def test_without_devops_the_harness_merges_a_confirmed_pr_once_ci_passes_and_the_watcher_wakes_it(self):
        _, review, tree = self.to_review(devops=False)
        head = self.approve(review, tree)
        self.assertEqual(self.start(), [], "no DevOps worker starts")
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["confirmed"])
        (wake,) = self.wakes()
        self.assertEqual(wake[0], ORCH)
        self.assertTrue(wake[1].startswith("use t3 workflow:"), wake)
        self.assertEqual(self.wakes(), [], "one wake per step")
        self.start()
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["merging"], "the harness holds it while CI runs")
        self.assertEqual(self.wakes(), [], "CI has not finished")
        self.green(head)
        self.assertEqual(len(self.wakes()), 1)
        step = self.orchestrator("next")
        self.assertEqual(self.hub.merged, [7])
        self.assertIn("Merged PR #7: Fix the login typo (M30).", step["tell"])
        self.assertIn("Plan M30 done: 1 merged.", step["tell"])
        self.assertEqual(git(self.workspace, "rev-parse", "main"), git(self.origin, "rev-parse", "main"))

    def test_without_devops_a_ci_failure_goes_to_the_implementer_who_can_ask_the_user_about_the_pipeline(self):
        call, review, tree = self.to_review(devops=False)
        head = self.approve(review, tree)
        self.start()
        self.start()
        self.hub.checks_on[head] = {"acceptance": "failure"}
        (fix,) = self.start()
        self.assertEqual(fix["arguments"]["threadId"], f"child-{worker_command(call)[0]}")
        message = fix["arguments"]["message"]
        self.assertIn("acceptance", message)
        self.assertIn("gh run view", message)
        self.assertIn("worker stuck", message, "a broken pipeline is the user's call")
        self.assertEqual(self.hub.merged, [])

    def test_without_devops_a_pr_behind_main_is_updated_and_keeps_its_approval_when_its_change_is_the_same(self):
        _, review, tree = self.to_review(devops=False)
        self.approve(review, tree)
        self.start()
        self.push_main("src/other.ts", "export const other = 1;\n")
        self.start()
        head = self.hub.prs[7]["head"]
        self.assertIn((head, "t3/review", "success"), self.hub.posted)
        self.assertEqual(git(tree, "rev-parse", "HEAD"), head, "the checkout follows its PR")
        self.green(head)
        self.start()
        self.assertEqual(self.hub.merged, [7])
        self.assertTrue((self.workspace / "src" / "other.ts").exists())

    def test_without_devops_a_conflict_with_main_goes_back_to_the_implementer(self):
        call, review, tree = self.to_review(devops=False)
        self.approve(review, tree)
        self.start()
        self.push_main("src/login.ts", "export const a = 2;\n")
        (fix,) = self.start()
        self.assertEqual(fix["arguments"]["threadId"], f"child-{worker_command(call)[0]}")
        self.assertIn("git merge origin/main", fix["arguments"]["message"])

    def test_turning_devops_on_leaves_a_running_plan_without_it_and_a_new_plan_gets_it(self):
        _, review, tree = self.to_review(devops=False)
        self.assertFalse(self.orchestrator("devops", on=True)["ok"], "it needs the user's words")
        turned = self.orchestrator("devops", on=True, authorization="Turn DevOps on.")
        self.assertEqual((turned["devops"], turned["keep"]), (True, ["M30"]))
        self.assertTrue(self.status()["devops"])
        self.green(self.approve(review, tree))
        self.assertEqual(self.start(), [], "M30 started without DevOps and keeps it that way")
        self.start()
        self.assertEqual(self.hub.merged, [7])
        self.assertTrue(self.orchestrator("queue_edit", plans=[{"plan": "M31", "owns": ["src/"], "tasks": [
            {"task": "Add a footer", "brief": "Add it."}]}])["ok"])
        (call,) = self.start()
        worker, token, tree2 = self.implement(call, "export const footer = 1;\n", 8)
        self.assertTrue(self.worker("pr", worker, token, "--pr", "8", path=tree2)["ok"])
        (review2,) = self.start()
        self.approve(review2, tree2)
        (devops,) = self.start()
        self.assertIn("worker take", devops["arguments"]["task"])

    def test_a_pipeline_failure_holds_the_task_until_devops_own_fix_is_reviewed_and_merged(self):
        self.t3_caches()
        _, devops, _ = self.to_devops(picks={"reviewer": "claude-opus-5-5"})
        job = self.worker("take", *devops)
        self.hub.checks_on[job["head"]] = {"acceptance": "failure"}
        held = self.worker("failed", *devops, "--cause", "pipeline", "--detail", "The runner image lost Node")
        self.assertEqual(held["state"], "pipeline_wait", held)
        git(self.workspace, "fetch", "origin")
        fix_tree = Path(self.tmp.name) / "wt-devops"
        git(self.workspace, "worktree", "add", "-b", "t3/devops", str(fix_tree), "origin/main")
        (fix_tree / ".github").mkdir()
        (fix_tree / ".github" / "ci.yml").write_text("node: 24\n")
        git(fix_tree, "add", ".")
        git(fix_tree, "commit", "-m", "Pin Node")
        git(fix_tree, "push", "-u", "origin", "HEAD")
        self.hub.open(9, "t3/devops", [".github/ci.yml"], body="Pins Node 24.")  # No Decisions: a pipeline fix needs none.
        registered = self.worker("pipeline", *devops, "--pr", "9", path=fix_tree)
        self.assertTrue(registered["ok"], registered)
        self.assertIsNone(self.worker("take", *devops)["job"])
        (review,) = self.start()
        self.assertIn("PR #9", review["arguments"]["task"])
        self.assertEqual(review["arguments"]["target"]["model"], "gpt-6.2-astra", "DevOps wrote it on Sonnet 5.5")
        self.assertEqual(self.last_done["tell"], ["Reviewer for Fix the pipeline (PR #9) (M30) started on gpt-6.2-astra at "
                                                  "xhigh (a different family than the DevOps worker's claude-sonnet-5-5)."])
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(fix_tree, "rev-parse", "HEAD"))["ok"])
        (wake,) = self.start()
        self.assertIn("worker take", wake["arguments"]["message"])
        fix_job = self.worker("take", *devops)
        self.assertEqual(fix_job["pr"], 9)
        self.assertEqual(self.worker("update", *devops)["result"], "current")
        self.green(fix_job["head"])
        self.assertTrue(self.worker("merge", *devops)["ok"])
        again = self.worker("take", *devops)
        self.assertEqual((again["job"], again["pr"]), ("merge", 7))
        updated = self.worker("update", *devops)
        self.assertEqual(updated["result"], "updated")
        self.green(updated["head"])
        self.assertTrue(self.worker("merge", *devops)["ok"])
        self.assertEqual(self.hub.merged, [9, 7])

    def outside(self):
        """An approved PR that also changes package.json, outside the task's src/. Returns (implementer call, ask)."""
        call, review, tree = self.to_review(files=("src/login.ts", "package.json"))
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [])
        (ask,) = step["ask"]
        self.assertIn("package.json", ask["question"])
        self.assertEqual(ask["options"], ["merge", "back", "drop"])
        self.assertEqual(self.orchestrator("next")["ask"], [], "a question is asked once")
        return call, ask

    def test_files_outside_the_tasks_paths_are_asked_about_and_merge_hands_it_to_devops(self):
        _, ask = self.outside()
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="merge")["ok"])
        (devops,) = self.start()
        self.assertEqual(devops["arguments"]["role"], "general")

    def test_sending_it_back_names_the_files_to_take_out(self):
        call, ask = self.outside()
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="back")["ok"])
        (fix,) = self.start()
        self.assertEqual(fix["arguments"]["threadId"], f"child-{worker_command(call)[0]}")
        self.assertIn("package.json", fix["arguments"]["message"])

    def test_dropping_the_last_task_ends_its_plan(self):
        _, ask = self.outside()
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="drop")["ok"])
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [])
        self.assertIn("Plan M30 done: 0 merged, 1 dropped.", step["tell"])

    def guarded_ask(self, files, **binding):
        """An approved PR changing files; returns (implementer call, the one ask next puts)."""
        call, review, tree = self.to_review(files=files, **binding)
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [])
        (ask,) = step["ask"]
        self.assertEqual(ask["options"], ["merge", "back", "drop"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["asking"])
        return call, ask

    def test_a_pr_changing_a_guarded_path_inside_its_own_paths_waits_for_your_answer(self):
        _, ask = self.guarded_ask(("src/login.ts", "src/db/001_users.sql"), guarded=["src/db/"])
        self.assertIn("guarded files: src/db/001_users.sql", ask["question"])
        self.assertNotIn("outside", ask["question"])
        self.assertEqual(self.status()["guarded"], ["src/db/"])
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="merge")["ok"])
        (devops,) = self.start()
        self.assertEqual(devops["arguments"]["role"], "general")

    def test_a_pr_both_outside_its_paths_and_guarded_gets_one_ask_and_back_names_both(self):
        call, ask = self.guarded_ask(("src/db/001_users.sql", "package.json"), guarded=["src/db/"])
        self.assertIn("outside src/: package.json", ask["question"])
        self.assertIn("guarded files: src/db/001_users.sql", ask["question"])
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="back")["ok"])
        (fix,) = self.start()
        self.assertEqual(fix["arguments"]["threadId"], f"child-{worker_command(call)[0]}")
        self.assertIn("package.json", fix["arguments"]["message"])
        self.assertIn("src/db/001_users.sql", fix["arguments"]["message"])

    def test_without_devops_a_guarded_path_waits_for_your_answer_too(self):
        _, ask = self.guarded_ask(("src/login.ts",), devops=False, guarded=["src/login.ts"])
        self.assertIn("guarded files: src/login.ts", ask["question"])

    def test_the_user_changes_the_guarded_paths_on_their_word_and_new_approvals_follow_them(self):
        call, review, tree = self.to_review(files=("src/login.ts", "deploy/app.yml"), owns=("src/", "deploy/"))
        self.assertEqual(self.status()["guarded"], [], "none by default")
        self.assertFalse(self.orchestrator("guarded", paths=["deploy/"])["ok"], "it needs the user's own words")
        self.assertFalse(self.orchestrator("guarded", paths=["../elsewhere/"], authorization="Guard it.")["ok"])
        changed = self.orchestrator("guarded", paths=["deploy/"], authorization="Guard the deploy folder.")
        self.assertEqual(changed["guarded"], ["deploy/"], changed)
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
        (ask,) = self.orchestrator("next")["ask"]
        self.assertIn("guarded files: deploy/app.yml", ask["question"])
        self.assertTrue(self.orchestrator("guarded", paths=[], authorization="Guard nothing.")["ok"])
        self.assertEqual(self.status()["guarded"], [])

    def rounds(self, call, review, tree, count, number=7):
        """count reviews in a row ask for changes; the implementer pushes a fix after each but the last. Returns the
        last reviewer call."""
        for round_ in range(1, count + 1):
            self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "changes",
                                        "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
            if round_ == count:
                return review
            (fix,) = self.start()
            self.assertEqual(fix["tool"], "t3_thread_send")
            worker, token, tree = self.implement(call, f"export const name = 'round {round_}';\n", number)
            self.assertTrue(self.worker("pr", worker, token, "--pr", str(number), path=tree)["ok"])
            (review,) = self.start()

    def rounds_ask(self, **kwargs):
        """A task whose third review asked for changes. Returns (implementer call, worktree, ask)."""
        call, review, tree = self.to_review(**kwargs)
        self.rounds(call, review, tree, 3)  # The first two go back to the implementer as before.
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [], "the third round does not go back to its implementer")
        self.assertIn("Fix the login typo (M30) stopped after 3 review rounds: each asked for changes.", step["tell"])
        (ask,) = step["ask"]
        self.assertEqual(ask["options"], ["carry on", "restart", "drop"])
        self.assertIn("PR #7", ask["question"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["asking"])
        return call, tree, ask

    def test_after_three_review_rounds_carry_on_sends_it_back_and_the_count_starts_again(self):
        call, tree, ask = self.rounds_ask()
        self.assertFalse(self.orchestrator("answer", ask=ask["id"], answer="merge")["ok"])
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="carry on")["ok"])
        (fix,) = self.start()
        self.assertEqual((fix["tool"], fix["arguments"]["threadId"]), ("t3_thread_send", f"child-{worker_command(call)[0]}"))
        self.assertIn("asked for changes", fix["arguments"]["message"])
        worker, token, tree = self.implement(call, "export const name = 'again';\n", 7)
        self.assertTrue(self.worker("pr", worker, token, "--pr", "7", path=tree)["ok"])
        (review,) = self.start()
        self.rounds(call, review, tree, 2)
        step = self.orchestrator("next")
        self.assertEqual(step["ask"], [], "two more rounds after carry on go back without asking")
        self.assertEqual([c["tool"] for c in step["calls"]], ["t3_thread_send"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["implementing"])

    def test_after_three_review_rounds_restart_gives_the_pr_to_a_new_implementer_on_another_model(self):
        call, tree, ask = self.rounds_ask()
        self.assertEqual(call["arguments"]["target"]["model"], "gpt-6.2-astra")
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="restart")["ok"])
        cancel, fresh = self.start()  # The old implementer's run had not ended here, so it is cancelled.
        self.assertEqual((cancel["tool"], cancel["worker"]), ("task_cancel", call["worker"]))
        self.assertEqual(fresh["tool"], "delegate_task")
        self.assertNotEqual(fresh["worker"], call["worker"])
        self.assertEqual(fresh["arguments"]["target"]["model"], "claude-opus-5-5",
                         "Jev named the model that held it, so the fallback runs")
        self.assertNotIn("gpt-6.2-astra", self.jev_models, "Jev weighs only the other models")
        brief = fresh["arguments"]["task"]
        self.assertIn("PR #7 already holds this task's work", brief)
        self.assertIn("It came back because: The reviewer asked for changes on PR #7 3 times", brief)
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["implementing"])

    def test_after_three_review_rounds_restart_keeps_the_implementer_model_the_user_picked(self):
        self.t3_caches()
        call, tree, ask = self.rounds_ask(picks={"implementer": "gpt-6.2-sol"})
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="restart")["ok"])
        cancel, fresh = self.start()  # The old implementer's run had not ended here, so it is cancelled.
        self.assertEqual((cancel["tool"], cancel["worker"]), ("task_cancel", call["worker"]))
        self.assertNotEqual(fresh["worker"], call["worker"])
        self.assertEqual(fresh["arguments"]["target"]["model"], "gpt-6.2-sol")

    def test_after_three_review_rounds_drop_ends_the_task(self):
        _, _, ask = self.rounds_ask()
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="drop")["ok"])
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [])
        self.assertIn("Plan M30 done: 0 merged, 1 dropped.", step["tell"])

    def test_a_stuck_implementer_gets_your_answer_as_a_follow_up(self):
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        stuck = self.worker("stuck", *worker_command(call), "--why", "Which login API, v1 or v2?")
        self.assertTrue(stuck["ok"], stuck)
        (ask,) = self.orchestrator("next")["ask"]
        self.assertIn("Which login API, v1 or v2?", ask["question"])
        self.assertTrue(self.orchestrator("answer", ask=ask["id"], answer="Use v2.")["ok"])
        (fix,) = self.start()
        self.assertEqual(fix["arguments"]["threadId"], f"child-{worker_command(call)[0]}")
        self.assertIn("Use v2.", fix["arguments"]["message"])
        self.assertIn("--pr <number>", fix["arguments"]["message"])

    def end(self, call, status="completed", text=None):
        """T3 wakes the orchestrator: the run this call started has ended; its hook records that, with the notice's text."""
        ended = self.orchestrator("ended", taskId=self.runs[call["call"]], status=status, **({"text": text} if text else {}))
        self.assertTrue(ended["ok"], ended)

    def test_a_worker_that_ends_without_reporting_is_asked_once_then_its_task_is_blocked_until_restarted(self):
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.end(call)
        (nudge,) = self.start()
        self.assertEqual(nudge["arguments"]["threadId"], f"child-{worker_command(call)[0]}")
        self.assertIn("worker pr", nudge["arguments"]["message"])
        self.end(nudge)
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [])
        self.assertTrue(any("Fix the login typo" in line and "blocked" in line for line in step["tell"]), step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["blocked"])
        refused = self.orchestrator("restart", task="Fix the login typo")
        self.assertFalse(refused["ok"], "a restart needs your own words")
        self.assertTrue(self.orchestrator("restart", task="Fix the login typo", authorization="Restart it.")["ok"])
        (fresh,) = self.start()
        self.assertEqual(fresh["tool"], "delegate_task")
        self.assertNotEqual(worker_command(fresh)[0], worker_command(call)[0])

    def test_a_worker_that_reported_and_ended_is_left_alone(self):
        call, review, tree = self.to_review()
        self.end(call)
        self.assertEqual(self.orchestrator("next")["calls"], [])

    def test_restarting_a_worker_still_running_cancels_its_run(self):
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertTrue(self.orchestrator("restart", task="Fix the login typo", authorization="It went quiet.")["ok"])
        cancel, fresh = self.start()
        self.assertEqual((cancel["tool"], cancel["arguments"]["taskId"]), ("task_cancel", self.runs[call["call"]]))
        self.assertEqual(fresh["tool"], "delegate_task")

    def quiet(self, call, since="2026-11-23T09:00:00Z"):
        """The watcher finds the worker this call started quiet since `since`. Returns what it would send."""
        engine = flow.Flow(self.db, hub=lambda repo: self.hub, jev=self.advise)
        try:
            return engine.quiet(PROJECT, call["worker"], since, 30)
        finally:
            engine.close()

    def test_a_quiet_implementer_is_replaced_once_without_you_and_a_quiet_replacement_blocks_its_task(self):
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertTrue(self.quiet(call)["send"])
        cancel, fresh = self.start()
        self.assertEqual((cancel["tool"], cancel["arguments"]["taskId"]), ("task_cancel", self.runs[call["call"]]))
        self.assertEqual(fresh["tool"], "delegate_task")
        self.assertNotEqual(fresh["worker"], call["worker"])
        self.assertIn(f"{call['worker']}, the implementer of Fix the login typo (M30) was quiet since 2026-11-23T09:00:00Z, "
                      f"so it was stopped and a new implementer took over.", self.last_step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["implementing"])
        self.assertTrue(self.quiet(fresh, "2026-11-23T10:00:00Z")["send"])
        (cancel,) = self.start()
        self.assertEqual((cancel["tool"], cancel["worker"]), ("task_cancel", fresh["worker"]), "no third worker starts")
        self.assertIn("Fix the login typo (M30) is blocked: its new implementer was quiet too, since 2026-11-23T10:00:00Z. "
                      "Say restart to give it another implementer, or drop it.", self.last_step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["blocked"])
        self.assertTrue(self.orchestrator("restart", task="Fix the login typo", authorization="Restart it.")["ok"])
        (third,) = self.start()
        self.assertEqual(third["tool"], "delegate_task")
        self.assertTrue(self.quiet(third, "2026-11-23T11:00:00Z")["send"])
        self.assertEqual([c["tool"] for c in self.start()], ["task_cancel", "delegate_task"],
                         "your restart starts the count again")

    def test_a_quiet_reviewer_is_replaced_and_a_reported_review_starts_its_count_again(self):
        call, review, tree = self.to_review()
        self.quiet(review)
        cancel, fresh = self.start()
        self.assertEqual((cancel["tool"], fresh["arguments"]["role"]), ("task_cancel", "review"))
        self.assertIn("Review M30: Fix the login typo", fresh["arguments"]["title"])
        self.assertTrue(self.worker("review", *worker_command(fresh), "--verdict", "changes",
                                    "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
        self.start()
        worker, token, tree = self.implement(call, "export const name = 'fixed';\n", 7)
        self.assertTrue(self.worker("pr", worker, token, "--pr", "7", path=tree)["ok"])
        (second,) = self.start()
        self.quiet(second)
        self.assertEqual([c["tool"] for c in self.start()], ["task_cancel", "delegate_task"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["reviewing"])

    def test_a_quiet_devops_worker_is_replaced_and_the_task_it_held_waits_for_the_new_one(self):
        _, devops, _ = self.to_devops()
        self.assertEqual(self.worker("take", *devops)["pr"], 7)
        self.quiet({"worker": devops[0]})
        cancel, fresh = self.start()
        self.assertEqual((cancel["tool"], cancel["worker"]), ("task_cancel", devops[0]))
        self.assertEqual((fresh["tool"], fresh["arguments"]["role"]), ("delegate_task", "general"))
        self.assertIn(f"{devops[0]}, the DevOps worker of Fix the login typo (M30) was quiet since 2026-11-23T09:00:00Z, so "
                      f"it was stopped and a new DevOps worker took over.", self.last_step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["confirmed"])
        self.assertEqual(self.worker("take", *worker_command(fresh))["pr"], 7)
        self.quiet(fresh, "2026-11-23T10:00:00Z")
        (cancel,) = self.start()
        self.assertEqual((cancel["tool"], cancel["worker"]), ("task_cancel", fresh["worker"]))
        self.assertIn("Fix the login typo (M30) is blocked: its new DevOps worker was quiet too, since "
                      "2026-11-23T10:00:00Z. Say restart to give it another DevOps worker, or drop it.", self.last_step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["blocked"])

    def test_a_paused_binding_replaces_no_quiet_worker_and_tells_you(self):
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        self.assertTrue(self.orchestrator("control", action="pause")["ok"])
        self.assertTrue(self.quiet(call)["send"])
        self.assertEqual(self.start(), [])
        self.assertTrue(any("has been quiet since" in line and "Say restart Fix the login typo" in line
                            for line in self.last_step["tell"]), self.last_step["tell"])
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["implementing"])

    def test_when_nothing_more_can_move_without_you_one_summary_leads_with_what_waits_for_you(self):
        self.bind()
        self.queue("Fix the login typo", {"task": "Add a footer", "brief": "Add it."},
                   {"task": "Use the name", "brief": "Use it.", "after": ["Fix the login typo"]})
        first, second = self.start()
        self.assertTrue(self.worker("stuck", *worker_command(first), "--why", "Which login API, v1 or v2?")["ok"])
        self.end(first)
        self.end(second)
        (nudge,) = self.start()
        self.assertFalse([line for line in self.last_step["tell"] if line.startswith("Summary:")], "a follow-up is due")
        self.end(nudge)
        step = self.orchestrator("next")
        (ask,) = self.status()["openAsks"]
        self.assertEqual(step["tell"][-1], (
            f"Summary: nothing more can move without you. Waiting for you: {ask['id']} (Fix the login typo asks: Which "
            f"login API, v1 or v2?); Add a footer (M30) is blocked: its implementer ended twice without reporting. "
            f"So far 0 merged, 0 dropped."))
        self.assertEqual(self.orchestrator("next")["tell"], [], "told once per stop")
        self.assertTrue(self.orchestrator("queue_edit", drop="Add a footer")["ok"])
        self.assertTrue(self.orchestrator("next")["tell"][-1].endswith("So far 0 merged, 1 dropped."), "something changed")

    def test_a_finished_queue_gets_its_summary_once(self):
        call, review, tree = self.to_review()
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree, "rev-parse", "HEAD"))["ok"])
        (devops_call,) = self.start()
        devops = worker_command(devops_call)
        self.end(call)
        self.end(review)
        job = self.worker("take", *devops)
        self.green(job["head"])
        self.assertTrue(self.worker("merge", *devops)["ok"])
        self.assertFalse([line for line in self.orchestrator("next")["tell"] if line.startswith("Summary:")],
                         "DevOps's run is still going")
        self.end(devops_call)
        step = self.orchestrator("next")
        self.assertEqual(step["tell"][-1], "Summary: the queue is finished: 1 merged, 0 dropped.")
        self.assertEqual(self.orchestrator("next")["tell"], [])

    def test_devops_whose_run_ended_is_woken_by_the_next_confirmation(self):
        self.bind()
        self.queue("Fix the login typo", {"task": "Add a footer", "brief": "Add it.", "after": []})
        first, second = self.start()
        w1, t1, tree1 = self.implement(first, "export const name = 1;\n", 7)
        self.assertTrue(self.worker("pr", w1, t1, "--pr", "7", path=tree1)["ok"])
        (review,) = self.start()
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree1, "rev-parse", "HEAD"))["ok"])
        (devops_call,) = self.start()
        devops = worker_command(devops_call)
        self.worker("take", *devops)
        self.green(git(tree1, "rev-parse", "HEAD"))
        self.assertTrue(self.worker("merge", *devops)["ok"])
        self.end(devops_call)
        self.assertEqual(self.orchestrator("next")["calls"], [], "DevOps reported; it is not nudged")
        w2, t2, tree2 = self.implement(second, "export const footer = 1;\n", 8)
        self.assertTrue(self.worker("pr", w2, t2, "--pr", "8", path=tree2)["ok"])
        (review2,) = self.start()
        self.assertTrue(self.worker("review", *worker_command(review2), "--verdict", "approve",
                                    "--sha", git(tree2, "rev-parse", "HEAD"))["ok"])
        (wake,) = self.start()
        self.assertEqual(wake["arguments"]["threadId"], f"child-{devops[0]}")

    def test_queue_edit_adds_a_task_to_a_running_plan_and_a_dropped_task_frees_the_one_after_it(self):
        self.bind()
        self.queue("Fix the login typo", {"task": "Use the new name", "brief": "Use it.", "after": ["Fix the login typo"]})
        (first,) = self.start()
        added = self.orchestrator("queue_edit", plan="M30", tasks=[{"task": "Add a footer", "brief": "Add it."}])
        self.assertTrue(added["ok"], added)
        (footer,) = self.start()
        self.assertIn("Add a footer", footer["arguments"]["task"])
        self.assertTrue(self.orchestrator("queue_edit", drop="Fix the login typo")["ok"])
        cancel, freed = self.start()
        self.assertEqual((cancel["tool"], cancel["arguments"]["taskId"]), ("task_cancel", self.runs[first["call"]]))
        self.assertIn("Use the new name", freed["arguments"]["task"])

    def test_a_relink_off_an_unmerged_task_needs_why_which_its_implementer_and_status_see_and_the_user_is_not_told(self):
        self.bind()
        self.queue("Fix the login typo", {"task": "Use the new name", "brief": "Use it.", "after": ["Fix the login typo"]})
        self.start()
        refused = self.orchestrator("queue_edit", task="Use the new name", after=[])
        self.assertFalse(refused["ok"])
        self.assertIn("why", refused["error"])
        why = "It only renames the call site; it uses no code from the typo fix."
        self.assertTrue(self.orchestrator("queue_edit", task="Use the new name", after=[], why=why)["ok"])
        (second,) = self.start()
        brief = second["arguments"]["task"]
        self.assertIn("Use the new name", brief)
        self.assertIn(why, brief)
        self.assertIn("worker waits", brief)
        self.assertIn("Fix the login typo (M30)", brief, "the open tasks it could wait for")
        self.assertFalse(any("Fix the login typo" in line for line in self.last_step["tell"]), self.last_step["tell"])
        row = next(t for t in self.status()["tasks"] if t["task"] == "Use the new name")
        self.assertEqual([(u["task"], u["why"]) for u in row["unlinked"]], [("t1", why)])

    def test_a_relink_off_an_unfinished_plan_needs_why_and_its_tasks_briefs_carry_it(self):
        self.bind()
        self.assertTrue(self.orchestrator("queue_set", plans=[
            {"plan": "M30", "owns": ["src/"], "tasks": [{"task": "Fix the login typo", "brief": "Fix it."}]},
            {"plan": "M31", "owns": ["src/"], "dependsOn": ["M30"], "tasks": [{"task": "Add a footer", "brief": "Add it."}]},
        ])["ok"])
        (first,) = self.start()
        self.assertIn("Fix the login typo", first["arguments"]["task"])
        refused = self.orchestrator("queue_edit", plan="M31", dependsOn=[])
        self.assertFalse(refused["ok"])
        self.assertIn("why", refused["error"])
        why = "M31 changes only the footer; nothing in it calls M30's code."
        self.assertTrue(self.orchestrator("queue_edit", plan="M31", dependsOn=[], why=why)["ok"])
        (footer,) = self.start()
        self.assertIn("Add a footer", footer["arguments"]["task"])
        self.assertIn(why, footer["arguments"]["task"])

    def work(self, call, files=None, push=True):
        """An implementer's work in progress in its own worktree, no PR yet: files committed and, unless push is False,
        pushed. Returns (worker, token, worktree)."""
        worker, token = worker_command(call)
        tree = Path(self.tmp.name) / f"wt-{worker}"
        git(self.workspace, "worktree", "add", "-b", f"t3/{worker}", str(tree), "origin/main")
        for name, content in (files or {}).items():
            (tree / name).write_text(content)
            git(tree, "add", name)
        if files:
            git(tree, "commit", "-m", f"work of {worker}")
            if push:
                git(tree, "push", "-u", "origin", "HEAD")
        return worker, token, tree

    def merge_task(self, call, content, number):
        """The task this call started is implemented, reviewed, approved and merged by DevOps."""
        w, t, tree = self.implement(call, content, number)
        self.assertTrue(self.worker("pr", w, t, "--pr", str(number), path=tree)["ok"])
        (review,) = self.start()
        head = git(tree, "rev-parse", "HEAD")
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve", "--sha", head)["ok"])
        (devops_call,) = self.start()
        devops = worker_command(devops_call)
        self.worker("take", *devops)
        self.green(head)
        self.assertTrue(self.worker("merge", *devops)["ok"])

    def test_an_implementer_that_needs_another_tasks_code_waits_and_the_same_one_carries_on_after_it_merges(self):
        self.bind()
        self.queue("Fix the login typo", "Use the new name")  # Parallel: nobody declared the link.
        first, second = self.start()
        w2, t2, tree2 = self.work(second, {"src/use.ts": "export const use = 1;\n"})
        why = "src/login.ts has no name export yet"
        waits = self.worker("waits", w2, t2, "--on", "Fix the login typo", "--why", why, path=tree2)
        self.assertTrue(waits["ok"], waits)
        self.assertEqual(waits["state"], "waiting")
        self.assertEqual([t["state"] for t in self.status()["tasks"]], ["implementing", "waiting"])
        self.end(second)
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [], "it reported: no nudge, no second implementer")
        self.assertEqual(step["tell"], [], "nothing for the user")
        row = next(t for t in self.status()["tasks"] if t["task"] == "Use the new name")
        self.assertEqual([(n["task"], n["why"]) for n in row["needed"]], [("t1", why)])
        self.merge_task(first, "export const name = 1;\n", 7)
        (resume,) = self.start()
        self.assertEqual((resume["tool"], resume["arguments"]["threadId"]), ("t3_thread_send", f"child-{w2}"))
        for words in ("Fix the login typo merged.", "git merge origin/main", "worker pr"):
            self.assertIn(words, resume["arguments"]["message"])
        refused = self.worker("waits", w2, t2, "--on", "Fix the login typo", "--why", why, path=tree2)
        self.assertFalse(refused["ok"])
        self.assertIn("merged", refused["error"])
        git(tree2, "fetch", "origin")
        git(tree2, "merge", "--no-edit", "origin/main")
        git(tree2, "push")
        self.hub.open(8, f"t3/{w2}", ["src/use.ts"])
        self.assertTrue(self.worker("pr", w2, t2, "--pr", "8", path=tree2)["ok"])
        (review,) = self.start()
        self.assertIn(f"This task waited for Fix the login typo ({why})", review["arguments"]["task"])

    def test_waits_refuses_what_would_skip_or_stall_the_work_and_changes_nothing(self):
        self.bind()
        self.queue("Fix the login typo", {"task": "Use the new name", "brief": "Use it.", "after": ["Fix the login typo"]},
                   "Add a footer", "Add a header")
        first, _, _ = self.start()
        w1, t1, tree1 = self.work(first)
        self.assertTrue(self.orchestrator("queue_edit", drop="Add a header")["ok"])
        for on, why, words in (("Fix the login typo", "src/login.ts", "its own"),
                               ("Use the new name", "src/use.ts has no use export", "forever"),
                               ("Add a footer", "it needs the footer", "file"),
                               ("Add a header", "src/header.ts has no header", "worker stuck")):
            refused = self.worker("waits", w1, t1, "--on", on, "--why", why, path=tree1)
            self.assertFalse(refused["ok"], on)
            self.assertIn(words, refused["error"])
        (tree1 / "src" / "login.ts").write_text("export const name = 1;\n")
        why = "src/footer.ts has no footer export"
        refused = self.worker("waits", w1, t1, "--on", "Add a footer", "--why", why, path=tree1)
        self.assertFalse(refused["ok"])
        self.assertIn("uncommitted", refused["error"])
        git(tree1, "commit", "-am", "work")
        refused = self.worker("waits", w1, t1, "--on", "Add a footer", "--why", why, path=tree1)
        self.assertFalse(refused["ok"])
        self.assertIn("push", refused["error"])
        self.assertEqual(next(t for t in self.status()["tasks"] if t["task"] == "Fix the login typo")["state"], "implementing")

    def test_a_link_its_implementer_put_back_comes_off_only_when_the_user_says_so(self):
        self.bind()
        self.queue("Fix the login typo", "Use the new name")
        _, second = self.start()
        w2, t2, tree2 = self.work(second)
        self.assertTrue(self.worker("waits", w2, t2, "--on", "Fix the login typo", "--why",
                                    "src/login.ts has no name export yet", path=tree2)["ok"])
        why = "It can use the old name for now."
        refused = self.orchestrator("queue_edit", task="Use the new name", after=[], why=why)
        self.assertFalse(refused["ok"])
        self.assertIn("authorization", refused["error"])
        self.assertTrue(self.orchestrator("queue_edit", task="Use the new name", after=[], why=why,
                                          authorization="Let it start now with the old name.")["ok"])
        (resume,) = self.start()
        self.assertEqual(resume["arguments"]["threadId"], f"child-{w2}")
        self.assertIn(f"The user let this task go on without Fix the login typo: {why}", resume["arguments"]["message"])
        (tree2 / "src" / "use.ts").write_text("export const use = 'old name';\n")
        git(tree2, "add", ".")
        git(tree2, "commit", "-m", "use the old name")
        git(tree2, "push", "-u", "origin", "HEAD")
        self.hub.open(8, f"t3/{w2}", ["src/use.ts"])
        self.assertTrue(self.worker("pr", w2, t2, "--pr", "8", path=tree2)["ok"])
        (review,) = self.start()
        self.assertIn(f"The user let this task go on without Fix the login typo: {why}", review["arguments"]["task"])
        self.assertNotIn("request changes and name it", review["arguments"]["task"], "the user allowed a stand-in")

    def test_a_waiting_task_whose_needed_task_is_dropped_is_told_so_and_waits_only_once(self):
        self.bind()
        self.queue("Fix the login typo", "Use the new name", "Add a footer")
        first, second, _ = self.start()
        w2, t2, tree2 = self.work(second)
        self.assertTrue(self.worker("waits", w2, t2, "--on", "Fix the login typo", "--why",
                                    "src/login.ts has no name export yet", path=tree2)["ok"])
        self.assertTrue(self.orchestrator("queue_edit", drop="Fix the login typo")["ok"])
        cancel, resume = self.start()
        self.assertEqual((cancel["tool"], cancel["arguments"]["taskId"]), ("task_cancel", self.runs[first["call"]]))
        self.assertEqual(resume["arguments"]["threadId"], f"child-{w2}")
        self.assertIn("Fix the login typo was dropped", resume["arguments"]["message"])
        self.assertIn("worker stuck", resume["arguments"]["message"])
        again = self.worker("waits", w2, t2, "--on", "Add a footer", "--why", "src/footer.ts has no footer", path=tree2)
        self.assertFalse(again["ok"])
        self.assertIn("once", again["error"])
        self.assertIn("worker stuck", again["error"])

    def test_queue_edit_refuses_a_relink_that_would_wait_forever_and_changes_nothing(self):
        self.bind()
        self.assertTrue(self.orchestrator("control", action="pause")["ok"])
        self.assertTrue(self.orchestrator("queue_set", plans=[
            {"plan": "M30", "owns": ["src/"], "tasks": [
                {"task": "Fix the login typo", "brief": "Fix it."},
                {"task": "Use the new name", "brief": "Use it.", "after": ["Fix the login typo"]}]},
            {"plan": "M31", "owns": ["src/"], "dependsOn": ["M30"], "tasks": [{"task": "Add a footer", "brief": "Add it."}]},
        ])["ok"])
        for edit in ({"task": "Fix the login typo", "after": ["Fix the login typo"]},
                     {"task": "Fix the login typo", "after": ["Use the new name"]},
                     {"task": "Fix the login typo", "after": ["Add a footer"]},
                     {"plan": "M30", "dependsOn": ["M31"]}):
            refused = self.orchestrator("queue_edit", **edit)
            self.assertFalse(refused["ok"], edit)
            self.assertIn("forever", refused["error"])
        self.assertTrue(self.orchestrator("control", action="resume", authorization="Go on.")["ok"])
        (first,) = self.start()
        self.assertIn("Fix the login typo", first["arguments"]["task"])

    def test_queue_edit_leaves_the_links_of_a_started_task_as_they_are(self):
        self.bind()
        self.queue("Fix the login typo", "Add a footer")
        self.start()
        refused = self.orchestrator("queue_edit", task="Fix the login typo", after=["Add a footer"])
        self.assertFalse(refused["ok"])
        self.assertIn("already started", refused["error"])

    def test_status_shows_what_each_task_and_plan_waits_for(self):
        self.bind()
        self.assertTrue(self.orchestrator("queue_set", plans=[
            {"plan": "M30", "owns": ["src/"], "tasks": [
                {"task": "Fix the login typo", "brief": "Fix it."},
                {"task": "Use the new name", "brief": "Use it.", "after": ["Fix the login typo"]}]},
            {"plan": "M31", "owns": ["src/"], "dependsOn": ["M30"], "tasks": [{"task": "Add a footer", "brief": "Add it."}]},
        ])["ok"])
        status = self.status()
        names = {t["id"]: t["task"] for t in status["tasks"]}
        self.assertEqual({t["task"]: [names[a] for a in t["after"]] for t in status["tasks"]},
                         {"Fix the login typo": [], "Use the new name": ["Fix the login typo"], "Add a footer": []})
        self.assertEqual(status["dependsOn"], {"M31": ["M30"]})

    def test_setup_github_needs_your_words_and_reports_what_it_changed(self):
        self.bind()
        self.assertFalse(self.orchestrator("setup_github")["ok"])
        first = self.orchestrator("setup_github", authorization="Set up GitHub for this project.")
        self.assertEqual(first["changed"], ["delete_branch_on_merge", "protection of main"])
        self.assertEqual(self.hub.setups, [("main", ["acceptance"])])
        self.assertEqual(self.orchestrator("setup_github", authorization="Again.")["changed"], [])

    def test_a_paused_workflow_starts_nothing_until_resumed(self):
        self.bind()
        self.assertTrue(self.orchestrator("control", action="pause")["ok"])
        self.queue("Fix the login typo")
        self.assertEqual(self.start(), [])
        self.assertFalse(self.orchestrator("control", action="resume")["ok"], "resume needs your own words")
        self.assertTrue(self.orchestrator("control", action="resume", authorization="Go on.")["ok"])
        (call,) = self.start()
        self.assertEqual(call["tool"], "delegate_task")

    def test_a_task_after_another_starts_once_that_one_merges(self):
        self.bind()
        self.queue("Fix the login typo", {"task": "Use the new name", "brief": "Use it.", "after": ["Fix the login typo"]})
        (first,) = self.start()
        w1, t1, tree1 = self.implement(first, "export const name = 1;\n", 7)
        self.assertTrue(self.worker("pr", w1, t1, "--pr", "7", path=tree1)["ok"])
        (review,) = self.start()
        self.assertTrue(self.worker("review", *worker_command(review), "--verdict", "approve",
                                    "--sha", git(tree1, "rev-parse", "HEAD"))["ok"])
        (devops_call,) = self.start()
        devops = worker_command(devops_call)
        self.worker("take", *devops)
        self.green(git(tree1, "rev-parse", "HEAD"))
        self.assertTrue(self.worker("merge", *devops)["ok"])
        (second,) = self.start()
        self.assertIn("Use the new name", second["arguments"]["task"])

    def test_a_checkout_that_is_not_the_pull_requests_head_is_refused(self):
        self.bind()
        self.queue("Fix the login typo")
        (call,) = self.start()
        worker, token, tree = self.implement(call, "export const name = 1;\n", 7)
        (tree / "src" / "login.ts").write_text("export const name = 2;\n")  # Edited after the push.
        result = self.worker("pr", worker, token, "--pr", "7", path=tree)
        self.assertFalse(result["ok"])
        self.assertIn("uncommitted", result["error"])

    # The lead: a spec and its plan, which the user approves before any implementer starts.

    REQUEST = "Users sign in with their name; split it as you see fit."

    def spec_file(self, **changes):
        """The lead's JSON file, in its temporary folder."""
        plan = {"goal": "Let users sign in with a name.", "inScope": ["The login form"], "outOfScope": ["Sign-up"],
                "questions": [], "owns": ["src/"], "tasks": [
                    {"task": "Fix the login typo", "brief": "Rename nmae to name in src/login.ts.", "owns": ["src/"]},
                    {"task": "Use the name", "brief": "Show the name.", "after": ["Fix the login typo"]}]}
        self.files = getattr(self, "files", 0) + 1
        path = Path(self.tmp.name) / f"spec-{self.files}.json"
        path.write_text(json.dumps({**plan, **changes}), encoding="utf-8")
        return str(path)

    def to_spec(self, **binding):
        """A bound project whose lead started on plan M31. Returns the lead's call."""
        self.bind(**binding)
        result = self.orchestrator("spec", plan="M31", request=self.REQUEST)
        self.assertTrue(result["ok"], result)
        (lead,) = self.start()
        return lead

    def submitted_spec(self, **binding):
        """The lead submitted its spec, and its run ended. Returns (the lead's call, the user's question)."""
        lead = self.to_spec(**binding)
        result = self.worker("submit", *worker_command(lead), "--file", self.spec_file())
        self.assertTrue(result["ok"], result)
        self.end(lead)
        (ask,) = self.orchestrator("next")["ask"]
        return lead, ask

    def test_a_spec_starts_a_read_only_lead_on_fable_at_high_once_the_binding_is_active(self):
        self.bind()
        self.assertTrue(self.orchestrator("control", action="pause")["ok"])
        refused = self.orchestrator("spec", plan="M31", request=self.REQUEST)
        self.assertFalse(refused["ok"])
        self.assertIn("active binding", refused["error"])
        self.assertTrue(self.orchestrator("control", action="resume", authorization="Go on.")["ok"])
        self.assertTrue(self.orchestrator("spec", plan="M31", request=self.REQUEST)["ok"])
        self.assertFalse(self.orchestrator("spec", plan="M31", request=self.REQUEST)["ok"], "one open spec per plan")
        (lead,) = self.start()
        arguments = lead["arguments"]
        self.assertEqual((arguments["role"], arguments["isolation"]), ("review", "shared"), "the reviewer's read-only mode")
        self.assertEqual(arguments["target"], {"providerInstanceId": "claudeAgent", "model": "claude-fable-5-2",
                                               "options": {"effort": "high"}})
        for words in (self.REQUEST, "lead.md", "worker submit --worker", "Edit nothing there", "within 4000 characters"):
            self.assertIn(words, arguments["task"])
        self.assertEqual(self.last_done["tell"], ["Lead for plan M31 started on claude-fable-5-2 at high (the default)."])
        status = self.status()
        self.assertEqual(status["specs"], [{"plan": "M31", "state": "writing", "lead": lead["worker"], "goal": None}])
        self.assertEqual((status["models"]["lead"]["model"], status["tasks"]), ("claude-fable-5-2", []))

    def test_the_leads_plan_is_checked_as_queue_set_checks_one_and_nothing_starts_before_you_approve_it(self):
        lead = self.to_spec()
        me = worker_command(lead)
        for changes, words in (({"tasks": [{"task": "A", "brief": "Do A.", "after": ["Nope"]}]}, "No single task is named Nope"),
                               ({"tasks": [{"task": "A"}]}, "Task A needs a brief"),
                               ({"owns": [], "tasks": [{"task": "A", "brief": "Do A."}]}, "owns lists the paths"),
                               ({"questions": ["x" * flow.SPEC_CHARS]}, f"keep it within {flow.SPEC_CHARS}")):
            refused = self.worker("submit", *me, "--file", self.spec_file(**changes))
            self.assertFalse(refused["ok"], changes)
            self.assertIn(words, refused["error"])
            self.assertIn("Fix the file and run this again", refused["error"])
        missing = self.worker("submit", *me, "--file", str(Path(self.tmp.name) / "none.json"))
        self.assertIn("Write the file and run this again", missing["error"])
        self.assertTrue(self.worker("submit", *me, "--file", self.spec_file())["ok"])
        self.assertFalse(self.worker("submit", *me, "--file", self.spec_file())["ok"], "submitted once")
        step = self.orchestrator("next")
        (ask,) = step["ask"]
        self.assertEqual(ask["options"], ["approve", "drop"])
        self.assertEqual(ask["question"], "\n".join([
            "The lead's spec for plan M31.", "Goal: Let users sign in with a name.", "Tasks:",
            "1. Fix the login typo (paths: src/; starts at once)", "2. Use the name (paths: src/; waits for Fix the login typo)",
            "Open questions: none.", "Approve it, drop it, or say what to change."]))
        self.assertEqual(self.status()["tasks"], [], "nothing is queued before you approve")
        self.end(lead)
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [], "a lead that submitted owes nothing")
        self.assertEqual(step["tell"][-1], f"Summary: nothing more can move without you. Waiting for you: {ask['id']} (the "
                                           f"spec of plan M31: approve, drop or say what to change). So far 0 merged, 0 dropped.")

    def test_an_approved_spec_queues_its_plan_and_its_workers_briefs_carry_the_spec(self):
        _, ask = self.submitted_spec()
        answered = self.orchestrator("answer", ask=ask["id"], answer="approve")
        self.assertEqual((answered["spec"], answered["state"]), ("M31", "approved"))
        (call,) = self.start()
        self.assertIn("Plan M31 is queued from its approved spec: 2 tasks.", self.last_step["tell"])
        self.assertEqual([(t["task"], t["state"]) for t in self.status()["tasks"]],
                         [("Fix the login typo", "implementing"), ("Use the name", "waiting")])
        brief = call["arguments"]["task"]
        self.assertIn("Brief: Rename nmae to name in src/login.ts.\nThe plan's goal: Let users sign in with a name.", brief)
        self.assertNotIn("Out of scope", brief, "the implementer gets the goal alone")
        worker, token, tree = self.implement(call, "export const name = 1;\n", 7)
        self.assertTrue(self.worker("pr", worker, token, "--pr", "7", path=tree)["ok"])
        (review,) = self.start()
        self.assertIn("judge the change against it too:\nGoal: Let users sign in with a name.\nIn scope: The login form\n"
                      "Out of scope: Sign-up\nOpen questions: none", review["arguments"]["task"])
        self.assertFalse(self.orchestrator("answer", ask=ask["id"], answer="drop")["ok"], "answered once")

    def test_your_words_on_a_spec_go_to_its_lead_which_submits_again_and_drop_ends_it(self):
        lead, ask = self.submitted_spec()
        answered = self.orchestrator("answer", ask=ask["id"], answer="Split the first task in two.")
        self.assertEqual(answered["state"], "writing")
        (revise,) = self.start()
        self.assertEqual((revise["tool"], revise["arguments"]["threadId"]), ("t3_thread_send", f"child-{lead['worker']}"))
        self.assertIn("The user's answer to your spec: Split the first task in two.", revise["arguments"]["message"])
        self.assertIn("worker submit", revise["arguments"]["message"])
        self.assertTrue(self.worker("submit", *worker_command(lead), "--file", self.spec_file(goal="Sign in by name."))["ok"])
        (again,) = self.orchestrator("next")["ask"]
        self.assertNotEqual(again["id"], ask["id"])
        self.assertIn("Goal: Sign in by name.", again["question"])
        self.assertEqual(self.orchestrator("answer", ask=again["id"], answer="drop")["state"], "dropped")
        self.assertIn("Dropped the spec of plan M31.", self.orchestrator("next")["tell"])
        self.assertEqual((self.status()["specs"][0]["state"], self.status()["tasks"]), ("dropped", []))
        self.assertTrue(self.orchestrator("spec", plan="M31", request=self.REQUEST)["ok"], "the name is free again")

    def test_a_lead_that_ends_without_submitting_is_asked_once_then_blocked_until_you_restart_or_drop_it(self):
        lead = self.to_spec()
        self.end(lead)
        (nudge,) = self.start()
        self.assertIn("worker submit", nudge["arguments"]["message"])
        self.end(nudge)
        step = self.orchestrator("next")
        self.assertEqual(step["calls"], [])
        self.assertIn("Spec (M31) is blocked: its lead ended twice without reporting. Say restart to give it a new lead, "
                      "or drop it.", step["tell"])
        self.assertEqual(step["tell"][-1], "Summary: nothing more can move without you. Waiting for you: the spec of plan "
                                           "M31 is blocked: its lead ended twice without reporting. So far 0 merged, 0 dropped.")
        self.assertFalse(self.orchestrator("restart", spec="M31")["ok"], "a restart needs your own words")
        self.assertTrue(self.orchestrator("restart", spec="M31", authorization="Restart the lead.")["ok"])
        (fresh,) = self.start()
        self.assertEqual(fresh["tool"], "delegate_task")
        self.assertNotEqual(fresh["worker"], lead["worker"])
        dropped = self.orchestrator("spec", plan="M31", drop=True)
        self.assertEqual(dropped["state"], "dropped")
        (cancel,) = self.start()
        self.assertEqual((cancel["tool"], cancel["worker"]), ("task_cancel", fresh["worker"]))

    def test_a_quiet_lead_is_replaced_once_without_you(self):
        lead = self.to_spec()
        self.assertTrue(self.quiet(lead)["send"])
        cancel, fresh = self.start()
        self.assertEqual((cancel["tool"], fresh["tool"], fresh["arguments"]["role"]), ("task_cancel", "delegate_task", "review"))
        self.assertIn(f"{lead['worker']}, the lead of plan M31 was quiet since 2026-11-23T09:00:00Z, so it was stopped and "
                      f"a new lead took over.", self.last_step["tell"])
        self.assertTrue(self.quiet(fresh, "2026-11-23T10:00:00Z")["send"])
        (cancel,) = self.start()
        self.assertEqual((cancel["tool"], cancel["worker"]), ("task_cancel", fresh["worker"]))
        self.assertEqual(self.status()["specs"][0]["state"], "blocked")

    FABLE = [{"slug": "claude-fable-5-2", "capabilities": {"optionDescriptors": [
        {"id": "effort", "options": levels(*CLAUDE_EFFORTS)}]}}]

    def test_a_lead_stopped_at_its_quota_is_replaced_at_once_by_a_stand_in_on_opus(self):
        self.t3_caches(claude=CLAUDE_LIST + self.FABLE)
        lead = self.to_spec()
        self.assertEqual(lead["arguments"]["target"]["model"], "claude-fable-5-2")
        self.end(lead, "failed", CLAUDE_STOP)
        (fresh,) = self.start()
        self.assertEqual((fresh["tool"], fresh["arguments"]["target"]["model"]), ("delegate_task", "claude-opus-5-5"))
        self.assertIn("lead", self.jev_roles, "Jev is asked for the stand-in")
        self.assertIn("claude-fable-5-2 is at its usage limit, so a new lead runs on claude-opus-5-5 until it resets.",
                      self.last_step["tell"])
        self.assertEqual(self.last_done["tell"], ["Lead for plan M31 started on claude-opus-5-5 at high (a stand-in for "
                                                  "claude-fable-5-2)."])

    def test_the_lead_has_its_own_model_setting_which_models_status_and_pick_show(self):
        self.t3_caches(claude=CLAUDE_LIST + self.FABLE)
        models = self.orchestrator("models")
        self.assertEqual(models["defaults"]["lead"], {"model": "claude-fable-5-2", "effort": "high", "runs": True})
        self.bind()
        picked = self.orchestrator("pick", role="lead", model="claude-opus-5-5", effort="max", authorization="Opus at max.")
        self.assertTrue(picked["ok"], picked)
        self.assertEqual(self.status()["models"]["lead"], {"model": "claude-opus-5-5", "modelSource": "pick",
                                                           "effort": "max", "effortSource": "pick"})
        self.assertTrue(self.orchestrator("spec", plan="M31", request=self.REQUEST)["ok"])
        self.start()
        self.assertEqual(self.last_done["tell"], ["Lead for plan M31 started on claude-opus-5-5 at max (your choice)."])

    def test_a_binding_an_earlier_version_made_gets_the_leads_default(self):
        self.bind()
        db = store.connect(self.db)
        try:
            with db:
                config = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (PROJECT,)).fetchone()[0])
                for key in ("lead", "leadEffort"):
                    del config["defaults"][key]
                db.execute("UPDATE projects SET config=? WHERE id=?", (store.packed(config), PROJECT))
        finally:
            db.close()
        self.assertEqual(self.status()["models"]["lead"], {"model": "claude-fable-5-2", "modelSource": "default",
                                                           "effort": "high", "effortSource": "default"})


class QuotaTests(unittest.TestCase):
    """How the end of a failed run, as T3's wake notice gives it, tells a quota from any other failure, and its reset."""
    AT = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)

    def until(self, text):
        found = flow.quota_until(text, self.AT)
        return found and datetime.fromisoformat(found)

    def test_a_quota_that_says_when_it_resets_is_out_until_then(self):
        self.assertEqual(self.until(QUOTA_STOP), self.AT + timedelta(hours=4, minutes=11, seconds=25))
        self.assertEqual(self.until("You've hit your usage limit. Upgrade to Pro, or try again in 2 hours 5 minutes."),
                         self.AT + timedelta(hours=2, minutes=5))

    def test_a_reset_given_as_a_clock_time_is_its_next_one_in_the_zone_it_names(self):
        until = self.until("Claude API rate limit reached. Try again later.\n\nLast assistant message before the run "
                           "failed:\n\nYou've hit your 5-hour limit · resets 1:50am (Europe/Berlin)")
        try:
            zone = zoneinfo.ZoneInfo("Europe/Berlin")
        except zoneinfo.ZoneInfoNotFoundError:  # Windows without tzdata: the clock is read in this computer's zone.
            zone = None
        self.assertTrue(self.AT < until <= self.AT + timedelta(days=1), until)
        self.assertEqual(until.astimezone(zone).strftime("%H:%M"), "01:50")

    def test_a_quota_that_names_no_reset_is_tried_again_after_half_an_hour(self):
        for text in ("Please check your account with your provider to add more credits, then resend your message to continue.",
                     "Claude API rate limit reached. Try again later."):
            self.assertEqual(self.until(text), self.AT + timedelta(minutes=30), text)

    def test_any_other_failure_is_no_quota_even_when_the_workers_own_words_name_a_limit(self):
        for text in ("The provider event stream closed unexpectedly. Retry the turn; if it keeps failing, check the "
                     "provider and server logs.", "Failed to authenticate. API Error: 403", "Provider turn failed.",
                     "Claude API unknown. Last assistant message before the run failed: I will add a rate limit to "
                     "the login route, then a usage limit page that resets in 5 minutes.", "", None):
            self.assertIsNone(flow.quota_until(text, self.AT), text)


if __name__ == "__main__":
    unittest.main()
