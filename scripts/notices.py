"""T3's notice that a worker's run ended: the message T3 adds to the orchestrator's thread, "Delegated task <task>
(<label>) … [completed]:" and then how the run ended.

Only the line's two ends are read, the task and the status: T3 has added words between them before (2026-11-23,
"in child thread <thread>"). The message also lists its tasks in delegatedCompletion.taskIds, which still names them
when T3 words the line differently again. Standard library only: the hooks import it on every call.
"""
import re

LINE = re.compile(r"^Delegated task (\S+)[^\n]* \[([A-Za-z_]+)\]:", re.MULTILINE)


def read(text, tasks=()):
    """{T3 task: (status, the notice's text)} for each notice in `text`. Each of `tasks` (the message's
    delegatedCompletion) whose line did not read counts as completed, with the whole text."""
    text = text if isinstance(text, str) else ""
    found = {}
    lines = list(LINE.finditer(text))
    for line, after in zip(lines, [*lines[1:], None]):
        found[line.group(1)] = (line.group(2).lower(), text[line.end():after.start() if after else len(text)].strip())
    for task in tasks:
        if isinstance(task, str) and task not in found:
            found[task] = ("completed", text.strip())
    return found


def of(message):
    """The notices one of T3's messages (its payload) carries."""
    completion = message.get("delegatedCompletion")
    return read(message.get("text"), (completion.get("taskIds") or []) if isinstance(completion, dict) else ())
