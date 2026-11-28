# Setting up a project

Setup makes a Claude or Codex session in a T3 project that project's orchestrator. It runs on the
user's message asking for it with "use t3 workflow". It creates no other session: workers start
later, as delegated tasks. Requirements:
- the project is hosted on GitHub
- this computer has the workflow installed (see the README in the parent folder)
- `gh` is signed in with an account that can push to the repo and change its settings

## 1. Gather the facts

Until the binding exists, your hook does not guard this session. Read and ask; change nothing.

- **The T3 project id and this thread's id:** from T3's own tools. Never guess them.
- **The project folder:** the absolute path of the T3 project's checkout.
- **The repo:** `git remote get-url origin` must point to GitHub; take `owner/name` from it. If there
  is no GitHub remote, stop and tell the user the workflow needs one.
- **The branch tasks merge into:** the repo's default branch
  (`gh repo view <owner/name> --json defaultBranchRef`).
- **`requiredChecks`:** the CI checks that run on pull requests to that branch, named as GitHub shows
  them (`gh pr checks` on a recent PR, or the job names in `.github/workflows/`). If there is no CI
  yet, see [no CI yet](#no-ci-yet).
- **`checks`:** the local checks `worker pr` runs in each implementer's checkout before review. Take
  them from the project's instructions or package scripts, and give each `argv` as a list. Do not run
  them now. If none apply, write `noChecksReason` instead, saying why.

## 2. Ask about models, DevOps, docs and guarded paths

Each role has a default model and effort. The user can choose, for each role, any model their T3 runs,
any effort it offers, both, or neither. Run `models` the way step 3 runs `init`, with the ids from
step 1:

```json
{"op":"models","projectId":"<T3 project id>","threadId":"<this thread's id>","search":"<words>"}
```

- If `known` is false, tell the user its `reason` and go on: no model can be picked, only efforts.
- Otherwise tell the user which `providers` T3 can run, and why any other cannot. If none is ready,
  stop: T3 needs Claude, Codex, goose or Antigravity enabled and signed in.
- Then, for the implementer, the reviewer, DevOps, the lead and the docs worker in turn, show the role's
  default model and effort from `defaults` (for the implementer, the two `models` that take turns), and
  ask for a model, an effort, both, or a skip. When `runs` is false, this T3 cannot run the default, so
  the user picks a model; `init` refuses the reviewer and DevOps without one. The implementer, the lead
  and the docs worker start on another approved model T3 runs (the lead on Opus 5.5; the docs worker on
  a subscription model when goose cannot run Gemini 4 Carbon).
- Search with the user's words and show the matches with their `provider`; `more` counts the ones not
  shown. Offer only a model the list returned.
- Offer the efforts in the chosen model's `efforts` (none when it is empty). Without a chosen model,
  offer low, medium, high, xhigh or max.
- A role runs on the model and effort the user chose, else the default: the implementer on Opus 5.5 and
  Gemini 4 Neon in turns at `xhigh` (each new one on the one with fewer unfinished tasks; a model the
  user picks replaces the pair). Jev never sets an effort. A reviewer of the same family as the model that wrote its PR (its
  maker: Anthropic, OpenAI, Google) runs instead on the best-scored approved reviewer of another family.
- A model runs on its first route with capacity (Opus: Claude, Antigravity, then goose). Paid goose
  routes are offered only while no subscription route has capacity, except for a default that runs only
  on goose: the docs worker's Gemini 4 Carbon, paid through OpenRouter. While a model has no route with
  capacity (T3 shows it spent, or a run stopped at its quota), Jev starts that role's new workers on an
  approved model with capacity, over the user's choice too, until the limit resets. It reads the ability
  cards, the record and the task's size. Without its answer, the best-scored one starts.

Add the choices to `init`, leaving out what the user skipped:
`"picks":{"reviewer":{"model":"<model>","effort":"max"},"implementer":{"effort":"xhigh"}}`.

Then ask whether a DevOps worker merges the PRs (the default), or the harness, with no model. For the
harness, add `"devops":false` to `init`. Without DevOps, the harness brings each confirmed PR up to
date with the branch, waits for CI and merges it. Every CI failure goes back to the implementer, which
asks the user when CI itself is broken. DevOps also fixes the pipeline; the harness does not.

Then ask whether a docs worker updates the docs when a plan's tasks finish (the default), and which
paths it may change: by default `README.md`, `CHANGELOG.md` and `docs/`. It changes no code, and edits a
changelog only if one exists. For other paths, add `"docs":["README.md","site/"]` to `init`; for none,
`"docs":false`.

Then ask which paths, if any, are guarded: paths no PR changes without the user's word, such as
database migrations or deploy configuration. Write them the way a plan's `owns` does, for example
`db/migrations/`. A PR that changes a file under one waits for the user's answer before it merges, even
inside its task's paths. Add them to `init` as `"guarded":["db/migrations/"]`, or leave it out for none.

## 3. Bind it

Run the harness directly; the `t3-workflow` form works only once this thread is the orchestrator.
`<install>` is this workflow's folder, the parent of the folder this file is in.

```bash
python "<install>/scripts/harness.py" <<'EOF'
{"op":"init","projectId":"<T3 project id>","threadId":"<this thread's id>","workspace":"<project folder>","repo":"owner/name","branch":"main","requiredChecks":["<CI check>"],"checks":[{"name":"tests","argv":["npm","test"],"cwd":".","timeoutSeconds":900}]}
EOF
```

In Codex's PowerShell, pipe a here-string instead, and keep the JSON ASCII (write other characters as
`\uXXXX`):

