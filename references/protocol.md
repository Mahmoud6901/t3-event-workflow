# Protocol

Every workflow call prints one JSON object: `{"ok": true, ...}`, or `{"ok": false, "error": "..."}`
with a reason you can act on.

The orchestrator runs `t3-workflow '<JSON>'` from its shell. Its hook adds the project and thread and
runs [harness.py](../scripts/harness.py).

## Orchestrator operations

| Operation | Example | Result |
|---|---|---|
| next | `{"op":"next"}` | `calls`, `tell`, `ask` |
| done | `{"op":"done","results":[{"call":"w1-start","taskId":"…","childThreadId":"…"}]}` | `recorded` |
| queue_set | `{"op":"queue_set","plans":[{"plan":"M30","owns":["src/login/"],"dependsOn":[],"tasks":[{"task":"Fix the login typo","brief":"…"},{"task":"Add the login test","brief":"…","after":["Fix the login typo"]}]}]}` | `plans` |
| queue_edit | `{"op":"queue_edit","plan":"M30","tasks":[{"task":"…","brief":"…"}]}`, `{"op":"queue_edit","plans":[…]}`, `{"op":"queue_edit","drop":"Fix the login typo"}`, `{"op":"queue_edit","task":"Add the login test","after":[],"why":"…"}` | `plans`, `tasks`, `dropped`, `relinked` |
| answer | `{"op":"answer","ask":"a7","answer":"merge"}` | `task`, `state` |
| restart | `{"op":"restart","task":"Fix the login typo","authorization":"<the user's words>"}` | `task`, `state` |
| control | `{"op":"control","action":"activate","authorization":"<the user's words>"}` | `control` |
| status | `{"op":"status"}` | plans with `dependsOn`, tasks with `after`, each role's model, working workers, open asks, problems |
| setup_github | `{"op":"setup_github","authorization":"<the user's words>"}` | `repo`, `changed` |
| models | `{"op":"models","search":"qwen"}` | `providers`, `defaults` (each with `runs`), `models` (each with `provider`, `efforts`) |
| pick | `{"op":"pick","role":"reviewer","model":"…","effort":"xhigh","authorization":"<the user's words>"}` | `model`, `effort`, each with its source |
| devops | `{"op":"devops","on":false,"authorization":"<the user's words>"}` | `devops`, `keep` |
| ci | `{"op":"ci","on":false,"authorization":"<the user's words>"}` | `ci`, `changed`, `freed` |

- **next** returns:
  - `calls`: each one has `call` (its id), `worker`, `tool` (`delegate_task`, `t3_thread_send` or
    `task_cancel`) and `arguments`. Each carries a `clientRequestId`, so making a call again is safe.
    A call stays in `calls` until `done` records it.
  - `tell`: lines for the user.
  - `ask`: questions for the user, each with an `id`, a `question` and, if it has them, `options`.
- **queue_set** starts a queue when none is running. Rules for its tasks:
  - `owns` lists the paths a plan's tasks may change; a task can narrow it with its own `owns`.
  - A task waits for its `after` tasks to merge and its plan's `dependsOn` plans to finish. The rest
    start at once, with no cap.
- **queue_edit** changes a running queue: it adds plans, adds tasks to a plan (a finished plan opens
  again), drops a task, or relinks. Dropping leaves the task's PR open on GitHub and counts as finished
  for the tasks after it.
  - **Relinking:** `task` with `after`, or `plan` with `dependsOn`, replaces that list; `[]` waits for
    nothing. A task's links change only while it is `waiting`, a loop is refused, and taking off a link
    to unfinished work needs `why`.
- **answer** answers an `ask`:
  - A PR that changes files outside its task's paths takes `merge`, `back` (to its implementer) or
    `drop`.
  - A stuck implementer's question takes the user's own words, which are sent to it.
- **restart** replaces the worker that holds a task (an implementer, reviewer or DevOps) with a new one
  in the same role.
- **control**: `activate`, `pause`, `resume` or `stop`. All but `pause` need the user's own words.
  Workers start only while the binding is active. Running workers finish their current step.
