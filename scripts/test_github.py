"""The GitHub layer (seam 2): what each call sends to `gh`, and how GitHub's answers come back.

A fake `gh` stands in for the real one. It answers `gh api` by method and path, the way the real one does:
the response body on stdout, and on an HTTP error exit code 1 with `gh: <message> (HTTP <status>)` on stderr.
"""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import github

REPO = "owner/app"


class FakeGh:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def on(self, method, path, *answers):
        """Each answer is a body (HTTP 200) or (status, body); successive calls take successive answers, the last repeats."""
        self.routes[(method, path)] = [a if isinstance(a, tuple) else (200, a) for a in answers]

    def __call__(self, argv, stdin=None, cwd=None, timeout=None):
        assert argv[0] == "gh", argv
        if argv[1] == "api":
            method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
            path = next(a for a in argv[2:] if not a.startswith("-") and a not in {method, "-"})
            body = json.loads(stdin) if stdin else None
            self.calls.append((method, path, body))
            answers = self.routes.get((method, path))
            if not answers:
                return 1, json.dumps({"message": "Not Found"}), "gh: Not Found (HTTP 404)"
            status, reply = answers.pop(0) if len(answers) > 1 else answers[0]
            text = reply if isinstance(reply, str) else json.dumps(reply)
            if status >= 400:
                message = json.loads(text).get("message", "error") if text.startswith("{") else text
                return 1, text, f"gh: {message} (HTTP {status})"
            return 0, text, ""
        self.calls.append(tuple(argv[1:]))
        answers = self.routes.get(tuple(argv[1:3]))
        if not answers:
            return 1, "", "unknown command"
        status, reply = answers.pop(0) if len(answers) > 1 else answers[0]
        return (0, reply, "") if status < 400 else (1, "", reply)


def hub(fake, sleep=lambda seconds: None):
    return github.GitHub(REPO, run=fake, sleep=sleep)


class ReviewStatusTests(unittest.TestCase):
    def test_an_approval_posts_t3_review_success_on_the_reviewed_commit(self):
        fake = FakeGh()
        fake.on("POST", f"repos/{REPO}/statuses/abc123", {"state": "success"})
        hub(fake).approve("abc123", "Reviewed by the t3 workflow reviewer")
        self.assertEqual(fake.calls, [("POST", f"repos/{REPO}/statuses/abc123",
                                       {"state": "success", "context": "t3/review",
                                        "description": "Reviewed by the t3 workflow reviewer"})])


def pull_body(number=7, sha="head1", ref="t3/fix-login", state="open", merged=False):
    return {"number": number, "state": state, "merged": merged, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "head": {"sha": sha, "ref": ref}, "base": {"ref": "main"}, "merge_commit_sha": None}


class PullTests(unittest.TestCase):
    def test_a_pull_request_reads_its_head_commit_branch_and_every_changed_file(self):
        fake = FakeGh()
        fake.on("GET", f"repos/{REPO}/pulls/7", pull_body())
        page_one = [{"filename": f"src/f{i}.ts"} for i in range(100)]
        fake.on("GET", f"repos/{REPO}/pulls/7/files?per_page=100&page=1", page_one)
        fake.on("GET", f"repos/{REPO}/pulls/7/files?per_page=100&page=2", [{"filename": "README.md"}])
        pr = hub(fake).pull(7)
        self.assertEqual((pr["number"], pr["head"], pr["branch"], pr["base"], pr["state"]),
                         (7, "head1", "t3/fix-login", "main", "open"))
        self.assertEqual(len(pr["files"]), 101)
        self.assertEqual(pr["files"][-1], "README.md")

    def test_a_missing_pull_request_is_a_failure_with_its_status(self):
        with self.assertRaises(github.Failed) as caught:
            hub(FakeGh()).pull(99)
        self.assertEqual(caught.exception.status, 404)


def run(name, status="completed", conclusion="success", started="2026-11-10T10:00:00Z"):
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started}


class CheckTests(unittest.TestCase):
    def checks(self, runs, statuses, names):
        fake = FakeGh()
        fake.on("GET", f"repos/{REPO}/commits/s1/check-runs?per_page=100", {"total_count": len(runs), "check_runs": runs})
        fake.on("GET", f"repos/{REPO}/commits/s1/status", {"statuses": statuses})
        return hub(fake).checks("s1", names)

    def test_each_named_check_reads_as_success_failure_pending_or_missing(self):
        found = self.checks(
            [run("acceptance"), run("lint", conclusion="failure"), run("e2e", status="in_progress", conclusion=None),
             run("docs", conclusion="skipped")],
            [{"context": "t3/review", "state": "success"}],
            ["acceptance", "lint", "e2e", "docs", "t3/review", "flutter"])
        self.assertEqual(found, {"acceptance": "success", "lint": "failure", "e2e": "pending", "docs": "success",
                                 "t3/review": "success", "flutter": "missing"})

    def test_a_rerun_check_reads_by_its_latest_run(self):
        found = self.checks([run("acceptance", conclusion="failure", started="2026-11-10T10:00:00Z"),
                             run("acceptance", status="queued", conclusion=None, started="2026-11-10T11:00:00Z")],
                            [], ["acceptance"])
        self.assertEqual(found, {"acceptance": "pending"})

    def test_a_failed_or_errored_status_reads_as_failure(self):
        found = self.checks([], [{"context": "t3/review", "state": "error"}, {"context": "ci", "state": "pending"}],
                            ["t3/review", "ci"])
        self.assertEqual(found, {"t3/review": "failure", "ci": "pending"})


