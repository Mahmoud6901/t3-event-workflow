"""The user's opt-in (2026-11-03): a T3 session follows the workflow only when the user's message says "use t3 workflow".

The phrase right after an opening quote mark only names it, as a pasted handoff quoting this rule does (2026-11-11).
Agent messages (workflow events, the watcher's alerts, T3's notices) carry an already started workflow on. Standard
library only: the hooks import it on every call.
"""
from itertools import chain
import json
import re

RESUME = "Continue where you left off."
PHRASE = re.compile(r"(?<![\"'`“‘«])\buse\s+(?:the\s+)?t3[\s-]+workflow\b", re.IGNORECASE)


def asked(text):
    return isinstance(text, str) and bool(PHRASE.search(text))


def resumed(message):
    """T3's own message when a usage limit resets. It says createdBy "user", yet nobody wrote it. The text must match too:
    the same source also carries real words of the user's ("yes"), and the user may type the same sentence (source web).
    If T3 rewords it, a resume reads as the user's message again: refused, as before this existed."""
    return (message.get("createdBy") == "user" and message.get("creationSource") == "server"
            and (message.get("text") or "").strip() == RESUME)


def plain(messages, older=()):
    """A turn of only the user's own messages, none asking for the workflow: an ordinary turn. T3's resume is neither: it
    carries on the turn before it, so `older` gives the earlier turns' messages, newest first, read only for a resume."""
    if not messages:
        return False
    for turn in chain([messages], older):
        own = [m for m in turn if not resumed(m)]
        if own:
            return all(m.get("createdBy") == "user" for m in own) and not any(asked(m.get("text")) for m in own)
    return True  # Nothing to carry on.


def older_turns(db, thread_id, run_id):
    """The user-role messages of the session's turns before this one, newest first, read as they are asked for."""
    runs = [(json.loads(p).get("ordinal") or 0, r) for r, p in db.execute(
        "SELECT run_id, payload_json FROM orchestration_v2_projection_runs WHERE thread_id=?", (thread_id,))]
    current = next((ordinal for ordinal, r in runs if r == run_id), 0)
    for _, run in sorted((turn for turn in runs if turn[0] < current), reverse=True):
        yield [json.loads(p) for (p,) in db.execute(
            "SELECT payload_json FROM orchestration_v2_projection_messages WHERE run_id=? AND thread_id=? AND role='user'", (run, thread_id))]