- **setup_github** applies the repo settings and protection in [setup](setup.md#4-protect-the-branch),
  and needs the user's own words.
- **pick** sets a role's model (from `models`), its effort, or both; the other stays. An effort is one of
  the model's `efforts`, or low to max alone. `"default"` puts one back (a model, if T3 runs it).
  Running workers keep theirs.
- **devops** turns DevOps on or off for plans that start later; `keep` lists running plans that stay as
  they are. Off, `next` merges each confirmed PR itself, one at a time, with DevOps's update and merge
  checks; the watcher wakes you when CI finishes. Every CI failure goes to the implementer.
- **ci** off, as when GitHub billing stops Actions: PRs merge on local checks and review, the branch
  requires only `t3/review`, and `freed` tasks go on. On puts back what it removed.

The hook also runs `ended` for each "Delegated task …" notice. You never run it yourself.

## Task states

| State | Meaning | What moves it |
|---|---|---|
| `waiting` | Its `after` tasks or `dependsOn` plans are not merged | `next` starts an implementer, or wakes the one that ran `worker waits` |
| `implementing` | Its implementer works, or fixes what a follow-up names | `worker pr`, `worker stuck`, `worker waits` |
| `review_needed` | A PR commit waits for review | `next` starts a reviewer |
| `reviewing` | A reviewer reads it | `worker review` |
| `approved` | `t3/review` is on the reviewed commit | `next` applies the rule below |
| `asking` | The PR changes files outside its paths | Your `answer` |
| `ci_failed` | CI failed on the approved commit | DevOps triages it |
| `confirmed` | Ready to merge | `worker take` |
| `merging` | DevOps holds it (or `next`, without DevOps) | `worker merge`, `worker failed`, `worker update` |
| `pipeline_wait` | Waits for DevOps's pipeline fix to merge | That fix's merge |
| `blocked` | A question for the user, or its worker ended twice without reporting | `answer`, `restart`, or a drop |
| `merged`, `dropped` | Finished | |

**The confirmation rule**, which `next` applies to each approved task:
- If the PR's head is not the reviewed commit, the task goes back to its implementer.
- If the PR changes files outside the task's paths, the task waits for your answer.
- If a required check failed, the task goes to `ci_failed`.
- Otherwise the task is `confirmed`.

A review asking for changes, a conflict, a failed CI check in the task's code, or your `back` sends
the task back to its implementer, with a follow-up naming the fix.

**A worker that ends without its closing command** gets one follow-up asking it to finish. If it ends
that way again, its task is `blocked`, and `tell` says so.

## Worker commands

```text
<python> <install>/scripts/harness.py worker <command> --worker <id> --token <token> [arguments]
```

| Worker | Command | What it does |
|---|---|---|
| Implementer | `worker pr --pr N` | Checks PR N and this checkout, then runs the project's checks here. A failed check refuses, with the output; on success the task goes to review. |
| Implementer | `worker stuck --why "…"` | Blocks the task with a question for the user |
| Implementer | `worker waits --on "<task>" --why "…"` | Waits for that task's code; the same implementer carries on once it merges |
| Reviewer | `worker review --verdict approve\|changes --sha S` | `approve` posts `t3/review` on S, which must still be the PR's head. `changes` sends the task back to its implementer. |
| DevOps | `worker take` | Gives the oldest confirmed or CI-failed task of its plan, or nothing while another merge runs |
| DevOps | `worker update` | Brings the PR up to date through GitHub. If the task's own change is unchanged, the approval carries to the new head; otherwise the task goes back to its implementer. |
| DevOps | `worker merge` | Squash-merges the approved head once its required checks and `t3/review` are green, then fast-forwards the project folder when it is on the branch. |
| DevOps | `worker failed --cause task\|pipeline --detail "…"` | `task` sends the failure to the implementer. `pipeline` holds the task until DevOps's own fix merges. |
| DevOps | `worker pipeline --pr N` | Registers DevOps's pipeline-fix PR as a task, which a reviewer then reads |

A refused command changes nothing. Its error says what to do.

## The binding

`init` creates a binding, paused; [setup](setup.md#3-bind-it) lists its fields.

Jev picks, per task, an implementer's or reviewer's model left open, even after "back to default", and its effort unless set. With no answer, the defaults apply.

**Problems:** `status` lists any background part (watcher scan, its sends, hooks, Jev, GitHub, a
checkout) that failed three times in a row. A part's next success clears it.