class UpdateTests(unittest.TestCase):
    def fake(self, behind_by):
        fake = FakeGh()
        fake.on("GET", f"repos/{REPO}/compare/main...head1", {"behind_by": behind_by, "ahead_by": 2})
        fake.on("GET", f"repos/{REPO}/pulls/7/files?per_page=100&page=1", [])
        return fake

    def test_a_branch_already_on_main_is_current_and_left_alone(self):
        fake = self.fake(0)
        fake.on("GET", f"repos/{REPO}/pulls/7", pull_body())
        self.assertEqual(hub(fake).update(7, "head1"), ("current", "head1"))
        self.assertFalse([c for c in fake.calls if c[0] == "PUT"])

    def test_a_branch_behind_main_is_updated_and_waits_for_its_new_head(self):
        fake = self.fake(3)
        fake.on("PUT", f"repos/{REPO}/pulls/7/update-branch", (202, {"message": "Updating pull request branch."}))
        fake.on("GET", f"repos/{REPO}/pulls/7", pull_body(sha="head1"), pull_body(sha="head1"), pull_body(sha="head2"))
        waits = []
        self.assertEqual(hub(fake, sleep=waits.append).update(7, "head1"), ("updated", "head2"))
        self.assertIn(("PUT", f"repos/{REPO}/pulls/7/update-branch", {"expected_head_sha": "head1"}), fake.calls)
        self.assertEqual(len(waits), 2)

    def test_a_conflict_with_main_is_reported_not_raised(self):
        fake = self.fake(3)
        fake.on("PUT", f"repos/{REPO}/pulls/7/update-branch", (422, {"message": "merge conflict between base and head"}))
        self.assertEqual(hub(fake).update(7, "head1"), ("conflict", "head1"))

    def test_an_update_github_never_applies_fails(self):
        fake = self.fake(3)
        fake.on("PUT", f"repos/{REPO}/pulls/7/update-branch", (202, {}))
        fake.on("GET", f"repos/{REPO}/pulls/7", pull_body(sha="head1"))
        with self.assertRaises(github.Failed):
            hub(fake).update(7, "head1")


DIFF = """diff --git a/src/login.ts b/src/login.ts
index 1111111..2222222 100644
--- a/src/login.ts
+++ b/src/login.ts
@@ -10,7 +10,7 @@ export function login(user) {
   const name = user.name;
-  return greet(nmae);
+  return greet(name);
 }
"""


class IdentityTests(unittest.TestCase):
    def identities(self, *diffs):
        fake = FakeGh()
        fake.on("pr", "diff", *diffs)
        gh = hub(fake)
        found = [gh.identity(7) for _ in diffs]
        self.assertEqual(fake.calls[0], ("pr", "diff", "7", "--repo", REPO))
        return found

    def test_the_same_change_at_other_lines_and_blob_ids_keeps_its_identity(self):
        moved = DIFF.replace("index 1111111..2222222", "index 3333333..4444444").replace("@@ -10,7 +10,7 @@", "@@ -14,7 +14,7 @@")
        first, second = self.identities(DIFF, moved)
        self.assertEqual(first, second)

    def test_a_changed_line_changes_the_identity(self):
        first, second = self.identities(DIFF, DIFF.replace("greet(name)", "greet(name.trim())"))
        self.assertNotEqual(first, second)

    def test_a_diff_github_cannot_give_has_no_identity(self):
        fake = FakeGh()
        fake.on("pr", "diff", (406, "diff exceeded the maximum number of lines"))
        self.assertIsNone(hub(fake).identity(7))


class MergeTests(unittest.TestCase):
    def test_a_merge_squashes_only_the_commit_that_was_checked(self):
        fake = FakeGh()
        fake.on("PUT", f"repos/{REPO}/pulls/7/merge", {"merged": True, "sha": "m1", "message": "Pull Request successfully merged"})
        self.assertEqual(hub(fake).merge(7, "head2", "M30: Task 4 (#7)"), "m1")
        self.assertEqual(fake.calls, [("PUT", f"repos/{REPO}/pulls/7/merge",
                                       {"merge_method": "squash", "sha": "head2", "commit_title": "M30: Task 4 (#7)"})])

    def test_a_merge_github_refuses_is_a_failure_with_its_reason(self):
        fake = FakeGh()
        fake.on("PUT", f"repos/{REPO}/pulls/7/merge", (405, {"message": "Required status check \"acceptance\" is expected."}))
        with self.assertRaises(github.Failed) as caught:
            hub(fake).merge(7, "head2", "M30: Task 4 (#7)")
        self.assertEqual(caught.exception.status, 405)
        self.assertIn("acceptance", str(caught.exception))


