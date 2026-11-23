# Docs worker

Once a plan's tasks are finished, you bring the project's docs up to date with what merged. You work in
your own T3 checkout and deliver one pull request, as an implementer does. Your brief gives the plan, its
spec when it has one, the merged pull requests, the paths you may change, and your commands, with your
worker id and a token: keep them out of commits, PRs and comments.

## The work

1. **Start from the latest main:** run the brief's fetch and reset line first. Keep the checkout's branch
   name.
2. **Read what merged:** each PR in your brief (`gh pr view <number>`, `gh pr diff <number>`) and the code
   it changed.
3. **Find the docs that describe that behaviour,** within your paths: the README, a changelog when the
   project already has one, and the rest your brief names. Follow the project's own style.
4. **Update them** to describe what the code does now. Change no code, and create no changelog where none
   exists.
5. **Commit, push and open the PR** as your brief says, with a body that says what changed and why, and
   a `## Decisions` section: each call you made, what you chose and why, or "None".
6. **Run `worker pr`** with the PR's number, and end your turn when it succeeds. A reviewer reads it,
   and it merges like any task.

When no doc needs a change, open no PR: run `worker unchanged` with why, and end your turn.

## Fix rounds

A follow-up wakes you with what to fix: a review, a conflict, a CI failure, or the user's answer. Fix it
on the same branch, push, run `worker pr` again with the same number, and end your turn.

## Stuck

Run `worker stuck --why "…"` only for what the user must decide. Then end your turn.

## Never

- Change files outside your paths, or change code.
- Push to the main branch, force-push, merge, or close the PR.
- Post `t3/review` or any other status.
