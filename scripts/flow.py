"""The delegated PR workflow: plans, tasks, workers and every transition between them. Standard library only.

references/protocol.md lists every operation. The orchestrator runs `next` on every wake and
makes the T3 calls it returns; workers report through `harness.py worker <command>`. GitHub goes through github.py,
and every write is one transaction on the harness database (store.py).
"""
import copy
from datetime import datetime, timedelta, timezone
import hmac
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import zoneinfo

import github
import health
import jev
import places
import record
import store
import watcher

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
DEFAULT_DB = places.STATE / "harness.sqlite3"
GOOSE = "acpRegistry_goose"
T3_CACHES = places.T3_HOME / "caches"  # T3's list of what each provider can run, one file per provider.
ANTIGRAVITY = "antigravity"  # Google's agent; T3 also lists Claude models under it, so it comes last here.
PROVIDERS = {"claudeAgent": ("Claude", "effort"), "codex": ("Codex", "reasoningEffort"), GOOSE: ("goose", "thinking_effort"),
             ANTIGRAVITY: ("Antigravity", "thought_level")}
MATCHES = 20  # Models one search shows.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROLES = ("implementer", "reviewer", "devops")
# The approved models and their roles. Only one with a card in assets/cards.json is approved: Jev reads it. A model T3
# runs through more than one provider lists its routes in the order to try them, each with the model's id there
# (its own by default); `provider` is its first. One card and one family serve every route. A goose route is paid
# per token through OpenRouter; the rest are subscriptions.
MODELS = {
    "claude-opus-5-5": {"provider": "claudeAgent", "roles": ["implementer", "reviewer", "devops"], "routes": [
        {"provider": "claudeAgent"}, {"provider": ANTIGRAVITY}, {"provider": GOOSE, "model": "anthropic/claude-opus-5.5"}]},
    "claude-sonnet-5-5": {"provider": "claudeAgent", "roles": ["implementer", "reviewer", "devops"], "routes": [
        {"provider": "claudeAgent"}, {"provider": ANTIGRAVITY}]},
    "gpt-6.2-astra": {"provider": "codex", "roles": ["implementer", "reviewer"], "routes": [
        {"provider": "codex"}, {"provider": GOOSE, "model": "openai/gpt-6.2-astra"}]},
    "gpt-6.2-sol": {"provider": "codex", "roles": ["implementer"]},
    "gemini-4-neon": {"provider": ANTIGRAVITY, "roles": ["implementer", "reviewer", "devops"], "routes": [
        {"provider": ANTIGRAVITY}, {"provider": GOOSE, "model": "google/gemini-4-neon"}]},
    "gemini-4-argon": {"provider": ANTIGRAVITY, "roles": ["implementer"]},
    "gemini-4-hydrogen": {"provider": ANTIGRAVITY, "roles": ["implementer"]},
    "claude-fable-5-2": {"provider": "claudeAgent", "roles": []},  # Kept for a later lead role.
    "google/gemini-4-carbon": {"provider": GOOSE, "roles": ["implementer", "reviewer"]},
    "deepseek/deepseek-v4.1-flash": {"provider": GOOSE, "roles": ["implementer"]},
    "z-ai/glm-5.3-prime": {"provider": GOOSE, "roles": ["implementer"]},
    "moonshotai/kimi-k3.1-pro": {"provider": GOOSE, "roles": ["implementer"]},
    "qwen/qwen4-max": {"provider": GOOSE, "roles": ["implementer"]},
}
DEFAULTS = {"implementerEffort": "xhigh", "reviewer": "gpt-6.2-astra", "reviewerEffort": "xhigh",
            "devops": "claude-sonnet-5-5", "devopsEffort": "high"}  # No implementer model: Jev chooses it, else fallback().
SHIPPED = {"reviewer": "claude-opus-5-5"}  # Defaults an earlier version shipped, which a binding it made still stores.
QUIET_MINUTES = 30
NUDGES = 1  # Follow-ups asking a worker that ended without its closing command to finish; then the task is blocked.
REVIEW_ROUNDS = 3  # Reviews asking for changes before the user decides how the task goes on.
ROUND_OPTIONS = ["carry on", "restart", "drop"]
OPEN = ("waiting", "ready", "implementing", "review_needed", "reviewing", "approved", "asking", "confirmed",
        "merging", "ci_failed", "pipeline_wait", "blocked")


