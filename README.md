# T3 event workflow

Run a GitHub project in [T3 Code](https://github.com/pingdotgg/t3code) through one session. You plan
with the orchestrator and it reports to you. Implementers, reviewers and a DevOps worker run in the
background as T3 delegated tasks. Each task becomes a branch and a pull request. A reviewer approves
the exact commit it read, the orchestrator confirms it, and DevOps merges it through GitHub, where CI
and branch protection have the last word. You can turn DevOps off at setup or later; then the
workflow merges confirmed PRs itself, with no model.

## Install (Windows, macOS, Linux)

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/) (or pipx), Git, the GitHub CLI (`gh`,
signed in), and T3 Code with at least one of Claude, Codex or goose (for OpenRouter) signed in. The
orchestrator runs on Claude Code (`claude`) or Codex (`codex`), so one of them must be on PATH.

The default models are GPT-6.1-Sol (Codex) for implementers, Sonnet 5.5 for reviewers and Opus 5.5 for
DevOps. Setup checks which models your T3 can run, and asks you to pick one for any role whose default
it cannot run.

```bash
uv tool install git+https://github.com/Mahmoud6901/t3-event-workflow
```

```bash
t3-workflow install
```

With pipx instead: `pipx install git+https://github.com/Mahmoud6901/t3-event-workflow`. To upgrade later:
`uv tool upgrade t3-event-workflow`.

Then:

1. **Jev key (optional):** put the raw TypeSafe key on one line in
   `~/.t3-workflow/secrets/typesafe-api-key.txt`, or set `TYPESAFE_API_KEY`. Without it, every task
   uses the chosen or default models.
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

`t3-workflow status` shows what is installed, and any background part that keeps failing; it exits 1
if one does. `t3-workflow uninstall` removes it all again. Every file it edits is backed up to
`~/.t3-workflow/state/backups/` first.

## What the installer changes

| File | Change |
|---|---|
| `~/.claude/settings.json` | Hooks that guard the orchestrator and record its workers' wakes |
| `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` | A marked block pointing to `references/session-default.md` |
| `~/.codex/hooks.json` | The same hooks, for an orchestrator on a Codex model |
| `~/.t3/userdata/settings.json` | Goose `customModels` for OpenRouter models T3 does not list |
| Windows Startup folder | `t3-workflow-watch.cmd`, which starts the watcher at logon |
| `~/Library/LaunchAgents/com.t3-workflow.watch.plist` (macOS) | The same, as a LaunchAgent |
| `~/.config/systemd/user/t3-workflow-watch.service` (Linux) | The same, as a systemd user unit, enabled with `systemctl --user enable` |

The workflow has only been tried on Windows so far. The macOS and Linux parts are covered by tests, not
yet by a real login; reports are welcome.

Everything else stays in `~/.t3-workflow/`: `state/` (bindings, logs, health, backups) and `secrets/`
belong to each computer. A clone that already has its own `state/` folder keeps using it instead.

## Read next

- [SKILL.md](SKILL.md): the workflow on one page
- [references/setup.md](references/setup.md): setting up a project
- [references/protocol.md](references/protocol.md): every operation and worker command

## License

MIT, see [LICENSE](LICENSE).
