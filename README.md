# T3 event workflow

Run a GitHub project in [T3 Code](https://github.com/pingdotgg/t3code) through one session. You plan
with the orchestrator and it reports to you. Implementers, reviewers and a DevOps worker run in the
background as T3 delegated tasks. Each task becomes a branch and a pull request. A reviewer approves
the exact commit it read, the orchestrator confirms it, and DevOps merges it through GitHub, where CI
and branch protection have the last word. You can turn DevOps off at setup or later; then the
workflow merges confirmed PRs itself, with no model. When GitHub cannot run CI, as when its billing
stops Actions, you can turn CI off: PRs then merge on their local checks and review, until you turn it
back on.

Website: [mahmoud6901.github.io/t3-event-workflow](https://mahmoud6901.github.io/t3-event-workflow/)
([العربية](https://mahmoud6901.github.io/t3-event-workflow/ar/)).

## Install (Windows, macOS, Linux)

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/) (or pipx), Git, the GitHub CLI (`gh`,
signed in), and T3 Code with at least one of Claude, Codex, goose (for OpenRouter) or Antigravity signed in. The
orchestrator runs on Claude Code (`claude`) or Codex (`codex`), so one of them must be on PATH.

The default models are GPT-6.2 Astra for reviewers at `xhigh` effort and Sonnet 5.5 for DevOps at `high`.
Implementers have no default model: Jev chooses one for each task. You can choose each role's model,
its effort, or both, at setup or later: any model your T3 runs, at any effort T3 offers for it. Setup
checks which models your T3 can run, and asks you to pick one for any role it cannot start.

With a Jev key, Jev chooses the model of each task's implementer where you left it open, seeing each
approved model's [LiveBench 2.0](https://livebench.ai/) scores, from tasks the board does not
publish. It matches the model to the size of the change the task asks for, not to how long its brief
is: a small, contained change goes to the model strongest on Coding, and a large or multi-step one to
the model strongest on Long Agentic. Without Jev's answer (no key, Jev unreachable, or an invalid reply), the implementer runs on
the approved model with the best average score that T3 runs. Jev never sets an effort: an
implementer runs at `xhigh` unless you choose another. Reviewers and DevOps run
on your choice or the default. One exception: while a model is at its usage limit, Jev starts that
role's new workers on an approved model with capacity, and they go back to your model when the limit
resets.

A limit counts when T3 shows it as spent, or when a worker's run stopped at the model's quota, as
in "Individual quota reached… Resets in 4h11m25s". That worker is not asked to finish, since it would
stop the same way. A new one takes over its task at once, on Jev's choice among the models with
capacity, or on the best-scored of them without Jev's answer. When no model the role can run on has
capacity, the task waits for the reset, and the watcher wakes the orchestrator to start it then. A
quota that names no reset is tried again after 30 minutes.

`python scripts/scores.py` refreshes those scores. Jev sees the Coding and Long Agentic suites;
`--suites coding,long_agentic,browser` adds others (`design`, `research`, `data_analysis`,
`reasoning` too), and later refreshes keep the choice. Another board can join LiveBench when its
maintainers run every model on tasks they do not publish and its data can be read without a key: it
gets a reader in `BOARDS` in `scripts/scores.py`, and Jev shows a model it has not scored as unknown.

```bash
uv tool install git+https://github.com/Mahmoud6901/t3-event-workflow
```

```bash
t3-workflow install
```

With pipx instead: `pipx install git+https://github.com/Mahmoud6901/t3-event-workflow`. To upgrade later:
`uv tool upgrade t3-event-workflow`, then `t3-workflow upgrade`, which brings each project's own copy to the
new version (see [Each project apart](#each-project-apart)).

Then:

1. **Jev key (optional):** put the raw TypeSafe key on one line in
   `~/.t3-workflow/secrets/typesafe-api-key.txt`, or set `TYPESAFE_API_KEY`. Without it, an implementer
   whose model you left open runs on the best-scored approved model T3 runs; the rest use the chosen
   or default models.
2. **GitHub's `workflow` scope (optional):** run `gh auth refresh -s workflow` if git on this computer
   signs in through `gh` (`gh auth setup-git`). Without that scope, GitHub refuses pushes that change
   files in `.github/workflows/`, such as DevOps's pipeline fixes. With Git Credential Manager, pushes
   already work, and the scope only matters when DevOps updates a branch where it and main changed the
   same workflow file.
3. **Codex (optional):** to run the orchestrator on a Codex model, run `codex`, type `/hooks` and trust
   the three `t3-workflow hook` hooks. Codex skips a hook it has not trusted, and asks again after an
   install changes one.
4. **Quit and reopen T3 Code.** If T3 was running during the install, run `t3-workflow install`
   again while it is closed, so goose gets the OpenRouter models T3 does not list.
5. **In a T3 project on GitHub,** open a Claude or Codex session and say "use t3 workflow: set up this
   project". It becomes the orchestrator, and the binding starts paused until you activate it. You
   can move that thread between Claude and Codex models in T3 at any time.

`t3-workflow status` shows what is installed, each project's own copy and whether it is at this
version, and any background part that keeps failing; it exits 1 if one does. `t3-workflow uninstall` removes it all again. Every file it edits is backed up to
`~/.t3-workflow/state/backups/` first.

## What the installer changes

| File | Change |
|---|---|
| `~/.claude/settings.json` | Hooks that keep each project's sessions inside it, guard the orchestrator and record its workers' wakes |
| `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` | A marked block pointing to `references/session-default.md` |
| `~/.codex/hooks.json` | The same hooks, for an orchestrator on a Codex model |
| `~/.t3/userdata/settings.json` | Goose `customModels` for OpenRouter models T3 does not list |
| Windows Startup folder | `t3-workflow-watch.cmd`, which starts the watcher at logon |
| `~/Library/LaunchAgents/com.t3-workflow.watch.plist` (macOS) | The same, as a LaunchAgent |
| `~/.config/systemd/user/t3-workflow-watch.service` (Linux) | The same, as a systemd user unit, enabled with `systemctl --user enable` |

The hooks record a worker's end from T3's notice of it. If they miss one, as they did once when T3 reworded
its notice, the watcher records it 10 minutes after the notice and wakes the orchestrator once.

The workflow has only been tried on Windows so far. The macOS and Linux parts are covered by tests, not
yet by a real login; reports are welcome.

Everything else stays in `~/.t3-workflow/`: `state/` (logs, health, backups, and each project's own
copy in `state/homes/`) and `secrets/` belong to each computer. A clone that already has its own `state/`
folder keeps using it instead.

## Each project apart

Each bound project runs its own copy of the workflow, with its own data: `state/homes/<project>/` holds
the code it runs and its own `state/` (its binding, logs and watcher). Setup makes the copy. Nothing one
project's sessions run or change reaches another project's copy or data.

The hooks also keep every T3 session of a bound project, the orchestrator and its workers, inside that
project, in every turn:

- they change files only in the project's folder, its git worktrees and the temporary folder;
- they never read another bound project's folders, the workflow's data or T3's own database, which holds
  every project's conversations;
- they read only the workflow's published code, and never change it.

Shell commands are judged by the paths they name. That stops mistakes and plain attempts, not a
determined program. Goose and Antigravity workers run no hooks, and every session still runs as you: only separate user
accounts would be a hard wall.

After `uv tool upgrade t3-event-workflow`, run `t3-workflow upgrade`. It moves each binding an older
version kept in the shared data into its own copy, and brings every copy to the new version. `t3-workflow status` names a copy that
is older. Workers that started before keep working, and each project's watcher restarts on the new code.

## Alerts on your phone

The workflow sends no notifications of its own. T3 Code's mobile app can alert you when the
orchestrator's thread finishes a turn, fails, needs an approval or asks for input. It needs T3 Connect
on the desktop (Settings → Sharing & access) and Device Notifications in the mobile app. A connection
over your local network alone gives no alerts. See T3's
[mobile notifications](https://github.com/pingdotgg/t3code/blob/main/docs/user/mobile-notifications.md)
and [remote access](https://github.com/pingdotgg/t3code/blob/main/docs/user/remote-access.md) docs.

## Read next

- [SKILL.md](SKILL.md): the workflow on one page
- [references/setup.md](references/setup.md): setting up a project
- [references/protocol.md](references/protocol.md): every operation and worker command

## License

MIT, see [LICENSE](LICENSE).
