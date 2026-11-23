---
name: t3-event-workflow
description: Run a GitHub project in T3 Code through one orchestrator session that plans with the user and reports to them, while lead, implementer, reviewer, DevOps and docs workers run in the background as delegated tasks. A lead writes a spec and task split for the user to approve. Each task becomes a branch and a pull request; DevOps merges it through GitHub once it is reviewed and confirmed, and a docs worker updates the docs when a plan finishes. Use for setup, planning in a bound orchestrator, worker wakes, and controls, when the user says "use t3 workflow".
---

# T3 event workflow

The user talks to one session per project: the orchestrator. It plans the work with them and reports
back. Everything else runs in the background, as T3 delegated tasks the orchestrator starts:

| Role | Does | Model |
|---|---|---|
| Orchestrator | Plans with the user, makes the harness's calls, reports | Any Claude or Codex model, picked by the user in T3 |
| Lead | Reads the project, writes a spec and splits it into tasks; the user approves it before any task starts. It edits nothing | The user's choice, else Fable 5.2, `high` |
| Implementer | One task in its own T3 checkout, ending in one PR | The user's choice, else Opus 5.5, `xhigh` |
| Reviewer | Reads the PR at one commit, tries the change, and approves it or asks for changes | The user's choice, else GPT-6.2 Astra, `xhigh` |
| DevOps | One per plan: updates and squash-merges confirmed PRs, triages CI, owns the pipeline. The user can turn it off; then the harness merges | The user's choice, else Sonnet 5.5, `high` |
| Docs worker | One per finished plan with merged work: updates the README, an existing changelog and the binding's docs paths in one PR, or ends with no PR when nothing needs it. The user can turn it off | The user's choice, else Gemini 4 Neon, `high` |

A reviewer whose model is of the same family (maker) as the model that wrote its PR runs instead on the
best-scored approved reviewer of another family that can run, over the user's choice too.

Only a model with an ability card (`assets/cards.json`) is approved. A model may have several routes
(Opus: Claude, Antigravity, then goose); a worker starts on the first with capacity, at the nearest
effort it offers. Paid goose routes are offered only while no subscription route has capacity.
While a model has no route with capacity (T3 shows it spent, or a run stopped at its quota), Jev starts
that role's new workers on an approved model with capacity, over the user's choice too, until the limit
resets. Without Jev's answer, the best-scored one does. A worker stopped at its quota is replaced at
once; with no model left with capacity, its task waits for the reset and then starts by itself.

A task merges only when all of these hold:
- the reviewer approved the PR's exact head commit (the `t3/review` status)
- the orchestrator's rule confirmed it: that commit is still the head, CI has not failed, only the
  task's paths changed, and no guarded path did (otherwise the user decides)
- GitHub's protection on the main branch passes: the required checks and `t3/review` are green on an
  up-to-date branch

A task whose reviewer asked for changes 3 times waits for the user: it carries on with its implementer,
restarts with a new one, or is dropped.

[harness.py](scripts/harness.py) owns every transition, and the binding lives in `state/`. Finished
workers wake the orchestrator. One background process runs: the watcher, which notices workers that
have been quiet too long. Each is replaced once without the user; if the new one goes quiet too, its task
waits for the user.

- **Setup:** [setup.md](references/setup.md)
- **The orchestrator:** [orchestrator.md](references/orchestrator.md) and the
  [protocol](references/protocol.md)
- **Workers:** [lead.md](references/lead.md), [implementer.md](references/implementer.md),
  [reviewer.md](references/reviewer.md), [devops.md](references/devops.md), [docs.md](references/docs.md),
  as each brief says
- **When the workflow applies:** [session-default.md](references/session-default.md)

The workflow is opt-in: it runs only when the user says "use t3 workflow", and in the orchestrator's
own wakes.
