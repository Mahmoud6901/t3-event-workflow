"""The delegated PR workflow: plans, tasks, workers and every transition between them. Standard library only.

references/protocol.md lists every operation. The orchestrator runs `next` on every wake and
makes the T3 calls it returns; workers report through `harness.py worker <command>`. GitHub goes through github.py,
and every write is one transaction on the harness database (store.py).
"""
import copy
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys

import github
import health
import jev
import places
import store
import watcher

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
DEFAULT_DB = places.STATE / "harness.sqlite3"
GOOSE = "acpRegistry_goose"
T3_CACHES = Path.home() / ".t3" / "caches"  # T3's list of what each provider can run, one file per provider.
PROVIDERS = {"claudeAgent": ("Claude", "effort"), "codex": ("Codex", "reasoningEffort"), GOOSE: ("goose", "thinking_effort")}
MATCHES = 20  # Models one search shows.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROLES = ("implementer", "reviewer", "devops")
MODELS = {
    "claude-haiku-5-5": {"provider": "claudeAgent", "tier": 1, "roles": ["implementer", "reviewer"]},
    "gpt-6-luna": {"provider": "codex", "tier": 1, "roles": ["implementer", "reviewer"]},
    "gpt-6.1-sol": {"provider": "codex", "tier": 2, "roles": ["implementer"]},
    "claude-sonnet-5-5": {"provider": "claudeAgent", "tier": 2, "roles": ["reviewer"]},
    "gpt-6-astra": {"provider": "codex", "tier": 3, "roles": ["implementer"]},
    "claude-opus-5-5": {"provider": "claudeAgent", "tier": 3, "roles": ["reviewer", "devops"]},
    # OpenRouter models run through T3's goose provider instance. None of these four declares thinking_effort in T3.
    "deepseek/deepseek-v4.1-flash": {"provider": GOOSE, "tier": 1, "roles": ["implementer", "reviewer"]},
    "google/gemini-4-argon": {"provider": GOOSE, "tier": 1, "roles": ["implementer", "reviewer"]},
    "qwen/qwen3.8-flash": {"provider": GOOSE, "tier": 1, "roles": ["implementer", "reviewer"]},
    "moonshotai/kimi-k3.1-pro": {"provider": GOOSE, "tier": 1, "roles": ["implementer", "reviewer"]},
}
DEFAULTS = {"implementer": "gpt-6.1-sol", "implementerEffort": "xhigh", "reviewer": "claude-sonnet-5-5",
            "reviewerEffort": "xhigh", "devops": "claude-opus-5-5", "devopsEffort": "xhigh"}
QUIET_MINUTES = 30
NUDGES = 1  # Follow-ups asking a worker that ended without its closing command to finish; then the task is blocked.
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
    `thinking_effort` only when it declares one (its entry says "thinking": true), and that option has no xhigh."""
    item = item or config["models"][model_id]
    options = {"claudeAgent": {"effort": effort},
               GOOSE: {"provider": "openrouter"}}.get(
        item["provider"], {"reasoningEffort": effort, "serviceTier": "default"})
    if item["provider"] == GOOSE and item.get("thinking"):
        options["thinking_effort"] = sent(GOOSE, effort)
    return {"providerInstanceId": item["provider"], "model": model_id, "options": options}


def sent(provider, effort):
    """The effort as the provider takes it: goose's thinking_effort has no xhigh, so xhigh goes as high."""
    return "high" if provider == GOOSE and effort == "xhigh" else effort


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
    config.setdefault("models", copy.deepcopy(MODELS))
    config["defaults"] = {**DEFAULTS, **(config.get("defaults") or {})}
    for model_id, item in config["models"].items():
        require(item.get("provider") in {"codex", "claudeAgent", GOOSE}, f"{model_id}: provider is codex, claudeAgent or goose")
        require(set(item.get("roles") or []) <= set(ROLES), f"{model_id}: roles are implementer, reviewer and devops")
    for role in ROLES:
        model = config["defaults"][role]
        require(role in (config["models"].get(model) or {}).get("roles", []), f"defaults.{role} must be an approved {role} model")
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