class Refused(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Refused(message)


def text(value, name):
    require(isinstance(value, str) and value.strip(), f"{name} must be non-empty text")
    return value.strip()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slash(path):
    return str(path).replace("\\", "/")


def command(*words):
    """A shell command that runs the same in bash, PowerShell and cmd: absolute paths with forward slashes."""
    return " ".join([slash(sys.executable), slash(SCRIPTS / "harness.py"), *words])


def git(cwd, *args, check=True):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    require(not check or done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr.strip()[:400]}")
    return done.stdout.strip() if check else (done.returncode, done.stdout.strip(), done.stderr.strip())


def selection(config, model_id, effort, item=None):
    """The T3 target for a model at an effort: each provider names its effort option its own way. A goose model gets
    `thinking_effort` only when it declares one (its entry says "thinking": true), and that option has no xhigh. A
    worker's entry names its route: the provider, and the model's id there when it differs."""
    item = item or config["models"][model_id]
    options = {"claudeAgent": {"effort": effort},
               GOOSE: {"provider": "openrouter"},
               ANTIGRAVITY: {"thought_level": sent(ANTIGRAVITY, effort)}}.get(
        item["provider"], {"reasoningEffort": effort, "serviceTier": "default"})
    if item["provider"] == GOOSE and item.get("thinking"):
        options["thinking_effort"] = sent(GOOSE, effort)
    return {"providerInstanceId": item["provider"], "model": item.get("model") or model_id, "options": options}


def routes(item, model):
    """Where a model runs, in the order to try: each {"provider", "model": its id there, "paid": through goose}."""
    return [{"provider": r["provider"], "model": r.get("model") or model, "paid": r["provider"] == GOOSE}
            for r in item.get("routes") or [{"provider": item.get("provider")}]]


def options(config, role, lists, spent):
    """What a new worker in the role may start on now: {model: the route it starts on}, for each approved model of the
    role that has one, in the order config lists them. A model starts on its first route that T3 runs and that has
    capacity. A paid route counts only while no subscription route of the role's approved models has capacity."""
    free = {m: [r for r in routes(item, m) if runs(lists, r["model"], r["provider"]) is not False
                and not at_limit(spent or {}, r["model"], r["provider"])]
            for m, item in config["models"].items() if role in item.get("roles", [])}
    paid = not any(not r["paid"] for found in free.values() for r in found)
    return {m: next(r for r in found if paid or not r["paid"]) for m, found in free.items()
            if any(paid or not r["paid"] for r in found)}


def can_run(config, model, lists):
    """Whether T3 runs some route of the approved model; None when T3's lists are not here."""
    found = [runs(lists, r["model"], r["provider"]) for r in routes(config["models"].get(model) or {}, model)]
    return None if lists is None else any(found)


FALLBACK = "claude-opus-5-5"  # The implementer's model when Jev gives no answer, while it can start.


def fallback(found):
    """The implementer's model without Jev's answer, among the options found: (FALLBACK, "fallback") while it is one,
    else (the best scored of them, "scores"); (None, None) for none."""
    if FALLBACK in found:
        return FALLBACK, "fallback"
    best = next(iter(jev.ranked(list(found))), None)
    return best, "scores" if best else None


def fit(effort, offered):
    """The effort a route takes: the one asked for when it offers it, else the nearest it offers at or below it (or its
    lowest). Unchanged when its offer is not known."""
    if not offered or effort in offered or effort not in EFFORTS:
        return effort
    below = [e for e in EFFORTS[:EFFORTS.index(effort) + 1] if e in offered]
    return below[-1] if below else next((e for e in EFFORTS if e in offered), effort)


def placed(config, model, route, effort, lists):
    """A worker's model on a route: its entry for T3, its effort as that route takes it, the route's provider when it is
    not the model's first, and whether it is paid."""
    offered = ((lists or {}).get(route["provider"]) or {}).get(route["model"]) or []
    first = routes(config["models"].get(model) or {}, model)[0]
    return {"model": model, "effort": fit(effort, offered),
            "entry": {"provider": route["provider"], "model": route["model"], "thinking": bool(offered)},
            "via": None if (route["provider"], route["model"]) == (first["provider"], first["model"])
            else PROVIDERS[route["provider"]][0], "paid": route["paid"]}


def sent(provider, effort):
    """The effort as the provider takes it: goose's thinking_effort has no xhigh, so xhigh goes as high, and
    Antigravity's thought_level has no max, so max goes as xhigh."""
    return {(GOOSE, "xhigh"): "high", (ANTIGRAVITY, "max"): "xhigh"}.get((provider, effort), effort)


def approved():
    """The approved models, {model: {"provider", "roles"}}: those in MODELS that have a card. Every binding works with
    this list, whatever list an earlier version stored in it."""
    cards = jev.cards()
    return {m: copy.deepcopy(item) for m, item in MODELS.items() if m in cards}


def validate_config(raw):
    config = copy.deepcopy(raw)
    text(config.get("projectId"), "projectId")
    text(config.get("orchestrator"), "orchestrator thread")
    root = Path(text(config.get("workspace"), "workspace"))
    require(root.is_absolute() and root.is_dir(), "workspace must be an existing absolute folder")
    config["workspace"] = str(root.resolve())
    config["branch"] = text(config.get("branch") or "main", "branch")
    hub = config.get("github") or {}
    require(isinstance(hub.get("repo"), str) and re.fullmatch(r"[\w.-]+/[\w.-]+", hub["repo"]),
            "github.repo must be owner/name")
    require(isinstance(hub.get("requiredChecks"), list) and all(isinstance(c, str) and c for c in hub["requiredChecks"]),
            "github.requiredChecks must list the CI check names")
    config.pop("models", None)  # The approved models are the shipped ones with a card (approved()), never a binding's.
    config["defaults"] = {key: value for key, value in {**DEFAULTS, **(config.get("defaults") or {})}.items() if key in DEFAULTS}
    models = approved()
    for role in ROLES:
        if role in config["defaults"]:
            require(role in (models.get(config["defaults"][role]) or {}).get("roles", []),
                    f"defaults.{role} must be an approved {role} model")
        require(config["defaults"][role + "Effort"] in EFFORTS, f"defaults.{role}Effort must be one of {', '.join(EFFORTS)}")
    config.setdefault("checks", [])
    require(isinstance(config["checks"], list), "checks must be a list")
    names = set()
    for check in config["checks"]:
        name = text(check.get("name"), "check name")
        require(name not in names, "Check names must be unique")
        names.add(name)
        require(isinstance(check.get("argv"), list) and check["argv"] and all(isinstance(a, str) for a in check["argv"]),
                "A check needs an argv list, not shell text")
        check.setdefault("cwd", ".")
        require(not Path(check["cwd"]).is_absolute() and ".." not in Path(check["cwd"]).parts,
                "A check's cwd is a folder inside the project, relative to its root")
        check.setdefault("timeoutSeconds", 900)
        require(type(check["timeoutSeconds"]) is int and 1 <= check["timeoutSeconds"] <= 7200, "Invalid check timeout")
    require(config["checks"] or config.get("noChecksReason"), "Name the local checks, or give noChecksReason")
    config.setdefault("jev", {})
    config["jev"].setdefault("model", "jev-1.13.0")
    config["jev"].setdefault("keyEnv", "TYPESAFE_API_KEY")
    config["jev"].setdefault("keyFile", jev.DEFAULT_KEY_FILE)
    config.setdefault("quietMinutes", QUIET_MINUTES)
    require(type(config["quietMinutes"]) is int and config["quietMinutes"] >= 1, "quietMinutes must be a whole number of minutes")
    return config


def guarded_paths(raw):
    """The guarded paths the user gave, in a plan's `owns` form: a PR that changes a file under one waits for the user's
    answer before it merges. [] for none."""
    require(isinstance(raw, list) and all(isinstance(p, str) and p.strip() for p in raw),
            "guarded lists paths in the project, like a plan's owns; [] for none")
    paths = [slash(p.strip()) for p in raw]
    require(all(not p.startswith("/") and not re.match(r"[A-Za-z]:", p) and ".." not in p.split("/") for p in paths),
            "A guarded path is inside the project, relative to its root")
    return list(dict.fromkeys(paths))


def listed(files, most=10):
    return ", ".join(files[:most]) + (" and more" if len(files) > most else "")


def offered_efforts(model, option):
    """The effort levels T3 offers for one model, from its effort option; [] when it has none."""
    for descriptor in (model.get("capabilities") or {}).get("optionDescriptors") or []:
        if isinstance(descriptor, dict) and descriptor.get("id") == option:
            return [o["id"] for o in descriptor.get("options") or []
                    if isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"] != "off"]
    return []


def t3_lists():
    """What this computer's T3 can run: ({provider: {model: the effort levels it offers}} for each ready provider,
    {provider: why not} for the others). (None, {}) when T3's lists are not here, so nothing can be checked."""
    if not Path(T3_CACHES).is_dir():
        return None, {}
    lists, why = {}, {}
    for provider, (name, option) in PROVIDERS.items():
        try:
            cache = json.loads((Path(T3_CACHES) / f"{provider}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            why[provider] = f"T3 has no {name} list, so {name} is not installed there"
            continue
        if not (cache.get("enabled") and cache.get("installed") and cache.get("status") == "ready"):
            why[provider] = f"{name} is not enabled and ready in T3"
            continue
        if (cache.get("auth") or {}).get("status") == "unauthenticated":
            why[provider] = f"{name} is not signed in, in T3"
            continue
        models = {m["slug"]: offered_efforts(m, option)
                  for m in cache.get("models") or [] if isinstance(m, dict) and isinstance(m.get("slug"), str)}
        if models:
            lists[provider] = models
        else:
            why[provider] = f"T3's {name} list is empty"
    return lists, why


def runs(lists, model, provider):
    """Whether T3 can run the model; None when T3's lists are not here."""
    return None if lists is None else model in lists.get(provider, {})


def t3_limits():
    """The usage limits T3 shows as spent: {provider: the model families covered, "" standing for every model}. A window
    counts while it is full and its reset is still ahead. T3 names a family's own window after the general one
    (seven_day_fable beside seven_day), and that one covers only the models named so."""
    spent = {}
    for provider in PROVIDERS:
        try:
            cache = json.loads((Path(T3_CACHES) / f"{provider}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        usage = cache.get("usageLimits") if isinstance(cache, dict) else None
        windows = [w for w in (usage.get("windows") if isinstance(usage, dict) else None) or [] if isinstance(w, dict)]
        ids = {window.get("id") for window in windows}
        for window in windows:
            used = window.get("usedPercent")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or used < 100 or passed(window.get("resetsAt")):
                continue
            base, _, family = str(window.get("id") or "").rpartition("_")
            spent.setdefault(provider, set()).add(family if base in ids else "")
    return spent


def passed(moment):
    """Whether a time T3 wrote is behind us. One that cannot be read is not: a full window with no clear reset counts."""
    try:
        at = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc)


def at_limit(spent, model, provider):
    """Whether the model's provider has a spent usage limit that covers it."""
    return any(family in model for family in spent.get(provider, ()))


# How a run that stopped at its model's quota ends (T3's wake notices, 2026-11): Antigravity's "Individual quota reached
# ... Resets in 4h11m25s", Claude's "rate limit reached ... resets 1:50am (Europe/Berlin)", a provider's "add more credits".
QUOTA = re.compile(r"quota (?:reached|exceeded)|exceeded (?:your|the) quota|out of quota|(?:usage|rate) limit (?:reached|exceeded)"
                   r"|(?:hit|reached) (?:your|the) [\w -]{0,20}limit|(?:add|buy|purchase) more credits|(?:out of|insufficient) credits",
                   re.IGNORECASE)
RESETS_IN = re.compile(r"(?:resets?|try again)\s+in\s+([^.;\n]+)", re.IGNORECASE)
SPAN = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", re.IGNORECASE)
RESETS_AT = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\s*(?:\(([\w/+-]+)\))?", re.IGNORECASE)
QUOTA_WAIT = timedelta(minutes=30)  # A quota that names no reset is tried again after this.


def quota_until(text, at=None):
    """When the quota a failed run stopped at resets (ISO, UTC), read from the end T3's wake notice gives; None when the
    run did not stop at a quota. A clock time is read in the zone the message names, else this computer's."""
    if not QUOTA.search(text or ""):
        return None
    at = at or datetime.now(timezone.utc)
    found = RESETS_IN.search(text)
    units = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
    wait = sum((timedelta(**{units[unit[0].lower()]: int(n)}) for n, unit in SPAN.findall(found.group(1) if found else "")),
               timedelta())
    clock = None if wait else RESETS_AT.search(text)
    if clock and (clock.group(2) or clock.group(3)):
        hour, minute, meridiem, zone = clock.groups()
        try:
            local = at.astimezone(zoneinfo.ZoneInfo(zone) if zone else None)
        except (KeyError, ValueError):  # A zone this computer cannot read (Windows without tzdata).
            local = at.astimezone()
        try:
            moment = local.replace(hour=int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0) if meridiem else int(hour),
                                   minute=int(minute or 0), second=0, microsecond=0)
            wait = (moment if moment > local else moment + timedelta(days=1)).astimezone(timezone.utc) - at
        except ValueError:
            wait = None
    return (at + (wait or QUOTA_WAIT)).isoformat(timespec="seconds")


def learn(config, state, worker, until):
    """A run of this worker stopped at its model's quota: that model counts as at its limit on that route (its provider)
    until `until`. Its other routes keep their own quotas."""
    entry = worker.get("entry") or config["models"].get(worker["model"]) or {}
    provider, model = entry.get("provider"), entry.get("model") or worker["model"]
    limits = {key: limit for key, limit in (state.get("limits") or {}).items() if not passed(limit["until"])}
    state["limits"] = {**limits, f"{model}@{provider}": {"model": model, "provider": provider, "until": until}}


def limits_now(state):
    """The usage limits that keep a model out now: T3's, and each quota a worker's run stopped at, until it resets."""
    spent = t3_limits()
    for key, limit in (state.get("limits") or {}).items():  # An earlier version keyed a limit by its model alone.
        if not passed(limit["until"]):
            spent.setdefault(limit["provider"], set()).add(limit.get("model", key))
    return spent


def out_until(config, state, role, spent, lists):
    """When no model a new worker in the role could run on has capacity, and a quota a run stopped at is what keeps
    them out: the earliest of those resets, which its work waits for. None while one has capacity, or when only T3's
    limits keep them out (that worker starts as before)."""
    model, provider = usual(config, state, role)
    able = {(r["model"], r["provider"]) for m, item in config["models"].items() if role in item.get("roles", [])
            for r in routes(item, m) if runs(lists, r["model"], r["provider"]) is not False}
    if model:
        able.add((model, provider))
    if not able or any(not at_limit(spent, m, p) for m, p in able):
        return None
    resets = [limit["until"] for key, limit in (state.get("limits") or {}).items()
              if (limit.get("model", key), limit["provider"]) in able and not passed(limit["until"])]
    return min(resets) if resets else None


def usual(config, state, role):
    """The model a role runs on without advice, and its provider: the user's pick, else the default. (None, None) for an
    implementer left to Jev."""
    pick = (state.get("picks") or {}).get(role) or {}
    if pick.get("model"):
        return pick["model"], pick.get("provider", GOOSE)
    model = config["defaults"].get(role)
    return model, (config["models"].get(model) or {}).get("provider")


def described(task):
    """What Jev is told about a task."""
    return {"request": task["task"], "brief": task["brief"], "plan": task["plan"]}


def devops_on(state, name):
    """A plan keeps the DevOps mode it started with; one not started yet follows the binding's current mode."""
    plan = state["plans"][name]
    return plan.get("devops", True) if plan["started"] else state.get("devopsOn", True)


def ci_checks(config, state):
    """The CI checks a merge waits for: none while the user has CI off."""
    return config["github"]["requiredChecks"] if state.get("ciOn", True) else []


FAMILIES = (("claude-", "Anthropic"), ("anthropic/", "Anthropic"), ("gpt-", "OpenAI"), ("openai/", "OpenAI"),
            ("gemini-", "Google"), ("google/", "Google"))


def family(model):
    """The model's maker, whichever provider runs it: a known prefix's, else the part of a maker/model id before its
    slash, else the id itself."""
    name = str(model or "").lower()
    return next((maker for prefix, maker in FAMILIES if name.startswith(prefix)), name.split("/", 1)[0])


def pr_size(pr, before=None):
    """The size bucket of a PR as GitHub gives it (record.size): its changed files and its added plus deleted lines.
    `before` stands when this reading has neither."""
    files = pr.get("changedFiles")
    files = len(pr["files"]) if files is None and isinstance(pr.get("files"), list) else files
    lines = pr["additions"] + pr["deletions"] if all(isinstance(pr.get(k), int) for k in ("additions", "deletions")) else None
    found = record.size(files, lines)
    return before if found == "unknown" and before else found


def tally(state, task):
    """The record's rows for a task that merged or was dropped: (model, role, size, counts), filed under its PR's size
    as last seen ("unknown" without a PR). The worker that held the task at the end (its implementer; for a pipeline
    fix, its DevOps worker) counts the task, whether its first review approved it, its review rounds and whether it was
    ever blocked. Each reviewer model counts the task and the changes it asked for. Each worker replaced for being
    quiet, or stopped at a quota, counts that."""
    rows, end = {}, {"merged": int(task["state"] == "merged"), "dropped": int(task["state"] == "dropped")}

    def count(model, role, **found):
        if model and role:
            row = rows.setdefault((model, role), dict.fromkeys(record.COUNTS, 0))
            for key, value in found.items():
                row[key] += value
    reviews = task.get("reviews") or []
    first = int(bool(reviews) and reviews[0]["verdict"] == "approve")
    author = state["workers"].get(task.get("implementer")) or {}
    count(author.get("model"), author.get("role"), tasks=1, **end, first_approved=first,
          rounds=sum(r["verdict"] == "changes" for r in reviews),
          blocked=int(any(h["to"] == "blocked" for h in task["history"])))
    for model in dict.fromkeys(r["model"] for r in reviews if r.get("model")):
        count(model, "reviewer", tasks=1, **end, first_approved=int(first and reviews[0].get("model") == model),
              rounds=sum(r["verdict"] == "changes" and r.get("model") == model for r in reviews))
    for stop in task.get("stops") or []:
        count(stop["model"], stop["role"], **{stop["why"]: 1})
    return [(model, role, task.get("size") or "unknown", found) for (model, role), found in rows.items()]


def stopped(task, worker, why):
    """For the record: this worker left the task for being quiet ("quiet") or at its quota ("quota")."""
    if task is not None:
        task.setdefault("stops", []).append({"model": worker["model"], "role": worker["role"], "why": why})


def uncount(item, role):
    """The role's quiet replacement on this work (a task, or a plan's DevOps entry) no longer counts."""
    if item and role in (item.get("quietReplaced") or []):
        item["quietReplaced"] = [r for r in item["quietReplaced"] if r != role]


def review_fix(task):
    """What the implementer is told when the reviewer asked for changes."""
    return {"kind": "review", "detail": f"The reviewer asked for changes on PR #{task['pr']}."}


def update_fix(config, outcome, number):
    """What the implementer is told when its approved PR cannot merge as it was reviewed."""
    return {
        "moved": {"kind": "moved", "detail": f"PR #{number} moved after its approval."},
        "updated": {"kind": "updated", "detail": (
            f"PR #{number} was updated with {config['branch']}, and its change now differs from the reviewed one. "
            f"Pull the update into this checkout (git pull), check the task still works, and push any fix.")},
        "conflict": {"kind": "conflict", "detail": f"PR #{number} conflicts with {config['branch']}."},
    }[outcome]


def role_model(config, state, role):
    """What a new worker in this role starts on: the user's model and effort where set, else the defaults. An
    implementer's model the user left open is Jev's, chosen per task ("jev"); Jev also stands in for any role's model
    that is at its usage limit. Jev sets no effort."""
    pick = (state.get("picks") or {}).get(role) or {}
    model = pick.get("model") or config["defaults"].get(role)
    return {"model": model, "modelSource": "pick" if pick.get("model") else "default" if model else "jev",
            "effort": pick.get("effort") or config["defaults"][role + "Effort"],
            "effortSource": "pick" if pick.get("effort") else "default"}


CLEAR = (None, "", "default")


def settle_pick(role, current, raw, defaults, lists):
    """A role's setting after the user's choice. Its model (one T3 runs) and its effort are separate: a choice names one
    or both and keeps the other, and "default" clears one. {} when neither is set."""
    require(role in ROLES, f"picks are for {', '.join(ROLES)}")
    raw = raw if isinstance(raw, dict) else {"model": raw}
    require("model" in raw or "effort" in raw, f"picks.{role} names a model, an effort or both")
    pick = dict(current or {})
    if "model" in raw:
        for key in ("model", "provider", "thinking", "efforts"):
            pick.pop(key, None)
        if raw["model"] not in CLEAR:
            model = text(raw["model"], f"picks.{role}.model")
            require(lists is not None, "T3's model lists are not on this computer, so no model can be picked")
            provider = next((p for p in PROVIDERS if model in lists.get(p, {})), None)
            require(provider, f"T3 cannot run {model}; search what it can with the models operation")
            efforts = list(lists[provider][model])
            pick.update(model=model, provider=provider, thinking=bool(efforts), efforts=efforts)
    if "effort" in raw:
        pick.pop("effort", None)
        if raw["effort"] not in CLEAR:
            pick["effort"] = text(raw["effort"], f"picks.{role}.effort")
    offered, effort = pick.get("efforts") or [], pick.get("effort")
    if offered and effort:
        require(effort in offered, f"{pick['model']} takes the effort {', '.join(offered)}, not {effort}")
    elif offered:
        default = defaults[role + "Effort"]
        require(sent(pick["provider"], default) in offered, f"{pick['model']} does not take the {role}'s default effort, "
                f"{default}; choose one of {', '.join(offered)}")
    elif effort and not pick.get("model"):
        require(effort in EFFORTS, f"Without a model, the effort is one of {', '.join(EFFORTS)}; {effort} is a level only "
                f"some models have, so pick the model with it")
    return pick


def default_jev(config, context, role="implementer"):
    """Jev's valid model for one role of one task, {} when its answer was invalid; add_worker fills in what it leaves
    out. Raises when Jev gives no answer, so the caller records the problem and the worker starts without advice."""
    decision = jev.evaluate(config, context, "task", role=role)
    if decision["status"] != "decided":
        raise RuntimeError(f"Jev gave no advice: {decision['reason']}")
    return dict(decision["selectedModels"])


def new_state():
    return {"version": 0, "control": "paused", "plans": {}, "tasks": {}, "workers": {}, "devops": {},
            "calls": {}, "asks": {}, "tell": [], "seq": 0}


def next_id(state, prefix):
    state["seq"] += 1
    return f"{prefix}{state['seq']}"


def loops(graph):
    """Whether following graph's edges (node -> nodes) from some node leads back to it."""
    done, path = set(), set()

    def visit(node):
        if node in path:
            return True
        if node in done:
            return False
        path.add(node)
        found = any(visit(n) for n in graph.get(node, ()))
        path.discard(node)
        done.add(node)
        return found
    return any(visit(node) for node in list(graph))


def waits_forever(state):
    """Plans that wait for each other, or waiting tasks that do, through `after` or their plan's `dependsOn`."""
    unfinished = {t["id"] for t in state["tasks"].values() if t["state"] not in {"merged", "dropped"}}
    tasks = {t["id"]: (set(t["after"]) | {o["id"] for o in state["tasks"].values()
                                          if o["plan"] in state["plans"][t["plan"]]["dependsOn"]}) & unfinished
             for t in state["tasks"].values() if t["state"] == "waiting"}
    return loops({name: set(p["dependsOn"]) for name, p in state["plans"].items()}) or loops(tasks)


class Flow:
    def __init__(self, db_path=DEFAULT_DB, hub=None, jev=None, record_path=None):
        self.path = Path(db_path).resolve()
        self.hub_for = hub or github.GitHub
        self.jev = jev or default_jev
        self.db = store.connect(self.path)
        # The shared record serves the database in this copy's own state folder; any other database (a test's) keeps
        # its record beside it.
        self.record = Path(record_path or (record.FILE if self.path.parent == Path(places.STATE).resolve()
                                           else self.path.parent / "record.sqlite3"))
        self.finished = []  # Tasks the current transaction merged or dropped, for the record.

    def close(self):
        self.db.close()

    # Storage

    def load(self, project):
        row = self.db.execute("SELECT config, state FROM projects WHERE id=?", (project,)).fetchone()
        require(row, f"Project {project} has no binding")
        config = json.loads(row[0])
        # Every binding works with the approved models of this version, whatever list an earlier one stored: a model
        # approved since joins, and one without a card leaves, a default on it giving way to the current one. An
        # implementer default an older version stored goes: Jev chooses that model. Jev weighs only the models T3 runs
        # at the time. A default an earlier version shipped moves to the current one once T3 runs it; a model the user
        # picked stays theirs.
        config["models"] = approved()
        config["defaults"] = {key: DEFAULTS[key] if key in ROLES and key not in (config["models"].get(value) or {}).get(
            "roles", []) else value for key, value in config["defaults"].items() if key in DEFAULTS}
        for role, old in SHIPPED.items():
            new = DEFAULTS[role]
            if config["defaults"].get(role) == old and runs(t3_lists()[0], new, config["models"][new]["provider"]) is not False:
                config["defaults"][role] = new
        return config, json.loads(row[1])

    def transact(self, project, change, op):
        """change(config, state) runs inside one write transaction; the state it leaves is saved with the next version.
        Once it is saved, each task it merged or dropped adds its counts to the record."""
        self.db.execute("BEGIN IMMEDIATE")
        self.finished = []
        try:
            config, state = self.load(project)
            result = change(config, state)
            rows = [row for task_id in dict.fromkeys(self.finished) for row in tally(state, state["tasks"][task_id])]
            state["version"] += 1
            self.db.execute("UPDATE projects SET state=? WHERE id=?", (store.packed(state), project))
            self.db.execute("INSERT INTO audit(project,operation,version,detail) VALUES (?,?,?,?)",
                            (project, op, state["version"], store.packed(result if isinstance(result, dict) else {})))
            self.db.commit()
            self.keep(rows)
            return result
        finally:
            self.finished = []
            if self.db.in_transaction:
                self.db.rollback()

    def keep(self, rows):
        """Add to the record. A failure never fails the operation: health reports it."""
        if not rows:
            return
        try:
            record.add(self.record, rows)
            health.ok(self.path.parent, "record")
        except Exception as error:  # noqa: BLE001 - the record is a side note to the work
            health.failed(self.path.parent, "record", f"{type(error).__name__}: {error}")

    def records(self, role):
        """{model: its record line for the role}; {} when the record cannot be read, which health reports."""
        try:
            found = record.lines(self.record, role)
            health.ok(self.path.parent, "record")
            return found
        except Exception as error:  # noqa: BLE001 - Jev then reads "no record yet"
            health.failed(self.path.parent, "record", f"{type(error).__name__}: {error}")
            return {}

    # The orchestrator

    def orchestrator(self, req):
        op = text(req.get("op"), "op")
        project = text(req.get("projectId"), "projectId")
        thread = text(req.get("threadId"), "threadId")
        if op == "init":
            return self.init(project, thread, req)
        if op == "models":  # Setup asks before there is a binding.
            return self.models(req)
        config, state = self.load(project)
        require(thread == config["orchestrator"], "Only the bound orchestrator runs workflow operations")
        handler = {"control": self.control, "queue_set": self.queue_set, "next": self.next, "done": self.done,
                   "status": self.status, "answer": self.answer, "ended": self.ended, "restart": self.restart,
                   "queue_edit": self.queue_edit, "setup_github": self.setup_github, "pick": self.pick,
                   "devops": self.devops, "ci": self.ci, "guarded": self.guarded}.get(op)
        require(handler, f"Unknown operation {op}")
        return handler(project, req)

    def init(self, project, thread, req):
        config = validate_config({"projectId": project, "orchestrator": thread, "workspace": req.get("workspace"),
                                  "branch": req.get("branch"), "github": {"repo": req.get("repo"),
                                                                          "requiredChecks": req.get("requiredChecks")},
                                  **{k: req[k] for k in ("checks", "noChecksReason", "defaults", "quietMinutes")
                                     if req.get(k) is not None}})
        require(not self.path.is_relative_to(Path(config["workspace"])), "The harness database must live outside the project")
        picks = req.get("picks") or {}
        require(isinstance(picks, dict), "picks maps a role to its model")
        devops = req.get("devops", True)
        require(isinstance(devops, bool), "devops is true (a DevOps worker merges) or false (the harness merges)")
        guarded = guarded_paths(req.get("guarded") or [])
        lists, why = t3_lists()
        state = new_state()
        state["guarded"] = guarded
        state["picks"] = {role: pick for role, raw in picks.items()
                          if (pick := settle_pick(role, {}, raw, config["defaults"], lists))}
        state["devopsOn"] = devops
        if lists is not None:  # Every role must start on something this T3 runs, and Jev advises only among those.
            models = {**config, "models": approved()}
            for role in ROLES:
                if state["picks"].get(role, {}).get("model"):
                    continue
                model = config["defaults"].get(role)
                if model:
                    provider = models["models"][model]["provider"]
                    require(can_run(models, model, lists),
                            f"T3 cannot run {model}, the {role}'s default ({why.get(provider) or 'T3 does not list it'}). "
                            f"Pick a model for the {role}; the models operation lists what T3 runs.")
                else:
                    names = ", ".join(m for m, item in models["models"].items() if role in item.get("roles", []))
                    require(options(models, role, lists, {}), f"T3 runs none of the approved {role}s ({names}). "
                            f"Pick a model for the {role}; the models operation lists what T3 runs.")
        with self.db:
            exists = self.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone()
            require(not exists, f"Project {project} is already bound")
            self.db.execute("INSERT INTO projects VALUES (?,?,?)", (project, store.packed(config), store.packed(state)))
        return {"projectId": project, "control": "paused", "repo": config["github"]["repo"]}

    def models(self, req):
        """For setup and model changes: which providers T3 can run, whether it runs each role's default (for the
        implementer, which Jev chooses, the fallback when Jev gives no answer), and the models whose names hold every
        word searched for."""
        lists, why = t3_lists()
        defaults = {}
        for role in ROLES:
            model, models = DEFAULTS.get(role), {"models": approved()}
            starts = model or fallback(options(models, role, lists, {}))[0]
            defaults[role] = {"model": model, **({} if model else {"fallback": starts}), "effort": DEFAULTS[role + "Effort"],
                              "runs": can_run(models, starts, lists) if starts else False}
        if lists is None:
            return {"known": False, "defaults": defaults,
                    "reason": "T3's model lists are not on this computer, so nothing can be checked or picked."}
        words = str(req.get("search") or "").lower().split()
        hits = [{"model": m, "provider": PROVIDERS[p][0], "efforts": lists[p][m]}
                for p in PROVIDERS for m in sorted(lists.get(p, {})) if all(w in m.lower() for w in words)]
        return {"known": True, "providers": {name: why.get(p, "ready") for p, (name, _) in PROVIDERS.items()},
                "defaults": defaults, "models": hits[:MATCHES], "more": max(0, len(hits) - MATCHES)}

    def pick(self, project, req):
        """The user's later change to a role's model, its effort or both; "default" (or no value) puts one back.
        Workers already started keep what they run on."""
        role = req.get("role")
        require(role in ROLES, f"role is one of {', '.join(ROLES)}")
        text(req.get("authorization"), "authorization (the user's own words)")
        raw = {key: req[key] for key in ("model", "effort") if key in req}
        require(raw, "Name a model, an effort or both")
        lists, _ = t3_lists()

        def change(config, state):
            picks = state.setdefault("picks", {})
            if "model" in raw and raw["model"] in CLEAR:
                model = config["defaults"].get(role)
                if model:
                    require(can_run(config, model, lists) is not False,
                            f"T3 cannot run {model}, the {role}'s default; pick another model")
                else:
                    require(options(config, role, lists, {}), f"T3 runs none of the approved {role}s, so Jev has none to "
                            f"choose; pick another model")
            pick = settle_pick(role, picks.get(role), raw, config["defaults"], lists)
            if pick:
                picks[role] = pick
            else:
                picks.pop(role, None)
            return {"role": role, **role_model(config, state, role)}
        return self.transact(project, change, "pick")

    def devops(self, project, req):
        """The user turns DevOps on or off for plans that start from now on; running plans keep the mode they started with."""
        on = req.get("on")
        require(isinstance(on, bool), "on is true or false")
        text(req.get("authorization"), "authorization (the user's own words)")

        def change(config, state):
            state["devopsOn"] = on
            keep = sorted(name for name, plan in state["plans"].items()
                          if plan["started"] and plan["status"] != "done" and plan.get("devops", True) != on)
            return {"devops": on, "keep": keep}
        return self.transact(project, change, "devops")

    def guarded(self, project, req):
        """The user's new list of guarded paths, replacing the old one; [] guards none. It applies to every approval from
        now on, in running plans too."""
        paths = guarded_paths(req.get("paths"))
        text(req.get("authorization"), "authorization (the user's own words)")

        def change(config, state):
            state["guarded"] = paths
            return {"guarded": paths}
        return self.transact(project, change, "guarded")

    def ci(self, project, req):
        """The user turns GitHub's CI off, as when GitHub billing stops Actions, or back on. Off, a PR merges on its local
        checks and review, main's protection stops requiring the CI checks, and tasks held for CI go on to merge."""
        on = req.get("on")
        require(isinstance(on, bool), "on is true or false")
        text(req.get("authorization"), "authorization (the user's own words)")
        config, state = self.load(project)
        gh = self.hub_for(config["github"]["repo"])
        removed = []
        if on:  # What CI off removed, as it was, and the binding's own checks, which an older CI off may have taken.
            back = list(state.get("ciRemoved") or [])
            back += [{"context": n} for n in config["github"]["requiredChecks"] if n not in {c["context"] for c in back}]
            changed = gh.restore_ci_checks(config["branch"], back)
        else:
            changed, removed = gh.drop_ci_checks(config["branch"])

        def change(config, state):
            state["ciOn"] = on
            if on:
                state.pop("ciRemoved", None)
            else:
                saved = state.get("ciRemoved") or []
                state["ciRemoved"] = saved + [c for c in removed if c["context"] not in {s["context"] for s in saved}]
            freed = [] if on else [t for t in state["tasks"].values() if t["state"] in {"ci_failed", "pipeline_wait"}]
            for task in freed:
                for key in ("ciFailed", "pipeline", "waitingOn"):
                    task.pop(key, None)
                self.move(task, "confirmed", "CI is off")
            return {"ci": on, "changed": changed, "freed": [t["id"] for t in freed]}
        return self.transact(project, change, "ci")

    def control(self, project, req):
        action = req.get("action")
        require(action in {"activate", "pause", "resume", "stop"}, "action is activate, pause, resume or stop")
        if action in {"activate", "resume", "stop"}:
            text(req.get("authorization"), "authorization (the user's own words)")

        def change(config, state):
            state["control"] = {"activate": "active", "resume": "active", "pause": "paused", "stop": "stopped"}[action]
            return {"control": state["control"]}
        return self.transact(project, change, "control")

    def queue_set(self, project, req):
        plans = req.get("plans")
        require(isinstance(plans, list) and plans, "plans lists at least one plan")

        def change(config, state):
            require(not any(t["state"] in OPEN for t in state["tasks"].values()), "A queue is running; add to it with queue_edit")
            for plan in plans:
                self.add_plan(state, plan)
            return {"plans": [p["plan"] for p in plans]}
        return self.transact(project, change, "queue_set")

    def add_plan(self, state, plan):
        name = text(plan.get("plan"), "plan")
        require(name not in state["plans"], f"Plan {name} is already queued")
        owns = plan.get("owns")
        require(isinstance(owns, list) and owns and all(isinstance(o, str) and o for o in owns),
                f"Plan {name}: owns lists the paths its tasks may change")
        depends = plan.get("dependsOn") or []
        require(all(d in state["plans"] for d in depends), f"Plan {name} depends on a plan that is not queued")
        state["plans"][name] = {"plan": name, "owns": owns, "dependsOn": depends, "status": "open", "started": False}
        self.add_tasks(state, name, plan.get("tasks"))

    def add_tasks(self, state, name, tasks):
        """Tasks join plan name as waiting; `after` names tasks among these or already queued."""
        require(isinstance(tasks, list) and tasks, f"Plan {name} needs tasks")
        names = {}
        for item in tasks:
            task_name = text(item.get("task"), "task")
            after = [names[a] if a in names else self.task_id(state, a) for a in item.get("after") or []]
            task_id = next_id(state, "t")
            names[task_name] = task_id
            state["tasks"][task_id] = {"id": task_id, "plan": name, "task": task_name, "brief": item.get("brief") or "",
                                       "owns": item.get("owns") or state["plans"][name]["owns"], "after": after,
                                       "kind": "task", "state": "waiting", "rev": 0, "history": []}
        return list(names.values())

    def queue_edit(self, project, req):
        """Change a running queue: new plans, new tasks for a plan (a done plan opens again), drop a task, or relink:
        replace a waiting task's `after` or a plan's `dependsOn`, unless that makes a loop."""
        def change(config, state):
            result = {}
            for plan in req.get("plans") or []:
                self.add_plan(state, plan)
                result.setdefault("plans", []).append(plan["plan"])
            if req.get("tasks"):
                name = text(req.get("plan"), "plan")
                require(name in state["plans"], f"No plan {name} is queued")
                result["tasks"] = self.add_tasks(state, name, req["tasks"])
                state["plans"][name]["status"] = "open"
            if req.get("drop"):
                task = state["tasks"][self.task_id(state, text(req["drop"], "drop"))]
                require(task["state"] in OPEN, f"{task['id']} is already {task['state']}")
                require(task["state"] != "merging" or task.get("merger") == "harness",
                        f"DevOps is merging {task['id']}; drop it after, or restart DevOps")
                self.drop(state, task, "you dropped it")
                result["dropped"] = task["id"]
            if req.get("task"):
                task = state["tasks"][self.task_id(state, text(req["task"], "task"))]
                require(task["state"] == "waiting", f"{task['id']} already started ({task['state']}); only a waiting "
                                                    "task's links can change")
                after = req.get("after")
                require(isinstance(after, list), "after lists the tasks it waits for; [] for none")
                new = [self.task_id(state, a) for a in after]
                self.unlink(state, task, [a for a in task["after"] if a not in new], req)
                task["after"] = new
                result.setdefault("relinked", []).append({"task": task["id"], "after": task["after"]})
            if "dependsOn" in req:
                name = text(req.get("plan"), "plan")
                require(name in state["plans"], f"No plan {name} is queued")
                depends = req["dependsOn"]
                require(isinstance(depends, list) and all(d in state["plans"] for d in depends),
                        f"Plan {name}: dependsOn lists queued plans; [] for none")
                gone = [p for p in state["plans"][name]["dependsOn"]
                        if p not in depends and state["plans"][p]["status"] != "done"]
                if gone:
                    why = text(req.get("why"), f"why (what of plan {', '.join(gone)} its tasks do not need)")
                    for task in state["tasks"].values():
                        if task["plan"] == name and task["state"] == "waiting":
                            task.setdefault("unlinked", []).extend({"plan": p, "why": why, "at": now()} for p in gone)
                            self.move(task, "waiting", f"its plan no longer waits for {', '.join(gone)}: {why}")
                state["plans"][name]["dependsOn"] = depends
                result.setdefault("relinked", []).append({"plan": name, "dependsOn": depends})
            require(not (result.get("relinked") and waits_forever(state)),
                    "That link makes a loop: the tasks in it would wait for each other forever")
            require(result, "queue_edit takes plans, tasks with plan, drop, task with after, or plan with dependsOn")
            return result
        return self.transact(project, change, "queue_edit")

    def unlink(self, state, task, removed, req):
        """Links queue_edit takes off a waiting task. One to unfinished work needs `why`, kept for the task's implementer,
        its reviewer and status. One its implementer put back with worker waits stays unless the user asks: then their
        own words come as `authorization`."""
        unfinished = [a for a in removed if state["tasks"][a]["state"] not in {"merged", "dropped"}]
        if not unfinished:
            return
        names = ", ".join(state["tasks"][a]["task"] for a in unfinished)
        why = text(req.get("why"), f"why (what of {names} it does not need)")
        needed = {n["task"]: n for n in task.get("needed") or []}
        words = req.get("authorization")
        for a in unfinished:
            if a in needed:
                require(isinstance(words, str) and words.strip(), (
                    f"Its implementer found it needs {needed[a]['name']} ({needed[a]['why']}), so the link stays and "
                    "the task waits. Only if the user themselves asks to start it now, pass their words as authorization."))
            task.setdefault("unlinked", []).append({"task": a, "name": state["tasks"][a]["task"], "why": why, "at": now(),
                                                    **({"authorization": words.strip()} if a in needed else {})})
        self.move(task, "waiting", f"no longer waits for {', '.join(unfinished)}: {why}")

    def setup_github(self, project, req):
        """Squash-only merges and main's protection on the bound repo, on your word. Reports what it changed."""
        text(req.get("authorization"), "authorization (the user's own words)")
        config, state = self.load(project)
        changed = self.hub_for(config["github"]["repo"]).setup(config["branch"], ci_checks(config, state))
        with self.db:
            self.db.execute("INSERT INTO audit(project,operation,version,detail) VALUES (?,?,?,?)",
                            (project, "setup_github", None, store.packed({"changed": changed})))
        return {"repo": config["github"]["repo"], "changed": changed}

    def task_id(self, state, name):
        found = [t["id"] for t in state["tasks"].values() if name in {t["id"], t["task"]}]
        require(len(found) == 1, f"No single task is named {name}")
        return found[0]

    def next(self, project, req):
        self.harness_merges(project)
        config, state = self.load(project)
        ready = {t["id"] for t in state["tasks"].values()
                 if t["state"] == "waiting" and self.unblocked(state, t) and not self.parked(state, t)}
        back, choices, facts, waits = set(), {}, {}, {}
        for worker in state["workers"].values():  # Quota-stopped and quiet workers whose work goes back to start anew.
            if (worker.get("ended") or {}).get("quota") and not worker["ended"].get("seen") and worker["state"] != "cancelled":
                task = self.owing(state, worker)
                if task or worker["role"] == "devops":
                    back.add((worker["role"], task and task["id"]))
            elif self.quiet_due(worker) and self.quiet_act(state, worker, self.owing(state, worker)) == "replace":
                task = self.owing(state, worker)
                back.add((worker["role"], task and task["id"]))
        ready |= {task_id for role, task_id in back if role == "implementer"}
        if state["control"] == "active":
            gh = self.hub_for(config["github"]["repo"])
            for task in state["tasks"].values():
                if task["state"] == "approved":
                    try:
                        pr = gh.pull(task["pr"])
                        facts[task["id"]] = (task["rev"], pr, gh.checks(pr["head"], ci_checks(config, state)))
                        health.ok(self.path.parent, "github")
                    except github.Failed as error:  # The rule waits for the next wake.
                        health.failed(self.path.parent, "github", error)
            reviews = sorted({t["id"] for t in state["tasks"].values() if t["state"] == "review_needed"}
                             | {task_id for role, task_id in back if role == "reviewer"})
            merges = any(role == "devops" for role, _ in back) or any(
                devops_on(state, name) and state["devops"].get(name, {}).get("state", "none") == "none"
                and any(t["plan"] == name and t["state"] in {"approved", "confirmed", "ci_failed"}
                        for t in state["tasks"].values()) for name in state["plans"])
            if ready or reviews or merges:
                lists, spent = t3_lists()[0], limits_now(state)
                waits = {role: out_until(config, state, role, spent, lists) for role in ROLES}
                for role, task_ids in (("implementer", sorted(ready)), ("reviewer", reviews)):
                    for task_id in task_ids if not waits[role] else ():
                        choices[role, task_id] = self.advise(config, state, role, described(state["tasks"][task_id]),
                                                             lists, spent)
                if merges and not waits["devops"]:  # One DevOps serves a whole plan, so its stand-in is not a task's.
                    choices["devops"] = self.advise(config, state, "devops", {
                        "request": "Merge the plan's confirmed pull requests through GitHub and triage CI failures"},
                        lists, spent)

        def change(config, state):
            def held(item, role, label):
                """No model the role could run on has capacity until a quota resets: the work waits, told once."""
                until = waits.get(role)
                if until and item.get("held") != until:
                    state["tell"].append(f"{label} waits until {until}: every model its {role} could run on is at its "
                                         "usage limit. It starts by itself then.")
                if until:
                    item["held"] = until
                else:
                    item.pop("held", None)
                return bool(until)

            for worker in state["workers"].values():
                if worker.get("ended") and not worker["ended"].get("seen"):
                    worker["ended"]["seen"] = True
                    self.settle(state, worker, waits)
            for worker in list(state["workers"].values()):
                if self.quiet_due(worker):
                    worker["quiet"]["acted"] = True
                    task = self.owing(state, worker)
                    act = self.quiet_act(state, worker, task)
                    if act:
                        self.quiet_over(state, worker, task, act)
            if state["control"] == "active":
                for task in state["tasks"].values():
                    label = f"{task['task']} ({task['plan']})"
                    if task["state"] == "waiting" and self.unblocked(state, task) and self.parked(state, task):
                        self.resume(config, state, task)
                    elif task["state"] == "waiting" and self.unblocked(state, task):
                        if not held(task, "implementer", label):
                            self.start_implementer(config, state, task, choices.get(("implementer", task["id"])))
                    elif task["state"] == "review_needed":
                        if not held(task, "reviewer", label):
                            self.start_reviewer(config, state, task, choices.get(("reviewer", task["id"])))
                    elif task["state"] == "approved" and task["id"] in facts and facts[task["id"]][0] == task["rev"]:
                        self.confirm(state, task, *facts[task["id"]][1:])
                    elif task["state"] == "implementing" and task.get("fix"):
                        self.send_fix(config, state, task)
                self.call_devops(config, state, choices.get("devops"), lambda entry, name: held(entry, "devops", f"Plan {name}"))
            summary = self.stop_summary(state)
            calls = list(state["calls"].values())
            told, state["tell"] = state["tell"] + ([summary] if summary else []), []
            asks = [a for a in state["asks"].values() if not a.get("asked")]
            for ask in asks:
                ask["asked"] = True
            return {"calls": calls, "tell": told, "ask": asks}
        return self.transact(project, change, "next")

    def done(self, project, req):
        """The orchestrator made these calls: each start's T3 task and child thread, each follow-up's new T3 task."""
        results = req.get("results")
        require(isinstance(results, list) and results, "results lists each call made, with what T3 returned")

        def change(config, state):
            recorded, unknown, told = [], [], []
            for item in results:
                call = state["calls"].pop(item.get("call"), None) if isinstance(item, dict) else None
                if not call:
                    unknown.append(item.get("call") if isinstance(item, dict) else item)
                    continue
                worker = state["workers"][call["worker"]]
                if call["tool"] == "delegate_task":
                    worker["childThreadId"] = text(item.get("childThreadId"), "childThreadId")
                    worker["state"] = "working"
                    line = started_line(state, worker)
                    if line not in told:
                        told.append(line)
                if call["tool"] in {"delegate_task", "t3_thread_send"} and item.get("taskId"):
                    worker["taskIds"].append(item["taskId"])
                    worker["state"] = "working" if worker["state"] != "cancelled" else "cancelled"
                    early = [e for e in state.get("unmatched", []) if e["taskId"] == item["taskId"]]
                    worker["ended"] = {**early[0], "seen": False} if early else None
                    if early and early[0].get("quota"):
                        learn(config, state, worker, early[0]["quota"])
                    state["unmatched"] = [e for e in state.get("unmatched", []) if e["taskId"] != item["taskId"]]
                if call["tool"] == "task_cancel":
                    worker["state"] = "cancelled"
                recorded.append(call["call"])
            summary = self.stop_summary(state)
            told += [summary] if summary else []
            return {"recorded": recorded, "tell": told, **({"unknown": unknown} if unknown else {})}
        return self.transact(project, change, "done")

    def ended(self, project, req):
        """A worker's T3 run ended (the hook reads it from the "Delegated task …" wake, with the notice's text). `next`
        acts on it. An end that arrives before `done` recorded its run waits for it. A failed run that stopped at its
        model's quota is learned: that model counts as at its limit until the quota resets. A late end is one the
        watcher found that the hook missed; late_wake wakes the orchestrator for it."""
        run, status = text(req.get("taskId"), "taskId"), req.get("status") or "completed"
        until = quota_until(req.get("text")) if status != "completed" and isinstance(req.get("text"), str) else None
        quota = {"quota": until} if until else {}
        quota = {**quota, "late": True} if req.get("late") is True else quota

        def change(config, state):
            for worker in state["workers"].values():
                if run in worker["taskIds"]:
                    if until:
                        learn(config, state, worker, until)
                    # An older run's end means nothing now, and an end already recorded is not news.
                    new = worker["taskIds"][-1] == run and (worker.get("ended") or {}).get("taskId") != run
                    if new:
                        worker["ended"] = {"taskId": run, "status": status, "at": now(), "seen": False, **quota}
                    return {"worker": worker["id"], "new": new}
            new = not any(e["taskId"] == run for e in state.setdefault("unmatched", []))
            if new:
                state["unmatched"].append({"taskId": run, "status": status, "at": now(), **quota})
            return {"worker": None, "new": new}
        return self.transact(project, change, "ended")

    def restart(self, project, req):
        """On your word: the task's current worker is cancelled and a new one in the same role takes over."""
        text(req.get("authorization"), "authorization (the user's own words)")
        name = text(req.get("task"), "task")

        def role_of(task):
            where = task.get("blockedFrom") if task["state"] == "blocked" else task["state"]
            return {"implementing": "implementer", "reviewing": "reviewer", "merging": "devops"}.get(where)

        config, state = self.load(project)
        task = state["tasks"][self.task_id(state, name)]
        kept = task.get("choice") or {}  # Jev's advice at the task's first start.
        advice, spent = {}, limits_now(state)
        # With a limit spent the new worker may need a stand-in; a stand-in, or a choice a limit narrowed, is advised afresh.
        stale = kept.get("limit") or kept.get("narrowed")
        if (spent or stale) and role_of(task) in ("implementer", "reviewer"):
            advice = self.advise(config, state, role_of(task), described(task), t3_lists()[0], spent)
        kept = {} if stale else kept

        def change(config, state):
            task = state["tasks"][self.task_id(state, name)]
            role = role_of(task)
            require(role, f"{task['id']} is {task['state']}; no worker of it can be restarted")
            old = state["workers"].get(task.get("blockedWorker") or task.get(role))
            if old:
                self.retire(state, old, "restarted on the user's word")
            uncount(task, role)  # The user's restart starts the count of quiet replacements again.
            for key in ("blockedFrom", "blockedWorker", "fix"):
                task.pop(key, None)
            for ask in state["asks"].values():
                if ask["task"] == task["id"] and not ask.get("answered"):
                    ask["answered"] = "restarted"
            if role == "implementer":
                self.start_implementer(config, state, task, advice or kept)
            elif role == "reviewer":
                self.start_reviewer(config, state, task, advice)
            else:
                self.devops_back(state, task["plan"], task, "DevOps restarted")
            return {"task": task["id"], "state": task["state"]}
        return self.transact(project, change, "restart")

    def retire(self, state, worker, reason, let_finish=False):
        """The worker is done for: its unmade calls are withdrawn and a run still going is cancelled. With let_finish,
        a run that already reported is left to end on its own."""
        for call_id, call in list(state["calls"].items()):
            if call["worker"] == worker["id"]:
                del state["calls"][call_id]
        if worker["state"] == "working" and not (let_finish and worker["job"].get("reported")):
            self.cancel(state, worker, reason)
        worker["state"] = "cancelled"

    def reported(self, state, worker, task=None):
        """The worker ran its closing command: its job is reported, and a quiet replacement in its role no longer counts
        for its work."""
        me = state["workers"][worker["id"]]
        me["job"]["reported"] = True
        uncount(task, me["role"])
        if me["role"] == "devops":
            uncount(state["devops"].get(me["plan"]), "devops")

    def owing(self, state, worker):
        """The task this worker still owes its closing command for, or None."""
        if worker["role"] == "devops":
            return next((t for t in state["tasks"].values() if t["state"] == "merging" and t.get("devops") == worker["id"]),
                        None)
        task = state["tasks"].get(worker["task"])
        owes = {"implementer": "implementing", "reviewer": "reviewing"}[worker["role"]]
        return task if task and task.get(worker["role"]) == worker["id"] and task["state"] == owes and not task.get("fix") \
            else None

    def settle(self, state, worker, waits=None):
        """A worker's run ended. One that owes nothing rests (an idle DevOps waits for a follow-up). One that owes its
        closing command is asked once to finish; the next time, its task is blocked and you are told. One whose run
        stopped at its model's quota is not asked: a follow-up would stop the same way, so its work goes back."""
        if worker["state"] == "cancelled":
            return
        worker["state"] = "idle"
        task = self.owing(state, worker)
        if worker["ended"].get("quota") and (task or worker["role"] == "devops"):
            self.put_back(state, worker, task, (waits or {}).get(worker["role"]))
            return
        if task is None:
            entry = (state["devops"].get(worker["plan"]) or {}) if worker["role"] == "devops" else {}
            if entry.get("worker") == worker["id"]:
                entry["state"] = "idle"
            return
        job = worker["job"]
        if job.get("nudged", 0) < NUDGES:
            job["nudged"] = job.get("nudged", 0) + 1
            self.send(state, worker, "nudge", "\n".join([
                "Your run ended before you reported. Finish the job, then run: " + closing(worker, task),
                "Then end your turn."]))
            return
        task["blockedFrom"], task["blockedWorker"] = task["state"], worker["id"]
        task.pop("devops", None)
        self.move(task, "blocked", f"its {worker['role']} ended twice without reporting")
        state["tell"].append(f"{task['task']} ({task['plan']}) is blocked: its {worker['role']} ended twice without "
                             f"reporting. Say restart to give it a new {worker['role']}, or drop it.")

    def put_back(self, state, worker, task, wait):
        """The worker's run stopped at its model's quota: it goes, and its work goes back to where `next` starts a new
        worker in its role, on a model with capacity (Jev advises among them); with none, after `wait`."""
        until, role = worker["ended"]["quota"], worker["role"]
        self.retire(state, worker, f"{worker['model']} is at its usage limit")
        stopped(task, worker, "quota")
        why = f"its {role} stopped at {worker['model']}'s usage limit"
        if role == "devops":
            self.devops_back(state, worker["plan"], task, why)
        else:
            self.move(task, "waiting" if role == "implementer" else "review_needed", why)
        label = f"{task['task']} ({task['plan']})" if task else f"Plan {worker['plan']}"
        then = "" if wait else f" A new {'DevOps worker' if role == 'devops' else role} starts on a model with capacity."
        state["tell"].append(f"{label}: {why}, which resets at {until}.{then}")

    def devops_back(self, state, plan, task, why):
        """The plan's DevOps worker is gone: a new one starts, and the task it held goes back to wait for it."""
        state["devops"][plan] = {"worker": None, "state": "none"}
        if task:
            task.pop("devops", None)
            self.move(task, "ci_failed" if task.get("job") == "triage" else "confirmed", why)

    def answer(self, project, req):
        """Your answer to a question from `ask`: merge, back or drop for a PR outside its paths; carry on, restart or
        drop for a task after its review rounds; any words for a stuck implementer, which get sent to it."""
        ask_id, reply = text(req.get("ask"), "ask"), text(req.get("answer"), "answer")
        config, state = self.load(project)
        asked = state["asks"].get(ask_id) or {}
        advice = {}
        if asked.get("kind") == "rounds" and reply == "restart" and not asked.get("answered"):
            advice = self.other_model(config, state, state["tasks"][asked["task"]])

        def change(config, state):
            ask = state["asks"].get(ask_id)
            require(ask and not ask.get("answered"), f"No open question {ask_id}")
            task = state["tasks"][ask["task"]]
            if ask["kind"] in {"outside", "rounds"}:
                require(reply in ask["options"], f"Answer {ask_id} with {', '.join(ask['options'])}")
                require(task["state"] == "asking", f"{task['id']} is no longer waiting for this answer")
            if ask["kind"] == "rounds":
                task["rounds"] = 0  # The count starts again from the user's answer.
                if reply == "carry on":
                    task["fix"] = review_fix(task)
                    self.move(task, "implementing", "you said carry on")
                elif reply == "restart":
                    self.take_over(config, state, task, advice)
                else:
                    self.drop(state, task, "you dropped it")
            elif ask["kind"] == "outside":
                if reply == "merge":
                    self.move(task, "confirmed", "you said merge")
                elif reply == "back":
                    outside, guarded = ask.get("outside") or [], ask.get("guarded") or []
                    task["fix"] = {"kind": "note", "detail": " ".join([
                        *([f"PR #{task['pr']} may change only {', '.join(task['owns'])}. "
                           f"Take out the changes to {', '.join(outside)}."] if outside else []),
                        *([f"PR #{task['pr']} changes guarded files, which the user sent back: {', '.join(guarded)}. "
                           f"Take out the changes to them."] if guarded else [])])}
                    self.move(task, "implementing", "you sent it back")
                else:
                    self.drop(state, task, "you dropped it")
            else:
                require(task["state"] == "blocked", f"{task['id']} is no longer waiting for this answer")
                task.pop("blockedFrom", None)
                task["fix"] = {"kind": "note", "detail": f"The answer to your question: {reply}"}
                self.move(task, "implementing", "you answered")
            ask["answered"] = reply
            return {"task": task["id"], "state": task["state"]}
        return self.transact(project, change, "answer")

    def ask_rounds(self, state, task):
        """The reviewer asked for changes REVIEW_ROUNDS times since the user last decided: the task waits for them."""
        ask_id = next_id(state, "a")
        state["asks"][ask_id] = {"id": ask_id, "task": task["id"], "kind": "rounds", "question": (
            f"The reviewer asked for changes on PR #{task['pr']} ({task['task']}) {REVIEW_ROUNDS} times. Carry on with "
            f"its implementer for {REVIEW_ROUNDS} more rounds, restart it with a new implementer, or drop it?"),
            "options": list(ROUND_OPTIONS)}
        self.move(task, "asking", f"{REVIEW_ROUNDS} review rounds asked for changes")
        state["tell"].append(f"{task['task']} ({task['plan']}) stopped after {REVIEW_ROUNDS} review rounds: each asked "
                             f"for changes.")

    def take_over(self, config, state, task, advice):
        """A new implementer takes the task after its review rounds, told why its PR came back. The author of a
        pipeline fix is its plan's DevOps worker, which stays."""
        old = state["workers"].get(task.get("implementer"))
        if old and old["role"] == "implementer":
            self.retire(state, old, "replaced after its review rounds")
        task.pop("fix", None)
        task["returned"] = (f"The reviewer asked for changes on PR #{task['pr']} {REVIEW_ROUNDS} times, and the user gave "
                            f"the task to a new implementer.")
        self.start_implementer(config, state, task, advice)

    def other_model(self, config, state, task):
        """Advice for the new implementer that takes a task after its review rounds. Where the user left the model
        open, it runs on another model than the one that held the task: Jev's choice among the other options, else the
        fallback, or the best scored of them when the fallback held it. With the user's model, or no other to run on, the
        usual advice."""
        lists, spent = t3_lists()[0], limits_now(state)
        last = (state["workers"].get(task.get("implementer")) or {}).get("model")
        others = {m: route for m, route in options(config, "implementer", lists, spent).items() if m != last}
        if ((state.get("picks") or {}).get("implementer") or {}).get("model") or not others:
            return self.advise(config, state, "implementer", described(task), lists, spent)
        choice = self.ask_jev(config, state, "implementer", described(task), others)
        if not choice:
            model, source = fallback(others)
            choice = {"implementer": model, "source": source}
        return choice

    def drop(self, state, task, note):
        """The task ends unmerged: its unmade calls are withdrawn, its workers still on it are cancelled."""
        for worker in state["workers"].values():
            if worker["task"] == task["id"] and worker["state"] != "cancelled":
                self.retire(state, worker, f"{task['task']} was dropped", let_finish=True)
        task.pop("devops", None)
        self.move(task, "dropped", note)
        kept = f"; PR #{task['pr']} stays open on GitHub" if task.get("pr") else ""
        state["tell"].append(f"Dropped {task['task']} ({task['plan']}){kept}.")
        self.finish_plan(state, task["plan"])

    def cancel(self, state, worker, reason):
        if not worker["taskIds"]:
            return
        call_id = f"{worker['id']}-cancel-{len(worker['taskIds'])}"
        state["calls"][call_id] = {"call": call_id, "worker": worker["id"], "tool": "task_cancel", "arguments": {
            "taskId": worker["taskIds"][-1], "reason": reason, "clientRequestId": call_id}}

    # The watcher (watch.py)

    def projects(self):
        return [(p, json.loads(c), json.loads(s)) for p, c, s in self.db.execute("SELECT id, config, state FROM projects")]

    def quiet(self, project, worker_id, since, minutes):
        """The watcher saw this working worker quiet since `since`, its latest activity in T3. While the binding is
        active, a worker that owes its work is dealt with by the next `next` (quiet_over), without the user: replaced
        the first time, its task blocked when its replacement goes quiet too. Any other quiet spell is told to the user
        once. Returns whether the orchestrator still needs the watcher's message for it, and the message."""
        def change(config, state):
            worker = state["workers"].get(worker_id)
            if not worker or worker["state"] != "working":
                return {"send": False}
            label, task = worker_label(state, worker)
            if (worker.get("quiet") or {}).get("since") != since:
                worker["quiet"] = {"since": since, "sent": False}
                if state["control"] == "active" and self.quiet_act(state, worker, self.owing(state, worker)):
                    worker["quiet"]["replace"] = True
                else:
                    advice = f" Say restart {task['task']} to replace it." if task else ""
                    state["tell"].append(f"{label} has been quiet since {since} ({minutes} minutes or more).{advice}")
            return {"send": not worker["quiet"]["sent"], "message": (
                f"use t3 workflow: {label} has been quiet for {minutes} minutes. "
                f"Run t3-workflow '{{\"op\":\"next\"}}' and tell the user.")}
        return self.transact(project, change, "quiet")

    @staticmethod
    def quiet_due(worker):
        """Whether this worker's quiet spell waits for `next` to deal with it."""
        spell = worker.get("quiet") or {}
        return bool(spell.get("replace")) and not spell.get("acted") and worker["state"] == "working"

    def quiet_act(self, state, worker, task):
        """What a quiet spell of this worker does without the user: "replace" it the first time for its work, "block" its
        task when a replacement went quiet too before that work reported. None when it owes nothing (or is a DevOps worker
        that holds no task and was replaced already): the user is told instead."""
        devops = worker["role"] == "devops"
        if not task and not (devops and not (worker.get("job") or {}).get("reported")):
            return None
        item = task or state["devops"].get(worker["plan"]) or {}
        if worker["role"] not in (item.get("quietReplaced") or []):
            return "replace"
        return "block" if task else None

    def quiet_over(self, state, worker, task, act):
        """A quiet worker is cancelled. The first time, its work goes back to where `next` starts a new worker in its
        role, as a restart does, and that work counts one replacement. The next time, its task is blocked for the user."""
        role, since = worker["role"], worker["quiet"]["since"]
        label, _ = worker_label(state, worker)
        name = "DevOps worker" if role == "devops" else role
        why = f"its {name} was quiet since {since}"
        self.retire(state, worker, why)
        if act == "block":
            task["blockedFrom"], task["blockedWorker"] = task["state"], worker["id"]
            task.pop("devops", None)
            self.move(task, "blocked", f"{why}, after a new one took over")
            state["tell"].append(f"{task['task']} ({task['plan']}) is blocked: its new {name} was quiet too, since "
                                 f"{since}. Say restart to give it another {name}, or drop it.")
            return
        stopped(task, worker, "quiet")
        if role == "devops":
            self.devops_back(state, worker["plan"], task, why)
        else:
            self.move(task, "waiting" if role == "implementer" else "review_needed", why)
        item = task or state["devops"][worker["plan"]]
        item["quietReplaced"] = [*(item.get("quietReplaced") or []), role]
        then = "a new DevOps worker starts when there is work" if role == "devops" and not task else f"a new {name} took over"
        state["tell"].append(f"{label} was quiet since {since}, so it was stopped and {then}.")

    def quiet_sent(self, project, worker_id, since):
        def change(config, state):
            spell = (state["workers"].get(worker_id) or {}).get("quiet") or {}
            if spell.get("since") == since:
                spell["sent"] = True
            return {"worker": worker_id}
        return self.transact(project, change, "quiet_sent")

    def status(self, project, req):
        config, state = self.load(project)
        return {"control": state["control"], "repo": config["github"]["repo"],
                "plans": {name: p["status"] for name, p in state["plans"].items()},
                "dependsOn": {name: p["dependsOn"] for name, p in state["plans"].items() if p["dependsOn"]},
                "tasks": [{k: t.get(k) for k in ("id", "plan", "task", "state", "after", "unlinked", "needed", "pr", "head", "held")}
                          for t in state["tasks"].values()],
                "working": sorted(w["id"] for w in state["workers"].values() if w["state"] == "working"),
                "models": {role: role_model(config, state, role) for role in ROLES}, "devops": state.get("devopsOn", True),
                "ci": state.get("ciOn", True), "guarded": state.get("guarded") or [], "pendingCalls": len(state["calls"]), "openAsks": [a for a in state["asks"].values() if not a.get("answered")],
                "problems": health.problems(self.path.parent, scanning=watcher.running(self.path.parent))}

    def unblocked(self, state, task):
        plan = state["plans"][task["plan"]]
        return (all(state["plans"][p]["status"] == "done" for p in plan["dependsOn"])
                and all(state["tasks"][a]["state"] in {"merged", "dropped"} for a in task["after"]))

    def parked(self, state, task):
        """The implementer of a task that waits since it ran worker waits, while a follow-up can still reach it."""
        worker = state["workers"].get(task.get("implementer")) if task["state"] == "waiting" and task.get("needed") else None
        return worker if worker and worker["state"] != "cancelled" and worker["childThreadId"] else None

    def resume(self, config, state, task):
        """What a parked task waited for is settled: merged, dropped, or the user let it go on without it. Its own
        implementer carries on in the same checkout."""
        me = f"--worker {task['implementer']} --token {state['workers'][task['implementer']]['token']}"
        lines = []
        for need in task["needed"]:
            other = state["tasks"][need["task"]]
            if other["state"] == "merged":
                lines.append(f"{other['task']} merged.")
            elif other["state"] == "dropped":
                lines.append(f"{other['task']} was dropped, so its code is not coming. If this task cannot go on without "
                             f"it, run {command('worker', 'stuck', me, '--why', QUESTION)} and end your turn.")
            else:
                why = next((u["why"] for u in reversed(task.get("unlinked") or []) if u.get("task") == other["id"]), "")
                lines.append(f"The user let this task go on without {other['task']}: {why}")
        task["fix"] = {"kind": "resume", "detail": " ".join(lines)}
        self.move(task, "implementing", "what it waited for is settled")
        self.send_fix(config, state, task)

    def move(self, task, to, note=None):
        task["history"].append({"at": now(), "from": task["state"], "to": to, **({"note": note} if note else {})})
        task["state"] = to
        task["rev"] += 1
        if to in {"merged", "dropped"}:
            self.finished.append(task["id"])

    def add_worker(self, config, state, role, plan, task_id, choice=None):
        """The model comes from the user's setting for the role; else Jev's advice (it names one of the options); else
        the default. An implementer has no default model: without Jev's answer it starts on the fallback (FALLBACK)
        while that can start, else on the best scored of the options. The effort is the user's, else the default: Jev
        sets none, and an effort a task's choice kept from an older version is not used. Advice marked "limit" stands in
        for a model that cannot start now, the user's own included; without it, the best scored of the options does.
        A model the user picked runs on the provider they picked it from; any other runs on its first route among the
        options (see options()), at the effort that route takes. The worker keeps its model entry, so a later change
        never reaches a worker already started."""
        pick = (state.get("picks") or {}).get(role) or {}
        choice = choice or {}
        lists, spent = t3_lists()[0], limits_now(state)
        found = options(config, role, lists, spent)
        own, provider = usual(config, state, role)
        advised = choice.get(role) if role in (config["models"].get(choice.get(role)) or {}).get("roles", []) else None
        standing = None  # The model a stand-in stands in for.
        if advised and (choice.get("limit") or not pick.get("model")):
            model, source, standing = advised, "limit" if choice.get("limit") else choice.get("source", "jev"), choice.get("limit")
        elif own:  # The user's pick, else the default, unless it cannot start now.
            model, source = own, "pick" if pick.get("model") else "default"
            out = at_limit(spent, own, provider) if pick.get("model") else own not in found
            best = next(iter(jev.ranked([m for m in found if m != own])), None) if out else None
            if best:
                model, source, standing = best, "limit", own
        else:
            model, source = fallback(found)
            if not model:  # No option can start now: as before, on what T3 runs.
                model, source = fallback(options(config, role, lists, {}) or options(config, role, None, {}))
        effort = pick.get("effort") or config["defaults"][role + "Effort"]
        worker = {"id": next_id(state, "w"), "token": secrets.token_hex(16), "role": role, "plan": plan, "task": task_id,
                  "model": model, "effort": effort, "modelSource": source, "childThreadId": None,
                  "taskIds": [], "state": "prepared", "job": None, "path": None}
        if source == "pick":
            worker["entry"] = {"provider": pick.get("provider", GOOSE), "roles": [role], "thinking": pick.get("thinking", False)}
        else:
            if effort not in EFFORTS:  # A level only the user's own model has does not carry to another.
                effort = config["defaults"][role + "Effort"]
            item = config["models"][model]
            route = found.get(model) or next((r for r in routes(item, model) if runs(lists, r["model"], r["provider"])),
                                             routes(item, model)[0])
            worker.update({k: v for k, v in placed(config, model, route, effort, lists).items() if v})
        if standing:
            worker["standsInFor"] = standing
            line = (f"{standing} is at its usage limit, so a new {'DevOps' if role == 'devops' else role} runs on "
                    f"{model} until it resets.")
            if line not in state["tell"]:
                state["tell"].append(line)
        state["workers"][worker["id"]] = worker
        return worker

    def advise(self, config, state, role, context, lists, spent):
        """Jev's advice for one new worker, {} without any. Jev chooses an implementer's model where the user left it
        open, among the options (see options()). A model the user picked, and a reviewer's or DevOps's default, it
        replaces only while that model cannot start now (the default's later routes come first): then with a stand-in
        among the options, marked "limit". A choice made while a limit kept out an approved model T3 runs is marked
        "narrowed", so a restart after the reset asks again."""
        model, provider = usual(config, state, role)
        picked = bool(((state.get("picks") or {}).get(role) or {}).get("model"))
        found = options(config, role, lists, spent)
        out = at_limit(spent, model, provider) if picked else bool(model) and model not in found
        if not out and (role != "implementer" or picked):
            return {}
        found = {m: route for m, route in found.items() if not (out and m == model)}
        if not found:
            return {}  # Nothing else can start, so the worker waits for the reset on its own model.
        narrowed = bool(set(options(config, role, lists, {})) - set(found) - {model})
        choice = self.ask_jev(config, state, role, context, found)
        return {**choice, **({"limit": model} if out else {"narrowed": True} if narrowed else {})} if choice else {}

    def ask_jev(self, config, state, role, context, found):
        """Jev's choice among the options found, {role: model}; {} when Jev gives no answer or names another."""
        known = self.records(role)
        weighed = {**config, "models": {m: {**config["models"][m], "route": route, "record": known.get(m)}
                                        for m, route in found.items()}}
        try:
            choice = self.jev(weighed, context, role) or {}
            health.ok(self.path.parent, "jev")
        except Exception as error:  # noqa: BLE001 - Jev is advice; the worker starts without it
            health.failed(self.path.parent, "jev", error)
            return {}
        return {role: choice[role]} if choice.get(role) in found else {}

    def delegate(self, config, state, worker, title, role, isolation, brief):
        call_id = f"{worker['id']}-start"
        state["calls"][call_id] = {"call": call_id, "worker": worker["id"], "tool": "delegate_task", "arguments": {
            "task": brief, "title": title, "role": role, "isolation": isolation, "mode": "async",
            "target": selection(config, worker["model"], worker["effort"], worker.get("entry")), "clientRequestId": call_id}}

    def start_implementer(self, config, state, task, choice):
        plan = state["plans"][task["plan"]]
        if not plan["started"]:
            plan["started"], plan["devops"] = True, state.get("devopsOn", True)
            count = sum(1 for t in state["tasks"].values() if t["plan"] == plan["plan"])
            state["tell"].append(f"Plan {plan['plan']} started: {count} task{'s' if count != 1 else ''}.")
        task["choice"] = choice or {}
        worker = self.add_worker(config, state, "implementer", task["plan"], task["id"], choice)
        worker["job"] = {"kind": "implement", "reported": False, "nudged": 0}
        task["implementer"] = worker["id"]
        self.move(task, "implementing")
        self.delegate(config, state, worker, f"{task['plan']}: {task['task']}", "implementation", "worktree",
                      implementer_brief(config, state, task, worker))

    def start_reviewer(self, config, state, task, choice=None):
        worker = self.add_worker(config, state, "reviewer", task["plan"], task["id"], choice)
        self.other_family(config, state, task, worker)
        worker["job"] = {"kind": "review", "reported": False, "nudged": 0}
        task["reviewer"] = worker["id"]
        self.move(task, "reviewing")
        self.delegate(config, state, worker, f"Review {task['plan']}: {task['task']}", "review", "shared",
                      reviewer_brief(config, task, worker))

    def other_family(self, config, state, task, worker):
        """A reviewer whose model is of the same family as the model that wrote the PR (its implementer's; for a pipeline
        fix, its plan's DevOps worker's) runs instead on the best scored of the reviewer options (see options()) of
        another family, over the user's pick too. With none, it runs as it would have."""
        author = state["workers"].get(task.get("implementer"))
        if not author or family(author["model"]) != family(worker["model"]):
            return
        lists, spent = t3_lists()[0], limits_now(state)
        others = {m: route for m, route in options(config, "reviewer", lists, spent).items()
                  if family(m) != family(author["model"])}
        best = next(iter(jev.ranked(list(others))), None)
        if not best:
            return
        if worker.get("standsInFor"):  # The usage-limit line named the stand-in this one replaces.
            state["tell"] = [line for line in state["tell"] if not line.startswith(
                f"{worker['standsInFor']} is at its usage limit, so a new reviewer runs on {worker['model']} ")]
        for key in ("entry", "standsInFor", "via", "paid"):
            worker.pop(key, None)
        effort = ((state.get("picks") or {}).get("reviewer") or {}).get("effort")
        effort = effort if effort in EFFORTS else config["defaults"]["reviewerEffort"]  # Not a level only one model has.
        worker.update({k: v for k, v in placed(config, best, others[best], effort, lists).items() if v})
        worker.update(modelSource="family", otherThan=author["model"],
                      otherRole="DevOps worker" if author["role"] == "devops" else "implementer")

    def confirm(self, state, task, pr, checks):
        """The orchestrator's rule: the reviewed commit is the head, CI has not failed, only the task's paths changed, and
        no guarded path did."""
        if pr["head"] != task["approvedSha"]:
            task["fix"] = {"kind": "moved", "detail": f"PR #{task['pr']} moved after its approval."}
            self.move(task, "implementing", "head moved after approval")
            return
        outside = [f for f in pr["files"] if not owned(f, task["owns"])]
        guarded = [f for f in pr["files"] if owned(f, state.get("guarded") or [])]
        if outside or guarded:
            what = [*([f"changes files outside {', '.join(task['owns'])}: {listed(outside)}"] if outside else []),
                    *([f"changes guarded files: {listed(guarded)}"] if guarded else [])]
            ask_id = next_id(state, "a")
            state["asks"][ask_id] = {"id": ask_id, "task": task["id"], "kind": "outside", "outside": outside,
                                     "guarded": guarded, "question": (
                f"PR #{task['pr']} ({task['task']}) {' and '.join(what)}. Merge it anyway, send it back, or drop it?"),
                "options": ["merge", "back", "drop"]}
            self.move(task, "asking", " and ".join(
                [*(["files outside the task's paths"] if outside else []), *(["guarded files"] if guarded else [])]))
            return
        failed = [name for name, result in checks.items() if result == "failure"]
        if failed and not devops_on(state, task["plan"]):  # Without DevOps every CI failure is the implementer's.
            task["fix"] = {"kind": "ci", "detail": f"CI failed on PR #{task['pr']}: {', '.join(failed)}."}
            self.move(task, "implementing", f"CI failed: {', '.join(failed)}")
            return
        if failed:
            task["ciFailed"] = failed
            self.move(task, "ci_failed", f"CI failed: {', '.join(failed)}")
            return
        self.move(task, "confirmed", "the orchestrator's rule holds")

    def send(self, state, worker, kind, message):
        """A follow-up to a worker's child thread; it starts a new run there, which wakes the orchestrator when it ends."""
        count = sum(1 for c in state["calls"].values() if c["worker"] == worker["id"])
        call_id = f"{worker['id']}-{kind}-{state['version']}-{count}"
        state["calls"][call_id] = {"call": call_id, "worker": worker["id"], "tool": "t3_thread_send", "arguments": {
            "threadId": worker["childThreadId"], "message": message, "mode": "queue", "clientRequestId": call_id}}

    def send_fix(self, config, state, task):
        worker = state["workers"][task["implementer"]]
        if not worker["childThreadId"]:
            return  # Its start is not confirmed yet.
        fix = task.pop("fix")
        if fix["kind"] != "resume":
            task["returned"] = fix["detail"]  # The next reviewer is told why the PR came back.
        worker["job"] = {"kind": "fix", "reported": False, "nudged": 0}
        me = f"--worker {worker['id']} --token {worker['token']}"
        kept = task.get("pushedBranch")
        guide = {"review": f"Read the reviewer's comment (gh pr view {task.get('pr')} --comments) and address it.",
                 "conflict": f"Run git fetch origin && git merge origin/{config['branch']}, resolve the conflicts, commit.",
                 "ci": f"Read the failing CI run (gh pr checks {task.get('pr')}; gh run view <id> --log-failed) and fix it.",
                 "moved": "Make sure this checkout is at the PR's head.",
                 "resume": (f"Run git fetch origin && git merge origin/{config['branch']} in this checkout, then finish the "
                            "task." + (f" If this checkout lost your work, first run git fetch origin && git reset --hard "
                                       f"origin/{kept}." if kept else ""))}.get(fix["kind"], "")
        if fix["kind"] == "ci" and not devops_on(state, task["plan"]):
            guide += (" If CI itself is broken, not this task's code, run "
                      + command("worker", "stuck", me, "--why", '"<what is broken>"') + " so the user decides, and end your turn.")
        number = str(task["pr"]) if task.get("pr") else "<number>"
        self.send(state, worker, "fix", "\n".join([line for line in [
            fix["detail"], guide, "Then push (opening the PR as your brief says if there is none yet), and run: "
            + command("worker", "pr", me, "--pr", number), "When it succeeds, end your turn."] if line]))

    def call_devops(self, config, state, choice=None, held=lambda entry, name: False):
        """One DevOps worker per plan with work for it: started the first time, woken by a follow-up when idle.
        While a merge runs nobody is called: take would give them nothing. `held` says a new one must wait for a reset."""
        if any(t["state"] == "merging" for t in state["tasks"].values()):
            return
        for name, plan in state["plans"].items():
            if not devops_on(state, name) or not any(
                    t["plan"] == name and t["state"] in {"confirmed", "ci_failed"} for t in state["tasks"].values()):
                continue
            entry = state["devops"].setdefault(name, {"worker": None, "state": "none"})
            if entry["state"] == "none" and not held(entry, name):
                worker = self.add_worker(config, state, "devops", name, None, choice)
                worker["job"] = {"kind": "devops", "reported": False, "nudged": 0}
                entry.update(worker=worker["id"], state="working")
                self.delegate(config, state, worker, f"DevOps: {name}", "general", "worktree", devops_brief(config, name, worker))
            elif entry["state"] == "idle":
                worker = state["workers"][entry["worker"]]
                if worker["childThreadId"]:
                    worker["job"] = {"kind": "devops", "reported": False, "nudged": 0}
                    entry["state"] = "working"
                    me = f"--worker {worker['id']} --token {worker['token']}"
                    self.send(state, worker, "work", "There is work for you. Run " + command("worker", "take", me)
                              + " and follow devops.md, then end your turn.")

    # Workers

    def worker(self, command, args):
        project, worker = self.find_worker(args["worker"], args["token"])
        handler = {"pr": self.pr, "stuck": self.stuck, "waits": self.waits, "review": self.review, "take": self.take,
                   "update": self.update, "merge": self.merge, "failed": self.failed, "pipeline": self.pipeline}.get(command)
        require(handler, f"{command} is not available yet")
        if command in {"take", "update", "merge", "failed", "pipeline"}:
            require(worker["role"] == "devops", f"Only DevOps runs {command}")
        if command == "waits":
            require(worker["role"] == "implementer", "Only an implementer runs waits")
        return handler(project, worker, args)

    def find_worker(self, worker_id, token):
        """Worker ids are counters per project, so several projects share them: the token picks the worker."""
        known = False
        for project, raw in self.db.execute("SELECT id, state FROM projects"):
            worker = json.loads(raw)["workers"].get(worker_id)
            if worker:
                known = True
                if hmac.compare_digest(worker["token"], token):
                    return project, worker
        raise Refused("This worker's token does not match" if known else f"No worker {worker_id} is bound")

    def own_checkout(self, config, path):
        """The worker's checkout must be the top of a clean worktree of the bound repository."""
        tree = Path(path).resolve()
        top = Path(git(tree, "rev-parse", "--show-toplevel")).resolve()
        require(top == tree, f"{tree} is not the top of a git checkout")
        common = Path(tree, git(tree, "rev-parse", "--git-common-dir")).resolve()
        bound = Path(config["workspace"], git(config["workspace"], "rev-parse", "--git-common-dir")).resolve()
        require(common == bound, f"{tree} is not a checkout of the bound repository {config['workspace']}")
        require(not git(tree, "status", "--porcelain"), f"{tree} has uncommitted changes; commit and push them first")
        return tree

    def checkout(self, config, path, pr):
        """The worker's checkout must be a clean worktree of the bound repository at the PR's head."""
        tree = self.own_checkout(config, path)
        head = git(tree, "rev-parse", "HEAD")
        require(head == pr["head"], f"{tree} is at {head[:12]} but PR #{pr['number']} is at {pr['head'][:12]}; push first")
        return str(tree)

    def pushed(self, config, path):
        """The worker's checkout holds nothing GitHub lacks: it is clean and its own commits are on a pushed branch.
        Returns that branch, or None when it has no commits of its own."""
        tree, main = self.own_checkout(config, path), f"origin/{config['branch']}"
        if git(tree, "merge-base", "--is-ancestor", "HEAD", main, check=False)[0] == 0:
            return None
        found = git(tree, "for-each-ref", "--contains", "HEAD", "--format=%(refname:short)", "refs/remotes/origin").split()
        branches = [b for b in found if b.startswith("origin/") and b not in {main, "origin/HEAD"}]
        require(branches, f"{tree} has commits that are not pushed; push them (git push -u origin HEAD) first")
        upstream = git(tree, "rev-parse", "--abbrev-ref", "@{u}", check=False)[1]
        return (upstream if upstream in branches else branches[0]).removeprefix("origin/")

    def run_checks(self, config, project, name, tree, head):
        """The binding's local checks in the worker's checkout; their logs go under state/harness-artifacts."""
        folder = self.path.parent.joinpath("harness-artifacts", *(places.segment(part) for part in (project, name, head)))
        folder.mkdir(parents=True, exist_ok=True)
        results = []
        for index, check in enumerate(config["checks"], 1):
            log = folder / f"check-{index}.log"
            code, error = None, None
            with log.open("wb") as output:
                try:
                    code = subprocess.run(check["argv"], cwd=Path(tree, check["cwd"]), stdout=output,
                                          stderr=subprocess.STDOUT, timeout=check["timeoutSeconds"]).returncode
                except subprocess.TimeoutExpired:
                    error = f"did not finish within {check['timeoutSeconds']} seconds"
                except OSError as failure:
                    error = f"could not start: {failure}"
            tail = log.read_bytes()[-2000:].decode("utf-8", "replace")
            results.append({"name": check["name"], "passed": code == 0, "returncode": code, "error": error,
                            "log": str(log), "tail": tail})
        return {"head": head, "passed": all(r["passed"] for r in results), "results": results,
                "noChecksReason": None if results else config.get("noChecksReason")}

    def pr(self, project, worker, args):
        require(args.get("pr"), "--pr names your pull request's number")
        config, state = self.load(project)
        task = state["tasks"].get(worker["task"])
        require(task and task.get("implementer") == worker["id"] and task["state"] == "implementing",
                "This task is not waiting for your pull request")
        require(not task.get("pr") or task["pr"] == args["pr"], f"This task's pull request is #{task.get('pr')}")
        rev = task["rev"]
        decisions = task.get("kind") != "pipeline"  # A pipeline fix's later rounds come here too, and need none.
        pr, tree, evidence = self.submitted(config, project, task["id"], args, decisions=decisions)

        def change(config, state):
            task = state["tasks"][worker["task"]]
            require(task["rev"] == rev, "The task changed while the checks ran; run this again")
            task.update(pr=pr["number"], url=pr["url"], head=pr["head"], branch=pr["branch"], files=pr["files"],
                        worktree=tree, evidence=evidence, size=pr_size(pr, task.get("size")))
            state["workers"][worker["id"]].update(path=tree)
            self.reported(state, worker, task)
            self.move(task, "review_needed", f"PR #{pr['number']} at {pr['head'][:12]}")
            return {"state": "review_needed", "pr": pr["number"], "head": pr["head"],
                    "next": "The reviewer is called next. End your turn."}
        return self.transact(project, change, "pr")

    def stuck(self, project, worker, args):
        why = text(args.get("why"), "--why")

        def change(config, state):
            task = state["tasks"].get(worker["task"])
            require(task and task.get("implementer") == worker["id"] and task["state"] == "implementing",
                    "This task is not waiting on you")
            self.reported(state, worker, task)
            ask_id = next_id(state, "a")
            state["asks"][ask_id] = {"id": ask_id, "task": task["id"], "kind": "stuck", "options": None,
                                     "question": f"The implementer of {task['task']} ({task['plan']}) asks: {why}"}
            task["blockedFrom"] = "implementing"
            self.move(task, "blocked", "the implementer is stuck")
            return {"state": "blocked", "next": "Your question goes to the user; the answer comes as a follow-up. End your turn."}
        return self.transact(project, change, "stuck")

    def waits(self, project, worker, args):
        """The implementer needs code another open task is still building. Its task waits for that task again, and the
        same implementer carries on when it merges. Its work must be pushed first, and a task waits only once."""
        why = text(args.get("why"), "--why (the file you need and what in it)")
        on = [text(o, "--on") for o in args.get("on") or []]
        require(on, "--on names the task whose code you need")
        config, _ = self.load(project)
        kept = self.pushed(config, args.get("path") or Path.cwd())

        def change(config, state):
            task = state["tasks"].get(worker["task"])
            require(task and task.get("implementer") == worker["id"] and task["state"] == "implementing",
                    "This task is not waiting on you")
            stuck = command("worker", "stuck", f"--worker {worker['id']} --token {worker['token']}", "--why", QUESTION)
            needs = []
            for name in on:
                other = state["tasks"][self.task_id(state, name)]
                require(other["id"] != task["id"], f"{other['task']} is its own task: build what it needs here")
                require(other["state"] != "merged", (
                    f"{other['task']} already merged as {(other.get('mergeCommit') or '')[:12]}: run git fetch origin && "
                    f"git merge origin/{config['branch']}, then carry on"))
                require(other["state"] != "dropped", (
                    f"{other['task']} was dropped, so its code is not coming; if this task cannot go on without it, "
                    f"run {stuck}"))
                require(any(owned(p, other["owns"]) for p in named_paths(why)), (
                    f"--why must name the file you need from {other['task']}, inside its paths ({', '.join(other['owns'])}), "
                    f"and what in it, for example \"{other['owns'][0].rstrip('/')}/name.ts has no exportName yet\""))
                needs.append(other)
            require(not task.get("needed"), (
                f"This task already waited once, for {', '.join(n['name'] for n in task.get('needed') or [])}. If it needs code no "
                f"open task will build, run {stuck}"))
            task["after"] = task["after"] + [o["id"] for o in needs if o["id"] not in task["after"]]
            task["needed"] = [{"task": o["id"], "name": o["task"], "why": why, "at": now()} for o in needs]
            if kept:
                task["pushedBranch"] = kept
            self.reported(state, worker, task)
            self.move(task, "waiting", f"its implementer needs {', '.join(o['id'] for o in needs)}: {why}")
            require(not waits_forever(state), (
                f"That would wait forever: {', '.join(o['task'] for o in needs)} waits for this task, directly or "
                "through others"))
            names = " and ".join(o["task"] for o in needs)
            return {"state": "waiting", "next": f"When {names} {'merge' if len(needs) > 1 else 'merges'}, a follow-up "
                                                "wakes you here to carry on. End your turn."}
        return self.transact(project, change, "waits")

    def submitted(self, config, project, name, args, decisions=False):
        """A worker's PR as it stands: open against the bound branch, with a Decisions section in its body when
        `decisions` asks, the worker's clean checkout at its head, and the binding's checks passing there. Returns (pr,
        checkout, evidence); a failed check refuses with its output."""
        pr = self.hub_for(config["github"]["repo"]).pull(args["pr"])
        require(pr["state"] == "open" and pr["base"] == config["branch"],
                f"PR #{args['pr']} must be open against {config['branch']}")
        require(not decisions or has_decisions(pr.get("body")), (
            f"PR #{args['pr']}'s body has no Decisions section. Add one at its end: a \"## Decisions\" heading, then "
            f"each call your brief left open, what you chose and why, or \"None\" when there were none. Write the whole "
            f"new body to a file, run gh pr edit {args['pr']} --body-file <that file>, then run this again."))
        tree = self.checkout(config, args.get("path") or Path.cwd(), pr)
        evidence = self.run_checks(config, project, name, tree, pr["head"])
        if not evidence["passed"]:
            raise Refused("Checks failed; fix them, push, and run this again:\n" + "\n".join(
                f"--- {r['name']}: {r['error'] or 'exit ' + str(r['returncode'])}\n{r['tail']}"
                for r in evidence["results"] if not r["passed"]))
        return pr, tree, evidence

    def review(self, project, worker, args):
        verdict, sha = args.get("verdict"), args.get("sha")
        require(verdict and sha, "--verdict approve|changes and --sha <the commit you reviewed> are both needed")
        config, state = self.load(project)
        task = state["tasks"].get(worker["task"])
        require(task and task.get("reviewer") == worker["id"] and task["state"] == "reviewing",
                "This task is not waiting for your review")
        require(sha == task["head"], f"You were asked to review {task['head']}, not {sha}")
        rev = task["rev"]
        gh = self.hub_for(config["github"]["repo"])
        head = gh.pull(task["pr"])["head"]
        require(head == sha, f"PR #{task['pr']} moved to {head[:12]} after the commit you reviewed; end your turn")
        if verdict == "approve":
            gh.approve(sha, f"Approved by the t3 workflow reviewer ({worker['model']})")

        def change(config, state):
            task = state["tasks"][worker["task"]]
            require(task["rev"] == rev, "The task changed during your review; end your turn")
            self.reported(state, worker, task)
            task.setdefault("reviews", []).append({"sha": sha, "verdict": verdict, "model": worker["model"]})
            task.pop("returned", None)
            if verdict == "approve":
                task["approvedSha"] = sha
                self.move(task, "approved", f"t3/review on {sha[:12]}")
            else:
                task["rounds"] = task.get("rounds", 0) + 1
                if task["rounds"] >= REVIEW_ROUNDS:
                    self.ask_rounds(state, task)
                else:
                    task["fix"] = review_fix(task)
                    self.move(task, "implementing", "changes requested")
            return {"state": task["state"], "next": "End your turn."}
        return self.transact(project, change, "review")

    # DevOps

    def held(self, state, worker):
        """The task this DevOps worker took and has not finished."""
        found = [t for t in state["tasks"].values() if t["state"] == "merging" and t.get("devops") == worker["id"]]
        require(found, "You hold no task; run take")
        return found[0]

    def take(self, project, worker, args):
        def change(config, state):
            me = state["workers"][worker["id"]]
            entry = state["devops"][me["plan"]]
            merging = [t for t in state["tasks"].values() if t["state"] == "merging"]
            mine = [t for t in merging if t.get("devops") == me["id"]]
            ready = sorted((t for t in state["tasks"].values()
                            if t["plan"] == me["plan"] and t["state"] in {"confirmed", "ci_failed"}),
                           key=lambda t: int(t["id"][1:]))
            if mine:
                task = mine[0]
            elif merging or not ready or state["control"] != "active":
                self.reported(state, me)
                entry["state"] = "idle"
                why = "Another merge is in progress" if merging else "Nothing is waiting for you"
                return {"job": None, "next": f"{why}. End your turn; a follow-up wakes you when there is work."}
            else:
                task = ready[0]
                task["devops"] = me["id"]
                task["job"] = "merge" if task["state"] == "confirmed" else "triage"
                self.move(task, "merging", f"taken by {me['id']}")
            me["job"] = {"kind": "devops", "reported": False, "nudged": 0}
            entry["state"] = "working"
            return {"job": task["job"], "task": task["id"], "pr": task["pr"], "head": task["head"], "url": task.get("url"),
                    "branch": task.get("branch"), **({"failed": task.get("ciFailed")} if task["job"] == "triage" else {}),
                    "next": devops_steps(config, task)}
        return self.transact(project, change, "take")

    def update(self, project, worker, args):
        config, state = self.load(project)
        task = self.held(state, worker)
        rev, number = task["rev"], task["pr"]
        gh = self.hub_for(config["github"]["repo"])
        pr = gh.pull(number)
        same = False
        if pr["head"] != task["head"]:
            outcome, head = "moved", pr["head"]
        else:
            before = gh.identity(number)
            outcome, head = gh.update(number, task["head"], config["branch"])
            same = outcome == "updated" and before is not None and gh.identity(number) == before
            if same:
                gh.approve(head, f"Carried from {task['head'][:12]}: the same change after updating with {config['branch']}")
                self.follow(task.get("worktree"), task.get("branch"))

        def change(config, state):
            task = state["tasks"][self.held(state, worker)["id"]]
            require(task["rev"] == rev, "The task changed meanwhile; run take")
            if outcome == "current":
                return {"result": "current", "head": head, "next": wait_steps(config, state, task)}
            if same:
                task.update(head=head, approvedSha=head)
                task["rev"] += 1
                task["history"].append({"at": now(), "from": "merging", "to": "merging",
                                        "note": f"updated with {config['branch']}; approval carried to {head[:12]}"})
                return {"result": "updated", "head": head, "next": wait_steps(config, state, task)}
            task["fix"] = update_fix(config, outcome, number)
            task["head"] = head
            task.pop("devops", None)
            self.reported(state, worker, task)
            self.move(task, "implementing", f"update: {outcome}")
            return {"result": outcome, "next": "The task went back to its implementer. End your turn."}
        return self.transact(project, change, "update")

    def merge(self, project, worker, args):
        config, state = self.load(project)
        task = self.held(state, worker)
        rev, number = task["rev"], task["pr"]
        gh = self.hub_for(config["github"]["repo"])
        pr = gh.pull(number)
        if pr["merged"]:  # An earlier merge that was not recorded.
            sha = pr["mergeCommit"]
        else:
            require(pr["state"] == "open", f"PR #{number} is closed without being merged")
            require(pr["head"] == task["head"] == task.get("approvedSha"),
                    f"PR #{number} is at {pr['head'][:12]}, but the approved commit is "
                    f"{(task.get('approvedSha') or 'none')[:12]}; run update")
            names = [*ci_checks(config, state), github.REVIEW_CONTEXT]
            waiting = [f"{name} is {result}" for name, result in gh.checks(pr["head"], names).items() if result != "success"]
            require(not waiting, f"PR #{number} cannot merge yet: {', '.join(waiting)}. {wait_steps(config, state, task)}")
            sha = gh.merge(number, pr["head"], f"{task['plan']}: {task['task']} (#{number})")
        moved = self.forward(config)

        def change(config, state):
            task = state["tasks"][self.held(state, worker)["id"]]
            require(task["rev"] == rev, "The task changed meanwhile")
            task.pop("devops", None)
            task["size"] = pr_size(pr, task.get("size"))
            self.reported(state, worker, task)
            self.landed(state, task, sha, moved)
            return {"merged": sha, "next": "End your turn."}
        return self.transact(project, change, "merge")

    def landed(self, state, task, sha, moved):
        """A task's PR merged: the user is told, tasks waiting on it as a pipeline fix go on, and its plan may finish."""
        task["mergeCommit"] = sha
        self.move(task, "merged", f"squash-merged as {sha[:12]}")
        state["tell"].append(f"Merged PR #{task['pr']}: {task['task']} ({task['plan']}).")
        if moved:
            state["tell"].append(moved)
        for waiting in state["tasks"].values():
            if waiting["state"] == "pipeline_wait" and waiting.get("waitingOn") == task["id"]:
                self.move(waiting, "confirmed", f"the pipeline fix {task['id']} merged")
        self.finish_plan(state, task["plan"])

    # Merging without DevOps: `next` runs these first; the watcher wakes the orchestrator when there is a step to take.

    def harness_task(self, state):
        """The task the harness merges for a plan without DevOps: the one it holds, else the oldest confirmed one while
        no merge runs. One merge at a time, as with DevOps."""
        merging = [t for t in state["tasks"].values() if t["state"] == "merging"]
        if merging:
            return next((t for t in merging if t.get("merger") == "harness"), None)
        ready = [t for t in state["tasks"].values() if t["state"] == "confirmed" and not devops_on(state, t["plan"])]
        return min(ready, key=lambda t: int(t["id"][1:]), default=None)

    def harness_merges(self, project):
        """Each step brings the PR up to date (carrying the approval when its change is the same), waits for CI, and
        merges with DevOps's checks. What cannot merge as reviewed goes back to its implementer."""
        while True:
            config, state = self.load(project)
            task = self.harness_task(state) if state["control"] == "active" else None
            if not task:
                return
            outcome = self.merge_step(config, task, ci_checks(config, state))
            self.transact(project, lambda config, state: self.merge_record(config, state, task["id"], task["rev"], outcome),
                          "harness_merge")
            if outcome["result"] != "merged":
                return

    def merge_step(self, config, task, ci):
        """The GitHub side of one step, outside any transaction; ci names the CI checks it waits for."""
        try:
            outcome = self.github_step(config, task, ci)
        except github.Failed as error:
            health.failed(self.path.parent, "github", error)
            return {"result": "github", "error": str(error)}
        health.ok(self.path.parent, "github")
        return outcome

    def github_step(self, config, task, ci):
        gh, number = self.hub_for(config["github"]["repo"]), task["pr"]
        pr = gh.pull(number)
        if pr["merged"]:  # An earlier merge that was not recorded.
            return {"result": "merged", "sha": pr["mergeCommit"], "moved": self.forward(config), "size": pr_size(pr)}
        if pr["state"] != "open":
            return {"result": "closed"}
        if pr["head"] != task.get("approvedSha"):
            return {"result": "moved", "head": pr["head"]}
        before = gh.identity(number)
        outcome, head = gh.update(number, pr["head"], config["branch"])
        if outcome == "updated" and before is not None and gh.identity(number) == before:
            gh.approve(head, f"Carried from {pr['head'][:12]}: the same change after updating with {config['branch']}")
            self.follow(task.get("worktree"), task.get("branch"))
            outcome = "carried"
        if outcome != "current":
            return {"result": outcome, "head": head}
        checks = gh.checks(head, [*ci, github.REVIEW_CONTEXT])
        failed = [name for name, result in checks.items() if result == "failure"]
        if failed:
            return {"result": "ci", "failed": failed}
        if any(result != "success" for result in checks.values()):
            return {"result": "waiting"}
        sha = gh.merge(number, head, f"{task['plan']}: {task['task']} (#{number})")
        return {"result": "merged", "sha": sha, "moved": self.forward(config), "size": pr_size(pr)}

    def merge_record(self, config, state, task_id, rev, outcome):
        task, result = state["tasks"][task_id], outcome["result"]
        number = task["pr"]
        if result == "merged":  # A fact on GitHub, whatever happened here meanwhile.
            task.pop("merger", None)
            task["size"] = outcome.get("size") if outcome.get("size") not in (None, "unknown") else task.get("size")
            self.landed(state, task, outcome["sha"], outcome.get("moved"))
            return outcome
        if task["rev"] != rev or task["state"] not in {"confirmed", "merging"}:
            return outcome  # The task moved on meanwhile.
        if result == "github":
            state["tell"].append(f"PR #{number} ({task['task']}) could not merge yet; GitHub said: {outcome['error'][:200]}. "
                                 f"The next wake tries again.")
            return outcome
        if result in {"waiting", "carried"}:
            if result == "carried":
                task.update(head=outcome["head"], approvedSha=outcome["head"])
                task["rev"] += 1
                task["history"].append({"at": now(), "from": task["state"], "to": task["state"],
                                        "note": f"updated with {config['branch']}; approval carried to {outcome['head'][:12]}"})
            if task["state"] == "confirmed":
                task["merger"] = "harness"
                self.move(task, "merging", "the harness merges it")
            return outcome
        task.pop("merger", None)
        if result == "closed":
            task["blockedFrom"], task["blockedWorker"] = "merging", None
            self.move(task, "blocked", f"PR #{number} was closed without merging")
            state["tell"].append(f"{task['task']} ({task['plan']}) is blocked: PR #{number} was closed without merging. "
                                 f"Reopen it and say restart {task['task']}, or drop it.")
            return outcome
        if result == "ci":
            task["fix"] = {"kind": "ci", "detail": f"CI failed on PR #{number}: {', '.join(outcome['failed'])}."}
        else:
            task["fix"] = update_fix(config, result, number)
            task["head"] = outcome.get("head", task["head"])
        self.move(task, "implementing", f"the harness could not merge it: {result}")
        return outcome

    def merge_wake(self, project):
        """The watcher's check: whether the orchestrator should run `next` for the harness's next merge step. Once for
        a confirmed task, once more when CI on the PR it holds has finished."""
        config, state = self.load(project)
        task = self.harness_task(state) if state["control"] == "active" else None
        if not task:
            return {"send": False}
        key = f"{task['id']}@{task['head']}:{task['state']}"
        if state.get("mergeWoken") == key:
            return {"send": False}
        if task["state"] == "merging":
            names = [*ci_checks(config, state), github.REVIEW_CONTEXT]
            checks = self.hub_for(config["github"]["repo"]).checks(task["head"], names)
            if any(result in {"pending", "missing"} for result in checks.values()):
                return {"send": False}
        return {"send": True, "key": key, "message": (
            f"use t3 workflow: PR #{task['pr']} ({task['task']}) is ready for its next merge step. "
            f"Run t3-workflow '{{\"op\":\"next\"}}' and carry on.")}

    def merge_woken(self, project, key):
        def change(config, state):
            state["mergeWoken"] = key
            return {"key": key}
        return self.transact(project, change, "merge_woken")

    def reset_wake(self, project):
        """The watcher's check: whether the orchestrator should run `next` because the quota some work waits for has
        reset. Once per reset."""
        config, state = self.load(project)
        due = sorted(f"{item}@{until}" for item, until in [
            *((t["id"], t.get("held")) for t in state["tasks"].values() if t["state"] in {"waiting", "review_needed"}),
            *((f"devops:{name}", entry.get("held")) for name, entry in state["devops"].items() if entry["state"] == "none")]
            if until and passed(until))
        key = ",".join(due)
        if state["control"] != "active" or not due or state.get("resetWoken") == key:
            return {"send": False}
        return {"send": True, "key": key, "message": (
            "use t3 workflow: the usage limit some work waits for has reset. Run t3-workflow '{\"op\":\"next\"}' and "
            "carry on.")}

    def reset_woken(self, project, key):
        def change(config, state):
            state["resetWoken"] = key
            return {"key": key}
        return self.transact(project, change, "reset_woken")

    def late_wake(self, project):
        """The watcher's check: whether the orchestrator should run `next` for workers' ends the watcher recorded late,
        which no `next` has acted on yet. Once per set of such ends."""
        config, state = self.load(project)
        due = sorted(f"{w['id']}@{w['ended']['taskId']}" for w in state["workers"].values()
                     if (w.get("ended") or {}).get("late") and not w["ended"].get("seen"))
        key = ",".join(due)
        if state["control"] != "active" or not due or state.get("lateWoken") == key:
            return {"send": False}
        return {"send": True, "key": key, "message": (
            "use t3 workflow: workers finished, and the workflow had missed T3's notices of it; they are recorded now. "
            "Run t3-workflow '{\"op\":\"next\"}' and carry on.")}

    def late_woken(self, project, key):
        def change(config, state):
            state["lateWoken"] = key
            return {"key": key}
        return self.transact(project, change, "late_woken")

    def failed(self, project, worker, args):
        cause = args.get("cause")
        require(cause in {"task", "pipeline"}, "--cause is task (the change broke CI) or pipeline (CI itself broke)")
        detail = text(args.get("detail"), "--detail")

        def change(config, state):
            task = self.held(state, worker)
            require(state.get("ciOn", True), "CI is off for this project, so no merge waits for it: run update, then merge")
            task.pop("devops", None)
            self.reported(state, worker, task)
            if cause == "task":
                task["fix"] = {"kind": "ci", "detail": f"CI failed on PR #{task['pr']}: {detail}"}
                self.move(task, "implementing", "CI failed in the task's code")
                return {"state": "implementing", "next": "It went back to its implementer. End your turn."}
            task["pipeline"] = detail
            self.move(task, "pipeline_wait", f"the pipeline failed: {detail}")
            me = f"--worker {worker['id']} --token {worker['token']}"
            branch = config["branch"]
            return {"state": "pipeline_wait", "next": (
                f"Fix the pipeline in a PR of your own. In your worktree: git fetch origin && git reset --hard "
                f"origin/{branch} (keep the checkout's branch name), make the fix, commit, git push -u origin HEAD, "
                f"gh pr create --base {branch}. Then run {command('worker', 'pipeline', me, '--pr', '<number>')} "
                f"and end your turn.")}
        return self.transact(project, change, "failed")

    def pipeline(self, project, worker, args):
        require(args.get("pr"), "--pr names your fix's pull request")
        config, state = self.load(project)
        require(any(t["state"] == "pipeline_wait" and not t.get("waitingOn") and t["plan"] == worker["plan"]
                    for t in state["tasks"].values()), "No task of your plan waits for a pipeline fix")
        pr, tree, evidence = self.submitted(config, project, f"pipeline-{args['pr']}", args)

        def change(config, state):
            require(not any(t.get("pr") == pr["number"] for t in state["tasks"].values()),
                    f"PR #{pr['number']} is already a task")
            held = [t for t in state["tasks"].values()
                    if t["state"] == "pipeline_wait" and not t.get("waitingOn") and t["plan"] == worker["plan"]]
            require(held, "No task of your plan waits for a pipeline fix")
            task_id = next_id(state, "t")
            task = {"id": task_id, "plan": worker["plan"], "task": f"Fix the pipeline (PR #{pr['number']})",
                    "brief": "DevOps's fix for a CI pipeline failure: " + "; ".join(t["pipeline"] for t in held),
                    "owns": [".github/"], "after": [], "kind": "pipeline", "state": "implementing", "rev": 0,
                    "history": [], "implementer": worker["id"], "pr": pr["number"], "url": pr["url"], "head": pr["head"],
                    "branch": pr["branch"], "files": pr["files"], "worktree": tree, "evidence": evidence,
                    "size": pr_size(pr)}
            state["tasks"][task_id] = task
            for waiting in held:
                waiting["waitingOn"] = task_id
            state["workers"][worker["id"]]["path"] = tree
            self.move(task, "review_needed", f"PR #{pr['number']} at {pr['head'][:12]}")
            return {"task": task_id, "state": "review_needed",
                    "next": "A reviewer reads it; then it merges like any other task. End your turn."}
        return self.transact(project, change, "pipeline")

    def follow(self, tree, branch):
        """Best effort: the implementer's checkout follows its PR after a clean update, so T3 can still clean it up
        (it removes a worktree only at the PR's head)."""
        if not (tree and branch and Path(tree).is_dir()):
            return
        for args in (("fetch", "origin", branch), ("merge", "--ff-only", f"origin/{branch}")):
            code, _, error = git(tree, *args, check=False)
            if code:
                health.failed(self.path.parent, "worktree", error)
                return

    def forward(self, config):
        """Fast-forward the project folder's branch after a merge, when the folder is on it. Returns a line for the
        user when that fails, else None."""
        tree, branch = config["workspace"], config["branch"]
        code, current, _ = git(tree, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        if code or current != branch:
            return None
        for args in (("fetch", "origin", branch), ("merge", "--ff-only", f"origin/{branch}")):
            code, _, error = git(tree, *args, check=False)
            if code:
                health.failed(self.path.parent, "workspace", error)
                return f"The project folder's {branch} did not move forward: {error[:300]}"
        health.ok(self.path.parent, "workspace")
        return None

    def finish_plan(self, state, name):
        plan = state["plans"][name]
        tasks = [t for t in state["tasks"].values() if t["plan"] == name]
        if plan["status"] == "done" or any(t["state"] not in {"merged", "dropped"} for t in tasks):
            return
        plan["status"] = "done"
        merged = sum(1 for t in tasks if t["state"] == "merged")
        dropped = len(tasks) - merged
        state["tell"].append(f"Plan {name} done: {merged} merged{f', {dropped} dropped' if dropped else ''}.")

    def stop_summary(self, state):
        """Once per stop of an active binding: when nothing can move without the user (no worker working or about to
        start, no call pending, every task finished or waiting on the user), one line for them, what waits for them
        first, then how many tasks merged and how many were dropped. Told again only after something moved."""
        tasks = list(state["tasks"].values())
        unfinished = [t for t in tasks if t["state"] not in {"merged", "dropped"}]
        if (state["control"] != "active" or not tasks or state["calls"]
                or any(w["state"] in {"working", "prepared"} for w in state["workers"].values())
                or any(t["state"] not in {"asking", "blocked", "waiting", "pipeline_wait"} or t.get("held")
                       for t in unfinished)):
            state.pop("stopped", None)
            return None
        asks = sorted((a for a in state["asks"].values() if not a.get("answered")), key=lambda a: int(a["id"][1:]))
        key = json.dumps([sorted([t["id"], t["state"]] for t in tasks), [a["id"] for a in asks]])
        if state.get("stopped") == key:
            return None
        state["stopped"] = key
        merged = sum(1 for t in tasks if t["state"] == "merged")
        counts = f"{merged} merged, {len(tasks) - merged - len(unfinished)} dropped"
        if not unfinished:
            return f"Summary: the queue is finished: {counts}."
        waiting = [f"{a['id']} ({ask_words(state, a)})" for a in asks]
        asked = {a["task"] for a in asks}
        for task in unfinished:
            if task["state"] == "blocked" and task["id"] not in asked:
                why = next((h.get("note") for h in reversed(task["history"]) if h["to"] == "blocked"), None)
                waiting.append(f"{task['task']} ({task['plan']}) is blocked" + (f": {why}" if why else ""))
            elif task["state"] == "pipeline_wait" and not task.get("waitingOn"):
                waiting.append(f"{task['task']} ({task['plan']}) waits for a pipeline fix DevOps has not opened")
        return (f"Summary: nothing more can move without you. Waiting for you: {'; '.join(waiting) or 'nothing named'}. "
                f"So far {counts}.")


def ask_words(state, ask):
    """A few words for an open question in the stop summary."""
    task = state["tasks"][ask["task"]]
    if ask["kind"] == "outside":
        return f"PR #{task.get('pr')} of {task['task']}: merge, back or drop"
    if ask["kind"] == "rounds":
        return f"{task['task']} after {REVIEW_ROUNDS} review rounds: carry on, restart or drop"
    words = ask["question"].split(" asks: ", 1)[-1]
    return f"{task['task']} asks: " + (words if len(words) <= 80 else words[:77].rstrip() + "...")


DECISIONS = re.compile(r"^[ \t]*(#{1,6}[ \t]*)?(\*\*)?Decisions(?:\*\*)?[ \t]*(:)?(?:\*\*)?[ \t]*(.*)$",
                       re.IGNORECASE | re.MULTILINE)


def has_decisions(body):
    """Whether a PR's body has a Decisions section with something in it: a line that is a "Decisions" heading (or
    "Decisions:"), with text after it on that line or on the next line that is not blank and not another heading."""
    body = (body or "").replace("\r\n", "\n")
    for found in DECISIONS.finditer(body):
        marked, rest = any(found.group(i) for i in (1, 2, 3)), found.group(4).strip()
        if rest:
            if marked:
                return True
            continue  # A sentence that starts with the word.
        after = next((line.strip() for line in body[found.end():].split("\n") if line.strip()), "")
        if after and not after.startswith("#"):
            return True
    return False


def named_paths(words):
    """The file paths a worker's words name: a word with a slash or a file extension."""
    found = re.findall(r"[\w.\-/]+", words.replace("\\", "/"))
    return [w.strip("./") or w for w in found if "/" in w.strip("/") or re.search(r"\w\.\w+$", w)]


def unlinked_lines(task, implementer):
    """Why the task starts without waiting for work it once waited for."""
    lines = []
    for u in task.get("unlinked") or []:
        what = f"plan {u['plan']}" if u.get("plan") else u["name"]
        if implementer:
            lines.append(f"This task once waited for {what}; it starts early because: {u['why']} Check that first.")
        elif u.get("authorization"):
            lines.append(f"The user let this task go on without {what}: {u['why']}")
        else:
            lines.append(f"This task started without waiting for {what}: {u['why']} If the PR stubs, fakes or copies "
                         f"code that {what} builds, request changes and name it.")
    return lines


def waits_line(state, task, me):
    """How an implementer waits for another open task's code, with the tasks it could name."""
    others = sorted((t for t in state["tasks"].values() if t["id"] != task["id"] and t["state"] not in {"merged", "dropped"}),
                    key=lambda t: t["plan"] != task["plan"])  # Its own plan first.
    if not others:
        return None
    listed = ", ".join(f"{t['id']} {t['task']} ({t['plan']})" for t in others[:20])
    more = f", and {len(others) - 20} more" if len(others) > 20 else ""
    return (f"If your work needs code another open task is still building, do not stub, fake or copy it: commit and push "
            f"what you have, run {command('worker', 'waits', me, '--on', TASK, '--why', NEED)} and end your turn. When "
            f"that task merges, a follow-up wakes you here to carry on. Open tasks: {listed}{more}.")


TASK, NEED = '"<task>"', '"<the file you need and what in it>"'


def implementer_brief(config, state, task, worker):
    branch = config["branch"]
    me = f"--worker {worker['id']} --token {worker['token']}"
    if task.get("pr"):  # A restart: the work so far is on the PR's branch, and why it came back, if it did.
        came_back = (f" It came back because: {task['returned']} Read its comments (gh pr view {task['pr']} --comments) "
                     "and make sure that is done." if task.get("returned") else "")
        start = (f"PR #{task['pr']} already holds this task's work on branch {task['branch']}. Continue it: "
                 f"git fetch origin {task['branch']} && git reset --hard origin/{task['branch']}.{came_back}",
                 f"When the work is done: commit and git push origin HEAD:{task['branch']}.")
    else:
        start = (f"Your checkout is new. Start with: git fetch origin {branch} && git reset --hard origin/{branch}",
                 f"When the work is done: commit, git push -u origin HEAD, then gh pr create --base {branch} "
                 f"--title \"{task['plan']}: {task['task']}\" with a body saying what changed, why, and how you checked it, "
                 f"and a Decisions section.")
    return "\n".join([
        f"You are the implementer of one task in the t3 workflow. First read {slash(ROOT / 'references' / 'implementer.md')}.",
        f"Plan: {task['plan']}. Task: {task['task']}.",
        f"Brief: {task['brief'] or task['task']}",
        f"Paths you may change: {', '.join(task['owns'])}.",
        *unlinked_lines(task, implementer=True),
        start[0],
        f"Keep this checkout's branch name. Never push to {branch}, never force-push, never merge.",
        start[1],
        "The PR's body ends with a \"## Decisions\" section: each call this brief left open, what you chose and why, "
        "or \"None\" when there were none. worker pr refuses a PR without one.",
        f"Then run: {command('worker', 'pr', me, '--pr', str(task['pr']) if task.get('pr') else '<number>')}",
        "It runs the project's checks in this folder. If it refuses, fix what it names, push, and run it again. "
        "When it succeeds, end your turn.",
        *filter(None, [waits_line(state, task, me)]),
        f"If you cannot go on for another reason, run {command('worker', 'stuck', me, '--why', QUESTION)} and end your turn.",
    ])


QUESTION = '"<your question>"'


def worker_label(state, worker):
    """("w3, the implementer of Fix the login typo (M30)", its task or None) for the user's eyes."""
    if worker["role"] == "devops":
        task = next((t for t in state["tasks"].values() if t["state"] == "merging" and t.get("devops") == worker["id"]), None)
    else:
        task = state["tasks"].get(worker["task"])
    role = {"devops": "DevOps worker"}.get(worker["role"], worker["role"])
    return f"{worker['id']}, the {role} of " + (f"{task['task']} ({task['plan']})" if task else f"plan {worker['plan']}"), task


WHY_MODEL = {"pick": "your choice", "default": "the default", "jev": "Jev's choice",
             "fallback": "the fallback, as Jev gave no answer",
             "scores": "the best-scored model that could start, as Jev gave no answer"}
PAID = "paid through OpenRouter, as the subscription models are at their limits"


def started_line(state, worker):
    """The user's line for a worker whose start T3 confirmed: its role, its task, its model (and its route when it is
    not the model's first) and effort, and why that model. A stand-in names only the model it stands in for: the
    usage-limit line already said the rest. A paid route says so, unless the user picked the model."""
    role = {"implementer": "Implementer", "reviewer": "Reviewer", "devops": "DevOps"}[worker["role"]]
    task = state["tasks"].get(worker.get("task"))
    what = f"{task['task']} ({task['plan']})" if task else f"plan {worker['plan']}"
    why = WHY_MODEL.get(worker.get("modelSource"))
    if worker.get("modelSource") == "limit":
        why = f"a stand-in for {worker.get('standsInFor') or 'a model at its usage limit'}"
    if worker.get("modelSource") == "family":
        why = f"a different family than the {worker['otherRole']}'s {worker['otherThan']}"
    if worker.get("paid") and worker.get("modelSource") != "pick":
        why = f"{why}; {PAID}" if why else PAID
    model = worker["model"] + (f" through {worker['via']}" if worker.get("via") else "")
    return f"{role} for {what} started on {model} at {worker['effort']}" + (f" ({why})." if why else ".")


def closing(worker, task):
    """The command a worker's job ends with, for a follow-up asking it to finish."""
    me = f"--worker {worker['id']} --token {worker['token']}"
    if worker["role"] == "implementer":
        return (command("worker", "pr", me, "--pr", str(task["pr"]) if task.get("pr") else "<number>")
                + f" (or, if you need code another open task is still building: "
                  f"{command('worker', 'waits', me, '--on', TASK, '--why', NEED)}; "
                  f"if you cannot go on for another reason: {command('worker', 'stuck', me, '--why', QUESTION)})")
    if worker["role"] == "reviewer":
        return command("worker", "review", me, "--verdict", "approve|changes", "--sha", task["head"])
    return (f"{command('worker', 'merge', me)} once CI passes on PR #{task['pr']}, or "
            f"{command('worker', 'failed', me, '--cause', 'task|pipeline', '--detail', QUESTION.replace('question', 'what failed'))}")


def owned(path, owns):
    return any(o in {".", "./", "*"} or path == o.rstrip("/") or path.startswith(o.rstrip("/") + "/") for o in owns)


def devops_brief(config, plan, worker):
    me = f"--worker {worker['id']} --token {worker['token']}"
    return "\n".join([
        f"You are the DevOps worker for plan {plan} of {config['github']['repo']} in the t3 workflow. "
        f"First read {slash(ROOT / 'references' / 'devops.md')}.",
        f"You merge confirmed pull requests into {config['branch']}, one per turn, and you own the CI workflows and repo settings.",
        f"Start with: {command('worker', 'take', me)}",
        "It gives you one job, or tells you to end your turn. Follow devops.md for the job, then end your turn.",
        f"Your other commands: {command('worker', '<update|merge|failed|pipeline>', me, '...')}",
    ])


def wait_steps(config, state, task):
    if not state.get("ciOn", True):
        return "CI is off for this project, so nothing waits for it: run merge."
    return (f"Wait for CI: gh pr checks {task['pr']} --repo {config['github']['repo']} --watch --required. "
            f"When it passes, run merge. If a check fails, follow devops.md: rerun a flaky one once, else run failed.")


def devops_steps(config, task):
    if task["job"] == "triage":
        return (f"CI failed on PR #{task['pr']}: {', '.join(task.get('ciFailed') or [])}. Read the failed run "
                f"(gh pr checks {task['pr']} --repo {config['github']['repo']}; gh run view <id> --log-failed) and "
                f"follow devops.md: a flaky test gets one rerun and then merge; otherwise run failed with the cause.")
    return f"Run update, then follow its next step."


def evidence_lines(task):
    evidence = task.get("evidence") or {}
    if not evidence.get("results"):
        return [f"No local checks: {evidence.get('noChecksReason')}"]
    return [f"{r['name']}: {'passed' if r['passed'] else 'failed'} (log {slash(r['log'])})" for r in evidence["results"]]


VERDICTS = {"approve": "approved", "changes": "changes requested"}


def reviewed_before(config, task, tree):
    """For a PR reviewed before: what each review said, why the PR came back, and where the next review starts."""
    reviews = task.get("reviews") or []
    if not reviews:
        return []
    said = ", then ".join(f"{VERDICTS[r['verdict']]} on {r['sha'][:12]}" for r in reviews)
    return [f"This PR was reviewed before: {said}.",
            *([f"It came back because: {task['returned']}"] if task.get("returned") else []),
            f"Start there: read the earlier review comments (gh pr view {task['pr']} --repo {config['github']['repo']} "
            f"--comments) and check that each blocking point is fixed. Then review the whole change as usual.",
            f"What changed since the last review (after an update, main's changes too): "
            f"git -C {tree} diff {reviews[-1]['sha']} {task['head']}"]


def reviewer_brief(config, task, worker):
    me = f"--worker {worker['id']} --token {worker['token']}"
    tree, head, branch = slash(task["worktree"]), task["head"], config["branch"]
    return "\n".join([
        f"You are the reviewer of one task in the t3 workflow. First read {slash(ROOT / 'references' / 'reviewer.md')}.",
        f"Plan: {task['plan']}. Task: {task['task']}.",
        f"Brief: {task['brief'] or task['task']}",
        f"Paths it may change: {', '.join(task['owns'])}.",
        *[f"This task waited for {n['name']} ({n['why']}). If the PR stubs, fakes or copies code that {n['name']} "
          f"builds, request changes and name it." for n in task.get("needed") or []
          if not any(u.get("task") == n["task"] and u.get("authorization") for u in task.get("unlinked") or [])],
        *unlinked_lines(task, implementer=False),
        f"Review PR #{task['pr']} ({task.get('url')}) at commit {head}, branch {task['branch']}.",
        f"The implementer's checkout is {tree}. Read and test there; change nothing in it: no edits, commits or pushes.",
        f"The change: git -C {tree} diff origin/{branch}...{head}",
        *reviewed_before(config, task, tree),
        f"Check each decision in the PR body's Decisions section (gh pr view {task['pr']} --repo "
        f"{config['github']['repo']}) against the brief. One that changes what the task asks for, or that the user "
        f"should have made, is a blocking finding.",
        f"Where the project can be run (its README or instructions say how), run it in {tree} and try what the task "
        f"changed, as a user would. Something that fails when tried is a blocking finding. In your comment, say what "
        f"you tried and what happened, or why you could not run it. Leave no files behind.",
        "Local checks at that commit:", *[f"- {line}" for line in evidence_lines(task)],
        f"Post your findings as one comment: gh pr comment {task['pr']} --repo {config['github']['repo']} --body \"...\"",
        f"Then run {command('worker', 'review', me, '--verdict', 'approve', '--sha', head)} "
        f"or the same with --verdict changes, and end your turn.",
    ])