WANTED_SETTINGS = {"allow_squash_merge": True, "allow_merge_commit": False, "allow_rebase_merge": False,
                   "delete_branch_on_merge": True, "allow_update_branch": True, "allow_auto_merge": False}
WANTED_PROTECTION = {"required_status_checks": {"strict": True, "checks": [{"context": "acceptance"}, {"context": "t3/review"}]},
                     "enforce_admins": True, "required_pull_request_reviews": {"required_approving_review_count": 0},
                     "restrictions": None, "allow_force_pushes": False, "allow_deletions": False}


class SetupTests(unittest.TestCase):
    def test_a_new_repo_gets_squash_only_settings_and_protected_main(self):
        fake = FakeGh()
        fake.on("GET", f"repos/{REPO}", {**WANTED_SETTINGS, "allow_merge_commit": True, "delete_branch_on_merge": False})
        fake.on("PATCH", f"repos/{REPO}", {})
        fake.on("GET", f"repos/{REPO}/branches/main/protection", (404, {"message": "Branch not protected"}))
        fake.on("PUT", f"repos/{REPO}/branches/main/protection", {})
        changed = hub(fake).setup("main", ["acceptance"])
        self.assertEqual(changed, ["allow_merge_commit", "delete_branch_on_merge", "protection of main"])
        self.assertIn(("PATCH", f"repos/{REPO}", WANTED_SETTINGS), fake.calls)
        self.assertIn(("PUT", f"repos/{REPO}/branches/main/protection", WANTED_PROTECTION), fake.calls)

    def test_a_repo_already_set_up_is_left_alone(self):
        fake = FakeGh()
        fake.on("GET", f"repos/{REPO}", WANTED_SETTINGS)
        fake.on("GET", f"repos/{REPO}/branches/main/protection", {
            "required_status_checks": {"strict": True, "contexts": ["t3/review", "acceptance"],
                                       "checks": [{"context": "t3/review", "app_id": None}, {"context": "acceptance", "app_id": 15368}]},
            "enforce_admins": {"enabled": True}, "required_pull_request_reviews": {"required_approving_review_count": 0},
            "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False}})
        self.assertEqual(hub(fake).setup("main", ["acceptance"]), [])
        self.assertEqual([c for c in fake.calls if c[0] != "GET"], [])


CHECKS_PATH = f"repos/{REPO}/branches/main/protection/required_status_checks"


class RequiredChecksTests(unittest.TestCase):
    """CI off and on: only the binding's CI checks leave or rejoin main's required checks."""

    def test_ci_off_takes_only_the_ci_checks_off_mains_required_checks(self):
        fake = FakeGh()
        fake.on("GET", CHECKS_PATH, {"strict": True, "contexts": ["t3/review", "acceptance", "lint"], "checks": [
            {"context": "t3/review", "app_id": None}, {"context": "acceptance", "app_id": 15368}, {"context": "lint", "app_id": 15368}]})
        fake.on("PATCH", CHECKS_PATH, {})
        self.assertEqual(hub(fake).require_checks("main", ["acceptance"], on=False), ["required checks of main"])
        self.assertEqual(fake.calls[-1], ("PATCH", CHECKS_PATH, {"strict": True, "checks": [
            {"context": "t3/review"}, {"context": "lint", "app_id": 15368}]}))

    def test_ci_on_puts_the_ci_checks_back_beside_the_rest(self):
        fake = FakeGh()
        fake.on("GET", CHECKS_PATH, {"strict": True, "contexts": ["t3/review"], "checks": [{"context": "t3/review", "app_id": None}]})
        fake.on("PATCH", CHECKS_PATH, {})
        self.assertEqual(hub(fake).require_checks("main", ["acceptance"], on=True), ["required checks of main"])
        self.assertEqual(fake.calls[-1], ("PATCH", CHECKS_PATH, {"strict": True, "checks": [
            {"context": "t3/review"}, {"context": "acceptance"}]}))

    def test_a_branch_that_requires_no_checks_or_already_matches_is_left_alone(self):
        fake = FakeGh()
        fake.on("GET", CHECKS_PATH, (404, {"message": "Required status checks not enabled"}))
        self.assertEqual(hub(fake).require_checks("main", ["acceptance"], on=False), [])
        fake.on("GET", CHECKS_PATH, {"strict": True, "contexts": ["t3/review"], "checks": [{"context": "t3/review", "app_id": None}]})
        self.assertEqual(hub(fake).require_checks("main", ["acceptance"], on=False), [])
        self.assertEqual([c for c in fake.calls if c[0] != "GET"], [])


if __name__ == "__main__":
    unittest.main()