```powershell
@'
{"op":"init", ...the same fields...}
'@ | python "<install>/scripts/harness.py"
```

`init`'s fields:

| Field | Meaning |
|---|---|
| `projectId`, `threadId` | The T3 project and the orchestrator's thread |
| `workspace` | The project folder, absolute |
| `repo` | The GitHub repo, `owner/name` |
| `branch` | The branch tasks merge into (default `main`) |
| `requiredChecks` | The CI check names that must pass before a merge |
| `checks` | Local checks `worker pr` runs, each `{"name","argv","cwd","timeoutSeconds"}`. Give `argv` as a list, not shell text; `cwd` is relative to the project folder. |
| `noChecksReason` | Why there are no local checks, when `checks` is empty |
| `defaults` | Models and efforts without the user's choice: implementer Opus 5.5 at `xhigh` (taking turns with Gemini 4 Neon), reviewer GPT-6.2 Astra at `xhigh`, DevOps Sonnet 5.5 at `high`, lead Fable 5.2 at `high`, docs worker Gemini 4 Carbon at `high`. The implementer's model is always the shipped one; the user's own is a pick. |
| `quietMinutes` | Minutes before a quiet worker is replaced (default 30) |
| `picks` | The user's model and effort per role, from step 2 |
| `devops` | `false` merges without a DevOps worker (default `true`), from step 2 |
| `docs` | `false` for no docs worker, or the paths it may change (default on, with `README.md`, `CHANGELOG.md`, `docs/`), from step 2 |
| `guarded` | Paths whose changes wait for the user's answer before a merge (default none), from step 2 |

The binding starts paused. If it says the project is already bound, point the user to its orchestrator
thread and stop. `init` gives the project its own copy of the workflow, with its own data: its hooks,
workers and watcher run that copy, so nothing another project's sessions run or change reaches it. Every
T3 session of the project, in every turn, now stays inside the project's folder and worktrees. From now
on, your hook guards this thread as the orchestrator: in a workflow turn,
you can only read, use T3's tools, and run `t3-workflow`. On Codex, read with Get-Content,
Get-ChildItem, rg or Select-String, one plain command without pipes or redirection.

## 4. Protect the branch

`setup_github` sets the repo to squash-only merges, branch deletion on merge, update-branch on and
auto-merge off. It protects the branch with a required PR (0 approvals), the CI checks plus
`t3/review` on an up-to-date branch, admins included, and no force-push or deletion. It changes only
what differs.

Tell the user what it changes and run it with their own words as `authorization`. Its result lists
what it changed; running it again changes nothing. If GitHub refuses branch protection, the repo is
private on a plan without it: tell the user.

## 5. Report

Tell the user:
- the binding: the repo, the branch, and both kinds of checks
- what `setup_github` changed
- the model and effort each role starts on, and that they can change either later with "use t3
  workflow: use <model> for the reviewer" or "use t3 workflow: use max effort for the implementer",
  or go back with "use t3 workflow: back to default for the reviewer"
- whether DevOps merges, and that "use t3 workflow: turn DevOps off" (or on) changes it for plans that
  start later
- whether the docs worker runs and on which paths, and that "use t3 workflow: turn the docs worker off"
  (or on, or new paths) changes it for plans that start later
- the guarded paths, if any, and that "use t3 workflow: guard <path>" (or stop guarding it) changes
  them
- that when GitHub cannot run CI, as when its billing stops Actions, "use t3 workflow: turn CI off"
  lets PRs merge on their local checks and review, and "turn CI on" brings CI back
- that the binding is paused, and work starts once they say to activate it
- that they choose this thread's model and effort in T3, and can change them at any time. Any Claude
  or Codex model works: T3 keeps the thread's id when it moves between them, and the binding and its
  tasks live in the harness, not in the thread.
- that before a Codex model runs this thread, they trust the workflow's hooks once: run `codex`, type
  `/hooks` and trust the three `t3-workflow hook` hooks. Without that, Codex skips them, and `t3-workflow`
  does not run.
- that T3 Code's mobile app can alert them when this thread finishes a turn or needs them, as the
  README's [alerts on your phone](../README.md#alerts-on-your-phone) says

Never ask the user to switch your model.

On their words, run `control` with `activate` and their words as `authorization`, then plan with them
as [orchestrator.md](orchestrator.md) says.

## No CI yet

Make CI the first plan: one task that adds the pull-request workflow, owning `.github/workflows/`. Its
job name must match `requiredChecks`, because the check first reports on that task's own PR. The same
task adds the standby deploy workflow below.

## CD on standby

Deployment waits until the user names a server. Until then, `deploy.yml` runs on pushes to the main
branch and skips itself until the repo variable `DEPLOY_TARGET` is set. Its one step fails, so setting
the variable early never looks like a deploy:

```yaml
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    if: vars.DEPLOY_TARGET != ''
    runs-on: ubuntu-latest
    steps:
      - run: echo "No deploy steps yet; they are written once the server is named." && exit 1
```

## Moving from the old workflow

- The first harness call after the upgrade moves every old binding aside, into the database's
  `retired_bindings` table. It touches no thread.
- Set each GitHub project up again from the thread that should be its orchestrator. The old
  orchestrator thread is a good choice.
- Archive the old coordinator, implementer, reviewer and decision-maker threads in T3.
- The old `<project>-tracks` folders can go once their work is merged or dropped.

A binding's settings are fixed at `init`, except the models and efforts the user chooses, which `pick`
changes, the guarded paths, which `guarded` changes, and DevOps, the docs worker and CI, which `devops`,
`docs` and `ci` turn on or off.
