---
name: t3-event-workflow
description: Run a GitHub project in T3 Code through one orchestrator session that plans with the user and reports to them, while implementer, reviewer and DevOps workers run in the background as delegated tasks. Each task becomes a branch and a pull request; DevOps merges it through GitHub once it is reviewed and confirmed. Use for setup, planning in a bound orchestrator, worker wakes, and controls, when the user says "use t3 workflow".
---

# T3 event workflow

The user talks to one session per project: the orchestrator. It plans the work with them and reports
back. Everything else runs in the background, as T3 delegated tasks the orchestrator starts:

| Role | Does | Model |
|---|---|---|
| Orchestrator | Plans with the user, makes the harness's calls, reports | Any Claude or Codex model, picked by the user in T3 |
| Implementer | One task in its own T3 checkout, ending in one PR | The user's choice, else Jev's, else GPT-6.1-Sol, `xhigh` |
| Reviewer | Reads the PR at one commit and approves it or asks for changes | The user's choice, else Jev's, else Sonnet 5.5, `xhigh` |
| DevOps | One per plan: updates and squash-merges confirmed PRs, triages CI, owns the pipeline. The user can turn it off; then the harness merges | The user's choice, else Opus 5.5, `xhigh` |

A task merges only when all of these hold:
- the reviewer approved the PR's exact head commit (the `t3/review` status)
- the orchestrator's rule confirmed it: that commit is still the head, CI has not failed, and only
  the task's paths changed (otherwise the user decides)
- GitHub's protection on the main branch passes: the required checks and `t3/review` are green on an
  up-to-date branch

[harness.py](scripts/harness.py) owns every transition, and the binding lives in `state/`. Finished
workers wake the orchestrator. One background process runs: the watcher, which reports workers that
have been quiet too long.

- **Setup:** [setup.md](references/setup.md)
- **The orchestrator:** [orchestrator.md](references/orchestrator.md) and the
  [protocol](references/protocol.md)
- **Workers:** [implementer.md](references/implementer.md), [reviewer.md](references/reviewer.md),
  [devops.md](references/devops.md), as each brief says
- **When the workflow applies:** [session-default.md](references/session-default.md)

The workflow is opt-in: it runs only when the user says "use t3 workflow", and in the orchestrator's
own wakes.