def devops_on(state, name):
    """A plan keeps the DevOps mode it started with; one not started yet follows the binding's current mode."""
    plan = state["plans"][name]
    return plan.get("devops", True) if plan["started"] else state.get("devopsOn", True)


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
    """What a new worker in this role starts on: the user's model and effort where set, else the defaults. For an
    implementer or reviewer, Jev may advise another model, and an effort the user did not set."""
    pick = (state.get("picks") or {}).get(role) or {}
    return {"model": pick.get("model") or config["defaults"][role], "modelSource": "pick" if pick.get("model") else "default",
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


def default_jev(config, context):
    """Jev's valid implementer and reviewer model and effort for one task; the rest fall back to the defaults.
    Raises when Jev gives no answer, so `next` records the problem and uses the defaults."""
    decision = jev.evaluate(config, context, "task")
    if decision["status"] != "decided":
        raise RuntimeError(f"Jev gave no advice: {decision['reason']}")
    return {**decision["selectedModels"], **{r + "Effort": e for r, e in decision["selectedEfforts"].items()}}


def new_state():
    return {"version": 0, "control": "paused", "plans": {}, "tasks": {}, "workers": {}, "devops": {},
            "calls": {}, "asks": {}, "tell": [], "seq": 0}


def next_id(state, prefix):
    state["seq"] += 1
    return f"{prefix}{state['seq']}"


class Flow:
    def __init__(self, db_path=DEFAULT_DB, hub=None, jev=None):
        self.path = Path(db_path).resolve()
        self.hub_for = hub or github.GitHub
        self.jev = jev or default_jev
        self.db = store.connect(self.path)

    def close(self):
        self.db.close()

    # Storage

    def load(self, project):
        row = self.db.execute("SELECT config, state FROM projects WHERE id=?", (project,)).fetchone()
        require(row, f"Project {project} has no binding")
        return json.loads(row[0]), json.loads(row[1])

    def transact(self, project, change, op):
        """change(config, state) runs inside one write transaction; the state it leaves is saved with the next version."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            config, state = self.load(project)
            result = change(config, state)
            state["version"] += 1
            self.db.execute("UPDATE projects SET state=? WHERE id=?", (store.packed(state), project))
            self.db.execute("INSERT INTO audit(project,operation,version,detail) VALUES (?,?,?,?)",
                            (project, op, state["version"], store.packed(result if isinstance(result, dict) else {})))
            self.db.commit()
            return result
        finally:
            if self.db.in_transaction:
                self.db.rollback()

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
                   "devops": self.devops}.get(op)
        require(handler, f"Unknown operation {op}")
        return handler(project, req)

    def init(self, project, thread, req):
        config = validate_config({"projectId": project, "orchestrator": thread, "workspace": req.get("workspace"),
                                  "branch": req.get("branch"), "github": {"repo": req.get("repo"),
                                                                          "requiredChecks": req.get("requiredChecks")},
                                  **{k: req[k] for k in ("checks", "noChecksReason", "defaults", "models", "quietMinutes")
                                     if req.get(k) is not None}})
        require(not self.path.is_relative_to(Path(config["workspace"])), "The harness database must live outside the project")
        picks = req.get("picks") or {}
        require(isinstance(picks, dict), "picks maps a role to its model")
        devops = req.get("devops", True)
        require(isinstance(devops, bool), "devops is true (a DevOps worker merges) or false (the harness merges)")
        lists, why = t3_lists()
        state = new_state()
        state["picks"] = {role: pick for role, raw in picks.items()
                          if (pick := settle_pick(role, {}, raw, config["defaults"], lists))}
        state["devopsOn"] = devops
        if lists is not None:  # Every role must start on something this T3 runs, and Jev advises only among those.
            for role in ROLES:
                model = config["defaults"][role]
                provider = config["models"][model]["provider"]
                require(state["picks"].get(role, {}).get("model") or runs(lists, model, provider),
                        f"T3 cannot run {model}, the {role}'s default ({why.get(provider) or 'T3 does not list it'}). "
                        f"Pick a model for the {role}; the models operation lists what T3 runs.")
            config["models"] = {m: item for m, item in config["models"].items() if runs(lists, m, item["provider"])}
        with self.db:
            exists = self.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone()
            require(not exists, f"Project {project} is already bound")
            self.db.execute("INSERT INTO projects VALUES (?,?,?)", (project, store.packed(config), store.packed(state)))
        return {"projectId": project, "control": "paused", "repo": config["github"]["repo"]}

    def models(self, req):
        """For setup and model changes: which providers T3 can run, whether it runs each role's default, and the models
        whose names hold every word searched for."""
        lists, why = t3_lists()
        defaults = {role: {"model": DEFAULTS[role], "effort": DEFAULTS[role + "Effort"],
                           "runs": runs(lists, DEFAULTS[role], MODELS[DEFAULTS[role]]["provider"])} for role in ROLES}
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
                model = config["defaults"][role]
                require(model in config["models"] and runs(lists, model, config["models"][model]["provider"]) is not False,
                        f"T3 cannot run {model}, the {role}'s default; pick another model")
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
        """Change a running queue: new plans, new tasks for a plan (a done plan opens again), or drop a task."""
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
            require(result, "queue_edit takes plans, tasks with plan, or drop")
            return result
        return self.transact(project, change, "queue_edit")

    def setup_github(self, project, req):
        """Squash-only merges and main's protection on the bound repo, on your word. Reports what it changed."""
        text(req.get("authorization"), "authorization (the user's own words)")
        config, _ = self.load(project)
        changed = self.hub_for(config["github"]["repo"]).setup(config["branch"], config["github"]["requiredChecks"])
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
        ready = {t["id"] for t in state["tasks"].values() if t["state"] == "waiting" and self.unblocked(state, t)}
        choices, facts = {}, {}
        if state["control"] == "active":
            gh = self.hub_for(config["github"]["repo"])
            for task in state["tasks"].values():
                if task["state"] == "approved":
                    try:
                        pr = gh.pull(task["pr"])
                        facts[task["id"]] = (task["rev"], pr, gh.checks(pr["head"], config["github"]["requiredChecks"]))
                        health.ok(self.path.parent, "github")
                    except github.Failed as error:  # The rule waits for the next wake.
                        health.failed(self.path.parent, "github", error)
            picks = state.get("picks") or {}  # Jev advises only roles whose model the user left on default.
            advised = {role for role in jev.ROLES if not (picks.get(role) or {}).get("model")}
            for task_id in sorted(ready) if advised else ():
                task = state["tasks"][task_id]
                try:
                    choices[task_id] = self.jev(config, {"request": task["task"], "brief": task["brief"], "plan": task["plan"]}) or {}
                    health.ok(self.path.parent, "jev")
                except Exception as error:  # noqa: BLE001 - Jev is advice; the defaults stand in
                    health.failed(self.path.parent, "jev", error)
                    choices[task_id] = {}

        def change(config, state):
            for worker in state["workers"].values():
                if worker.get("ended") and not worker["ended"].get("seen"):
                    worker["ended"]["seen"] = True
                    self.settle(state, worker)
            if state["control"] == "active":
                for task in state["tasks"].values():
                    if task["state"] == "waiting" and self.unblocked(state, task):
                        self.start_implementer(config, state, task, choices.get(task["id"]))
                    elif task["state"] == "review_needed":
                        self.start_reviewer(config, state, task)
                    elif task["state"] == "approved" and task["id"] in facts and facts[task["id"]][0] == task["rev"]:
                        self.confirm(state, task, *facts[task["id"]][1:])
                    elif task["state"] == "implementing" and task.get("fix"):
                        self.send_fix(config, state, task)
                self.call_devops(config, state)
            calls = list(state["calls"].values())
            told, state["tell"] = state["tell"], []
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
            recorded, unknown = [], []
            for item in results:
                call = state["calls"].pop(item.get("call"), None) if isinstance(item, dict) else None
                if not call:
                    unknown.append(item.get("call") if isinstance(item, dict) else item)
                    continue
                worker = state["workers"][call["worker"]]
                if call["tool"] == "delegate_task":
                    worker["childThreadId"] = text(item.get("childThreadId"), "childThreadId")
                    worker["state"] = "working"
                if call["tool"] in {"delegate_task", "t3_thread_send"} and item.get("taskId"):
                    worker["taskIds"].append(item["taskId"])
                    worker["state"] = "working" if worker["state"] != "cancelled" else "cancelled"
                    early = [e for e in state.get("unmatched", []) if e["taskId"] == item["taskId"]]
                    worker["ended"] = {**early[0], "seen": False} if early else None
                    state["unmatched"] = [e for e in state.get("unmatched", []) if e["taskId"] != item["taskId"]]
                if call["tool"] == "task_cancel":
                    worker["state"] = "cancelled"
                recorded.append(call["call"])
            return {"recorded": recorded, **({"unknown": unknown} if unknown else {})}
        return self.transact(project, change, "done")

    def ended(self, project, req):
        """A worker's T3 run ended (the hook reads it from the "Delegated task …" wake). `next` acts on it. An end
        that arrives before `done` recorded its run waits for it."""
        run, status = text(req.get("taskId"), "taskId"), req.get("status") or "completed"

        def change(config, state):
            for worker in state["workers"].values():
                if run in worker["taskIds"]:
                    # An older run's end means nothing now, and an end already recorded is not news.
                    new = worker["taskIds"][-1] == run and (worker.get("ended") or {}).get("taskId") != run
                    if new:
                        worker["ended"] = {"taskId": run, "status": status, "at": now(), "seen": False}
                    return {"worker": worker["id"], "new": new}
            new = not any(e["taskId"] == run for e in state.setdefault("unmatched", []))
            if new:
                state["unmatched"].append({"taskId": run, "status": status, "at": now()})
            return {"worker": None, "new": new}
        return self.transact(project, change, "ended")

    def restart(self, project, req):
        """On your word: the task's current worker is cancelled and a new one in the same role takes over."""
        text(req.get("authorization"), "authorization (the user's own words)")
        name = text(req.get("task"), "task")

        def change(config, state):
            task = state["tasks"][self.task_id(state, name)]
            where = task.get("blockedFrom") if task["state"] == "blocked" else task["state"]
            role = {"implementing": "implementer", "reviewing": "reviewer", "merging": "devops"}.get(where)
            require(role, f"{task['id']} is {task['state']}; no worker of it can be restarted")
            old = state["workers"].get(task.get("blockedWorker") or task.get(role))
            if old:
                self.retire(state, old, "restarted on the user's word")
            for key in ("blockedFrom", "blockedWorker", "fix"):
                task.pop(key, None)
            for ask in state["asks"].values():
                if ask["task"] == task["id"] and not ask.get("answered"):
                    ask["answered"] = "restarted"
            if role == "implementer":
                self.start_implementer(config, state, task, task.get("choice"))
            elif role == "reviewer":
                self.start_reviewer(config, state, task)
            else:
                state["devops"][task["plan"]] = {"worker": None, "state": "none"}
                task.pop("devops", None)
                self.move(task, "ci_failed" if task.get("job") == "triage" else "confirmed", "DevOps restarted")
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

    def settle(self, state, worker):
        """A worker's run ended. One that owes nothing rests (an idle DevOps waits for a follow-up). One that owes its
        closing command is asked once to finish; the next time, its task is blocked and you are told."""
        if worker["state"] == "cancelled":
            return
        worker["state"] = "idle"
        if worker["role"] == "devops":
            task = next((t for t in state["tasks"].values()
                         if t["state"] == "merging" and t.get("devops") == worker["id"]), None)
            if task is None:
                entry = state["devops"].get(worker["plan"]) or {}
                if entry.get("worker") == worker["id"]:
                    entry["state"] = "idle"
                return
        else:
            task = state["tasks"].get(worker["task"])
            owes = {"implementer": "implementing", "reviewer": "reviewing"}[worker["role"]]
            if not task or task.get(worker["role"]) != worker["id"] or task["state"] != owes or task.get("fix"):
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

    def answer(self, project, req):
        """Your answer to a question from `ask`: merge, back or drop for a PR outside its paths; any words for a
        stuck implementer, which get sent to it."""
        ask_id, reply = text(req.get("ask"), "ask"), text(req.get("answer"), "answer")

        def change(config, state):
            ask = state["asks"].get(ask_id)
            require(ask and not ask.get("answered"), f"No open question {ask_id}")
            task = state["tasks"][ask["task"]]
            if ask["kind"] == "outside":
                require(reply in ask["options"], f"Answer {ask_id} with {', '.join(ask['options'])}")
                require(task["state"] == "asking", f"{task['id']} is no longer waiting for this answer")
                if reply == "merge":
                    self.move(task, "confirmed", "you said merge")
                elif reply == "back":
                    task["fix"] = {"kind": "note", "detail": (
                        f"PR #{task['pr']} may change only {', '.join(task['owns'])}. "
                        f"Take out the changes to {', '.join(ask['outside'])}.")}
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
        """The watcher saw this working worker quiet since `since`, its latest activity in T3. A new quiet spell is told
        to the user once. Returns whether the orchestrator still needs the watcher's message for it, and the message."""
        def change(config, state):
            worker = state["workers"].get(worker_id)
            if not worker or worker["state"] != "working":
                return {"send": False}
            label, task = worker_label(state, worker)
            if (worker.get("quiet") or {}).get("since") != since:
                worker["quiet"] = {"since": since, "sent": False}
                advice = f" Say restart {task['task']} to replace it." if task else ""
                state["tell"].append(f"{label} has been quiet since {since} ({minutes} minutes or more).{advice}")
            return {"send": not worker["quiet"]["sent"], "message": (
                f"use t3 workflow: {label} has been quiet for {minutes} minutes. "
                f"Run t3-workflow '{{\"op\":\"next\"}}' and tell the user.")}
        return self.transact(project, change, "quiet")

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
                "tasks": [{k: t.get(k) for k in ("id", "plan", "task", "state", "pr", "head")} for t in state["tasks"].values()],
                "working": sorted(w["id"] for w in state["workers"].values() if w["state"] == "working"),
                "models": {role: role_model(config, state, role) for role in ROLES}, "devops": state.get("devopsOn", True),
                "pendingCalls": len(state["calls"]), "openAsks": [a for a in state["asks"].values() if not a.get("answered")],
                "problems": health.problems(self.path.parent, scanning=watcher.running(self.path.parent))}

    def unblocked(self, state, task):
        plan = state["plans"][task["plan"]]
        return (all(state["plans"][p]["status"] == "done" for p in plan["dependsOn"])
                and all(state["tasks"][a]["state"] in {"merged", "dropped"} for a in task["after"]))

    def move(self, task, to, note=None):
        task["history"].append({"at": now(), "from": task["state"], "to": to, **({"note": note} if note else {})})
        task["state"] = to
        task["rev"] += 1

    def add_worker(self, config, state, role, plan, task_id, choice=None):
        """Model and effort each come from the user's setting for the role; else Jev's advice (when the user set no model,
        and it names an approved one); else the default. The worker keeps its model entry, so a later change never
        reaches a worker already started."""
        pick = (state.get("picks") or {}).get(role) or {}
        choice = {} if pick.get("model") else (choice or {})
        model = choice.get(role) if role in (config["models"].get(choice.get(role)) or {}).get("roles", []) else None
        effort = choice.get(role + "Effort") if choice.get(role + "Effort") in EFFORTS else None
        worker = {"id": next_id(state, "w"), "token": secrets.token_hex(16), "role": role, "plan": plan, "task": task_id,
                  "model": model or config["defaults"][role],
                  "effort": pick.get("effort") or effort or config["defaults"][role + "Effort"],
                  "modelSource": "jev" if model else "default", "childThreadId": None, "taskIds": [], "state": "prepared",
                  "job": None, "path": None}
        if pick.get("model"):
            worker.update(model=pick["model"], modelSource="pick",
                          entry={"provider": pick.get("provider", GOOSE), "roles": [role], "thinking": pick.get("thinking", False)})
        state["workers"][worker["id"]] = worker
        return worker

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
                      implementer_brief(config, task, worker))

    def start_reviewer(self, config, state, task):
        worker = self.add_worker(config, state, "reviewer", task["plan"], task["id"], task.get("choice"))
        worker["job"] = {"kind": "review", "reported": False, "nudged": 0}
        task["reviewer"] = worker["id"]
        self.move(task, "reviewing")
        self.delegate(config, state, worker, f"Review {task['plan']}: {task['task']}", "review", "shared",
                      reviewer_brief(config, task, worker))

    def confirm(self, state, task, pr, checks):
        """The orchestrator's rule: the reviewed commit is the head, CI has not failed, and only the task's paths changed."""
        if pr["head"] != task["approvedSha"]:
            task["fix"] = {"kind": "moved", "detail": f"PR #{task['pr']} moved after its approval."}
            self.move(task, "implementing", "head moved after approval")
            return
        outside = [f for f in pr["files"] if not owned(f, task["owns"])]
        if outside:
            ask_id = next_id(state, "a")
            state["asks"][ask_id] = {"id": ask_id, "task": task["id"], "kind": "outside", "outside": outside, "question": (
                f"PR #{task['pr']} ({task['task']}) changes files outside {', '.join(task['owns'])}: {', '.join(outside[:10])}"
                f"{' and more' if len(outside) > 10 else ''}. Merge it anyway, send it back, or drop it?"),
                "options": ["merge", "back", "drop"]}
            self.move(task, "asking", "files outside the task's paths")
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
        task["returned"] = fix["detail"]  # The next reviewer is told why the PR came back.
        worker["job"] = {"kind": "fix", "reported": False, "nudged": 0}
        me = f"--worker {worker['id']} --token {worker['token']}"
        guide = {"review": f"Read the reviewer's comment (gh pr view {task.get('pr')} --comments) and address it.",
                 "conflict": f"Run git fetch origin && git merge origin/{config['branch']}, resolve the conflicts, commit.",
                 "ci": f"Read the failing CI run (gh pr checks {task.get('pr')}; gh run view <id> --log-failed) and fix it.",
                 "moved": "Make sure this checkout is at the PR's head."}.get(fix["kind"], "")
        if fix["kind"] == "ci" and not devops_on(state, task["plan"]):
            guide += (" If CI itself is broken, not this task's code, run "
                      + command("worker", "stuck", me, "--why", '"<what is broken>"') + " so the user decides, and end your turn.")
        number = str(task["pr"]) if task.get("pr") else "<number>"
        self.send(state, worker, "fix", "\n".join([line for line in [
            fix["detail"], guide, "Then push (opening the PR as your brief says if there is none yet), and run: "
            + command("worker", "pr", me, "--pr", number), "When it succeeds, end your turn."] if line]))

    def call_devops(self, config, state):
        """One DevOps worker per plan with work for it: started the first time, woken by a follow-up when idle.
        While a merge runs nobody is called: take would give them nothing."""
        if any(t["state"] == "merging" for t in state["tasks"].values()):
            return
        for name, plan in state["plans"].items():
            if not devops_on(state, name) or not any(
                    t["plan"] == name and t["state"] in {"confirmed", "ci_failed"} for t in state["tasks"].values()):
                continue
            entry = state["devops"].setdefault(name, {"worker": None, "state": "none"})
            if entry["state"] == "none":
                worker = self.add_worker(config, state, "devops", name, None)
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
        project, worker = self.find_worker(args["worker"])
        require(hmac.compare_digest(worker["token"], args["token"]), "This worker's token does not match")
        handler = {"pr": self.pr, "stuck": self.stuck, "review": self.review, "take": self.take, "update": self.update,
                   "merge": self.merge, "failed": self.failed, "pipeline": self.pipeline}.get(command)
        require(handler, f"{command} is not available yet")
        if command in {"take", "update", "merge", "failed", "pipeline"}:
            require(worker["role"] == "devops", f"Only DevOps runs {command}")
        return handler(project, worker, args)

    def find_worker(self, worker_id):
        for project, raw in self.db.execute("SELECT id, state FROM projects"):
            worker = json.loads(raw)["workers"].get(worker_id)
            if worker:
                return project, worker
        raise Refused(f"No worker {worker_id} is bound")

    def checkout(self, config, path, pr):
        """The worker's checkout must be a clean worktree of the bound repository at the PR's head."""
        tree = Path(path).resolve()
        top = Path(git(tree, "rev-parse", "--show-toplevel")).resolve()
        require(top == tree, f"{tree} is not the top of a git checkout")
        common = Path(tree, git(tree, "rev-parse", "--git-common-dir")).resolve()
        bound = Path(config["workspace"], git(config["workspace"], "rev-parse", "--git-common-dir")).resolve()
        require(common == bound, f"{tree} is not a checkout of the bound repository {config['workspace']}")
        require(not git(tree, "status", "--porcelain"), f"{tree} has uncommitted changes; commit and push them first")
        head = git(tree, "rev-parse", "HEAD")
        require(head == pr["head"], f"{tree} is at {head[:12]} but PR #{pr['number']} is at {pr['head'][:12]}; push first")
        return str(tree)

    def run_checks(self, config, project, name, tree, head):
        """The binding's local checks in the worker's checkout; their logs go under state/harness-artifacts."""
        folder = self.path.parent / "harness-artifacts" / project / name / head
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
        pr, tree, evidence = self.submitted(config, project, task["id"], args)

        def change(config, state):
            task = state["tasks"][worker["task"]]
            require(task["rev"] == rev, "The task changed while the checks ran; run this again")
            task.update(pr=pr["number"], url=pr["url"], head=pr["head"], branch=pr["branch"], files=pr["files"],
                        worktree=tree, evidence=evidence)
            state["workers"][worker["id"]].update(path=tree)
            state["workers"][worker["id"]]["job"]["reported"] = True
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
            state["workers"][worker["id"]]["job"]["reported"] = True
            ask_id = next_id(state, "a")
            state["asks"][ask_id] = {"id": ask_id, "task": task["id"], "kind": "stuck", "options": None,
                                     "question": f"The implementer of {task['task']} ({task['plan']}) asks: {why}"}
            task["blockedFrom"] = "implementing"
            self.move(task, "blocked", "the implementer is stuck")
            return {"state": "blocked", "next": "Your question goes to the user; the answer comes as a follow-up. End your turn."}
        return self.transact(project, change, "stuck")

    def submitted(self, config, project, name, args):
        """A worker's PR as it stands: open against the bound branch, the worker's clean checkout at its head, and the
        binding's checks passing there. Returns (pr, checkout, evidence); a failed check refuses with its output."""
        pr = self.hub_for(config["github"]["repo"]).pull(args["pr"])
        require(pr["state"] == "open" and pr["base"] == config["branch"],
                f"PR #{args['pr']} must be open against {config['branch']}")
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
            state["workers"][worker["id"]]["job"]["reported"] = True
            task.setdefault("reviews", []).append({"sha": sha, "verdict": verdict})
            task.pop("returned", None)
            if verdict == "approve":
                task["approvedSha"] = sha
                self.move(task, "approved", f"t3/review on {sha[:12]}")
            else:
                task["fix"] = {"kind": "review", "detail": f"The reviewer asked for changes on PR #{task['pr']}."}
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
                me["job"]["reported"] = True
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
                return {"result": "current", "head": head, "next": wait_steps(config, task)}
            if same:
                task.update(head=head, approvedSha=head)
                task["rev"] += 1
                task["history"].append({"at": now(), "from": "merging", "to": "merging",
                                        "note": f"updated with {config['branch']}; approval carried to {head[:12]}"})
                return {"result": "updated", "head": head, "next": wait_steps(config, task)}
            task["fix"] = update_fix(config, outcome, number)
            task["head"] = head
            task.pop("devops", None)
            state["workers"][worker["id"]]["job"]["reported"] = True
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
            names = [*config["github"]["requiredChecks"], github.REVIEW_CONTEXT]
            waiting = [f"{name} is {result}" for name, result in gh.checks(pr["head"], names).items() if result != "success"]
            require(not waiting, f"PR #{number} cannot merge yet: {', '.join(waiting)}. {wait_steps(config, task)}")
            sha = gh.merge(number, pr["head"], f"{task['plan']}: {task['task']} (#{number})")
        moved = self.forward(config)

        def change(config, state):
            task = state["tasks"][self.held(state, worker)["id"]]
            require(task["rev"] == rev, "The task changed meanwhile")
            task.pop("devops", None)
            state["workers"][worker["id"]]["job"]["reported"] = True
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
            outcome = self.merge_step(config, task)
            self.transact(project, lambda config, state: self.merge_record(config, state, task["id"], task["rev"], outcome),
                          "harness_merge")
            if outcome["result"] != "merged":
                return

    def merge_step(self, config, task):
        """The GitHub side of one step, outside any transaction."""
        try:
            outcome = self.github_step(config, task)
        except github.Failed as error:
            health.failed(self.path.parent, "github", error)
            return {"result": "github", "error": str(error)}
        health.ok(self.path.parent, "github")
        return outcome

    def github_step(self, config, task):
        gh, number = self.hub_for(config["github"]["repo"]), task["pr"]
        pr = gh.pull(number)
        if pr["merged"]:  # An earlier merge that was not recorded.
            return {"result": "merged", "sha": pr["mergeCommit"], "moved": self.forward(config)}
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
        checks = gh.checks(head, [*config["github"]["requiredChecks"], github.REVIEW_CONTEXT])
        failed = [name for name, result in checks.items() if result == "failure"]
        if failed:
            return {"result": "ci", "failed": failed}
        if any(result != "success" for result in checks.values()):
            return {"result": "waiting"}
        sha = gh.merge(number, head, f"{task['plan']}: {task['task']} (#{number})")
        return {"result": "merged", "sha": sha, "moved": self.forward(config)}

    def merge_record(self, config, state, task_id, rev, outcome):
        task, result = state["tasks"][task_id], outcome["result"]
        number = task["pr"]
        if result == "merged":  # A fact on GitHub, whatever happened here meanwhile.
            task.pop("merger", None)
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
            names = [*config["github"]["requiredChecks"], github.REVIEW_CONTEXT]
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

    def failed(self, project, worker, args):
        cause = args.get("cause")
        require(cause in {"task", "pipeline"}, "--cause is task (the change broke CI) or pipeline (CI itself broke)")
        detail = text(args.get("detail"), "--detail")

        def change(config, state):
            task = self.held(state, worker)
            task.pop("devops", None)
            state["workers"][worker["id"]]["job"]["reported"] = True
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
                    "branch": pr["branch"], "files": pr["files"], "worktree": tree, "evidence": evidence}
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


