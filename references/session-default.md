# T3 workflow sessions (opt-in)

This applies only inside T3 Code. A T3 connector in another application is not enough.

The workflow is opt-in (the user's rule, 2026-11-03). It runs only when the user's message says
"use t3 workflow", or when an agent message carries it on in a bound orchestrator thread: a finished
worker's "Delegated task …" notice, or the watcher's message. Any other message gets an ordinary
answer, even in a bound thread. The orchestrator's guard leaves such a turn alone; the fence that
keeps every session of a bound project inside that project applies in every turn. The phrase right after a quote
mark, as in a pasted handoff quoting this rule, only names it.

When it applies, find your place below:

- **You are the bound orchestrator** when your hook adds the "T3 workflow orchestrator" note to your
  context. Follow [orchestrator.md](orchestrator.md) and the [protocol](protocol.md).
- **You are a worker** (implementer, reviewer or DevOps) when your task came from a brief that names
  your role's doc. Follow that brief and doc. Nothing else here applies to you, and you never set up
  or run the workflow yourself.
- **The project has no binding**, and you are a Claude or Codex session: set it up with
  [setup.md](setup.md). Only projects hosted on GitHub use the workflow.
- **Another thread is the project's orchestrator:** the harness refuses you with "Only the bound
  orchestrator runs workflow operations". Point the user to that thread, and set up nothing.
- **You are a goose session:** you cannot be the orchestrator, because its hooks run only in Claude
  Code and Codex. Tell the user to use a Claude or Codex session for it.

In the orchestrator's turns, the workflow is the process. Generic process skills (brainstorming,
plans, subagents) add no question rounds, gates or helpers of their own. Workers may use them inside
their task.

There is no listener, polling loop, timer or schedule: finished workers wake the orchestrator. The
one background process is the installed watcher, which reports quiet workers.
