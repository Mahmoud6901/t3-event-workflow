"""GitHub calls for the delegated PR workflow, through the `gh` CLI. Standard library only.

Every call goes through one runner, run(argv, stdin=None, cwd=None, timeout=None) -> (code, stdout, stderr), which
tests replace. `gh api` prints the response body on stdout; on an HTTP error it exits 1 and names the status on stderr.
"""
import hashlib
import json
import re
import subprocess
import time

DEADLINE = 60  # Seconds for one gh call.
REVIEW_CONTEXT = "t3/review"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$")  # A hunk header, function context included.
UPDATE_POLLS, UPDATE_POLL_SECONDS = 30, 2  # How long an accepted branch update may take to show its new head.
SETTINGS = {"allow_squash_merge": True, "allow_merge_commit": False, "allow_rebase_merge": False,
            "delete_branch_on_merge": True, "allow_update_branch": True, "allow_auto_merge": False}


class Failed(Exception):
    """A GitHub call failed. status is the HTTP status when GitHub answered, else None."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def fingerprint(diff):
    """A diff without blob ids or hunk line numbers, hashed: the same change at other lines keeps it."""
    lines = [HUNK.sub("@@", line) for line in diff.splitlines() if not line.startswith("index ")]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def run_gh(argv, stdin=None, cwd=None, timeout=DEADLINE):
    try:
        done = subprocess.run(argv, input=stdin, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "", f"gh did not answer within {timeout} seconds"
    except OSError as error:
        return 1, "", f"gh could not run: {error}"
    return done.returncode, done.stdout, done.stderr


class GitHub:
    def __init__(self, repo, run=run_gh, sleep=time.sleep):
        self.repo = repo
        self.run = run
        self.sleep = sleep

    def api(self, method, path, body=None):
        argv = ["gh", "api", "-X", method, path]
        if body is not None:
            argv += ["--input", "-"]
        code, out, err = self.run(argv, stdin=json.dumps(body) if body is not None else None)
        if code != 0:
            found = re.search(r"\(HTTP (\d{3})\)", err or "")
            try:
                reply = json.loads(out) if out.strip() else None
            except ValueError:
                reply = None
            message = (reply or {}).get("message") if isinstance(reply, dict) else None
            raise Failed(message or (err or "gh failed").strip(), int(found.group(1)) if found else None, reply)
        return json.loads(out) if out.strip() else None

    def pull(self, number):
        """The PR as the workflow reads it: number, state, merged, head commit, branch, base, url, body and changed
        files."""
        pr = self.api("GET", f"repos/{self.repo}/pulls/{number}")
        files, page = [], 1
        while True:
            batch = self.api("GET", f"repos/{self.repo}/pulls/{number}/files?per_page=100&page={page}") or []
            files += [item["filename"] for item in batch]
            if len(batch) < 100:
                break
            page += 1
        return {"number": pr["number"], "state": pr["state"], "merged": bool(pr.get("merged")), "head": pr["head"]["sha"],
                "branch": pr["head"]["ref"], "base": pr["base"]["ref"], "url": pr.get("html_url"),
                "mergeCommit": pr.get("merge_commit_sha"), "body": pr.get("body") or "", "files": files}

    def checks(self, sha, names):
        """{name: success | failure | pending | missing} for each named check run or commit status on this commit.
        A check run that ran again reads by its latest run."""
        runs = (self.api("GET", f"repos/{self.repo}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs") or []
        statuses = (self.api("GET", f"repos/{self.repo}/commits/{sha}/status") or {}).get("statuses") or []
        found = {}
        for item in sorted(runs, key=lambda r: r.get("started_at") or ""):
            if item.get("status") != "completed":
                found[item["name"]] = "pending"
            else:
                found[item["name"]] = "success" if item.get("conclusion") in {"success", "neutral", "skipped"} else "failure"
        for item in statuses:  # The combined status already holds the latest per context.
            found[item["context"]] = {"success": "success", "pending": "pending"}.get(item.get("state"), "failure")
        return {name: found.get(name, "missing") for name in names}

    def update(self, number, head, base="main"):
        """Bring the PR's branch up to date with base through GitHub's update-branch, guarded by the head we know.
        Returns ("current" | "updated" | "conflict", the head commit afterwards)."""
        behind = (self.api("GET", f"repos/{self.repo}/compare/{base}...{head}") or {}).get("behind_by", 0)
        if not behind:
            return "current", head
        try:
            self.api("PUT", f"repos/{self.repo}/pulls/{number}/update-branch", {"expected_head_sha": head})
        except Failed as error:
            if error.status == 422 and "conflict" in str(error).lower():
                return "conflict", head
            raise
        for _ in range(UPDATE_POLLS):
            now = self.pull(number)["head"]
            if now != head:
                return "updated", now
            self.sleep(UPDATE_POLL_SECONDS)
        raise Failed(f"GitHub accepted the update of PR #{number} but its branch did not move within "
                     f"{UPDATE_POLLS * UPDATE_POLL_SECONDS} seconds")

    def identity(self, number):
        """A fingerprint of the PR's own changes against main: its diff without blob ids or hunk line numbers, so the
        same change after main moved keeps it. None when GitHub cannot give the diff; treat that as changed."""
        code, out, _ = self.run(["gh", "pr", "diff", str(number), "--repo", self.repo])
        return fingerprint(out) if code == 0 else None

    def merge(self, number, sha, title):
        """Squash-merge the PR only if its head is still sha. Returns the merge commit."""
        return self.api("PUT", f"repos/{self.repo}/pulls/{number}/merge",
                        {"merge_method": "squash", "sha": sha, "commit_title": title})["sha"]

    def setup(self, branch, required):
        """Squash-only merges that delete the branch, and protection on branch: a PR, the required checks and
        t3/review on an up-to-date branch, admins included, no force-push or deletion. Changes only what differs and
        returns what it changed."""
        changed = []
        current = self.api("GET", f"repos/{self.repo}") or {}
        changed += [key for key, value in SETTINGS.items() if current.get(key) != value]
        if changed:
            self.api("PATCH", f"repos/{self.repo}", SETTINGS)
        contexts = [*required, REVIEW_CONTEXT]
        try:
            rules = self.api("GET", f"repos/{self.repo}/branches/{branch}/protection") or {}
        except Failed as error:
            if error.status != 404:
                raise
            rules = {}
        checks = rules.get("required_status_checks") or {}
        reviews = rules.get("required_pull_request_reviews")
        if not (checks.get("strict") is True and set(checks.get("contexts") or []) == set(contexts)
                and (rules.get("enforce_admins") or {}).get("enabled") is True
                and reviews is not None and reviews.get("required_approving_review_count") == 0
                and (rules.get("allow_force_pushes") or {}).get("enabled") is False
                and (rules.get("allow_deletions") or {}).get("enabled") is False):
            self.api("PUT", f"repos/{self.repo}/branches/{branch}/protection", {
                "required_status_checks": {"strict": True, "checks": [{"context": c} for c in contexts]},
                "enforce_admins": True, "required_pull_request_reviews": {"required_approving_review_count": 0},
                "restrictions": None, "allow_force_pushes": False, "allow_deletions": False})
            changed.append(f"protection of {branch}")
        return changed

    def required_checks(self, branch):
        """branch's required-checks rules, each check as {context, app_id}; None when it requires no checks."""
        try:
            rules = self.api("GET", f"repos/{self.repo}/branches/{branch}/protection/required_status_checks") or {}
        except Failed as error:
            if error.status == 404:
                return None
            raise
        checks = rules.get("checks") or [{"context": c} for c in rules.get("contexts") or []]
        return {"strict": rules.get("strict", True),
                "checks": [{k: v for k, v in c.items() if k in {"context", "app_id"} and v is not None} for c in checks]}

    def set_required_checks(self, branch, rules, checks):
        self.api("PATCH", f"repos/{self.repo}/branches/{branch}/protection/required_status_checks",
                 {"strict": rules["strict"], "checks": checks})
        return [f"required checks of {branch}"]

    def drop_ci_checks(self, branch):
        """CI off: every required check but t3/review leaves branch's required checks, since GitHub runs none of them.
        Returns (what it changed, the checks it removed as they were, to restore). A branch that requires no checks
        is left alone."""
        rules = self.required_checks(branch)
        removed = [c for c in (rules or {}).get("checks", []) if c["context"] != REVIEW_CONTEXT]
        if not removed:
            return [], []
        kept = [c for c in rules["checks"] if c["context"] == REVIEW_CONTEXT]
        return self.set_required_checks(branch, rules, kept), removed

    def restore_ci_checks(self, branch, checks):
        """CI on: the checks CI off removed, and the binding's own, rejoin branch's required checks beside the rest."""
        rules = self.required_checks(branch)
        if rules is None:
            return []
        present = {c["context"] for c in rules["checks"]}
        back = [c for c in checks if c["context"] not in present]
        return self.set_required_checks(branch, rules, rules["checks"] + back) if back else []

    def approve(self, sha, description):
        """The reviewer's approval: t3/review success on exactly this commit."""
        self.api("POST", f"repos/{self.repo}/statuses/{sha}",
                 {"state": "success", "context": REVIEW_CONTEXT, "description": description})