def implementer_brief(config, task, worker):
    branch = config["branch"]
    me = f"--worker {worker['id']} --token {worker['token']}"
    if task.get("pr"):  # A restart: the work so far is on the PR's branch.
        start = (f"PR #{task['pr']} already holds this task's work on branch {task['branch']}. Continue it: "
                 f"git fetch origin {task['branch']} && git reset --hard origin/{task['branch']}",
                 f"When the work is done: commit and git push origin HEAD:{task['branch']}.")
    else:
        start = (f"Your checkout is new. Start with: git fetch origin {branch} && git reset --hard origin/{branch}",
                 f"When the work is done: commit, git push -u origin HEAD, then gh pr create --base {branch} "
                 f"--title \"{task['plan']}: {task['task']}\" with a body saying what changed, why, and how you checked it.")
    return "\n".join([
        f"You are the implementer of one task in the t3 workflow. First read {slash(ROOT / 'references' / 'implementer.md')}.",
        f"Plan: {task['plan']}. Task: {task['task']}.",
        f"Brief: {task['brief'] or task['task']}",
        f"Paths you may change: {', '.join(task['owns'])}.",
        start[0],
        f"Keep this checkout's branch name. Never push to {branch}, never force-push, never merge.",
        start[1],
        f"Then run: {command('worker', 'pr', me, '--pr', '<number>')}",
        "It runs the project's checks in this folder. If it refuses, fix what it names, push, and run it again. "
        "When it succeeds, end your turn.",
        f"If you cannot go on, run {command('worker', 'stuck', me, '--why', QUESTION)} and end your turn.",
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


def closing(worker, task):
    """The command a worker's job ends with, for a follow-up asking it to finish."""
    me = f"--worker {worker['id']} --token {worker['token']}"
    if worker["role"] == "implementer":
        return (command("worker", "pr", me, "--pr", str(task["pr"]) if task.get("pr") else "<number>")
                + f" (or, if you cannot go on: {command('worker', 'stuck', me, '--why', QUESTION)})")
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


def wait_steps(config, task):
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
        f"Review PR #{task['pr']} ({task.get('url')}) at commit {head}, branch {task['branch']}.",
        f"The implementer's checkout is {tree}. Read and test there; change nothing in it: no edits, commits or pushes.",
        f"The change: git -C {tree} diff origin/{branch}...{head}",
        *reviewed_before(config, task, tree),
        "Local checks at that commit:", *[f"- {line}" for line in evidence_lines(task)],
        f"Post your findings as one comment: gh pr comment {task['pr']} --repo {config['github']['repo']} --body \"...\"",
        f"Then run {command('worker', 'review', me, '--verdict', 'approve', '--sha', head)} "
        f"or the same with --verdict changes, and end your turn.",
    ])
