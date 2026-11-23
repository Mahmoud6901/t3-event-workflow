"""What the model reads stays small and true: each role's docs have a budget, and everything they name exists."""
import inspect
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import flow
import t3_hooks

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
REFERENCES = ROOT / "references"
DOCS = [*sorted(REFERENCES.glob("*.md")), ROOT / "SKILL.md"]
CODE = sorted(p for p in SCRIPTS.glob("*.py") if not p.name.startswith("test_"))
OPERATIONS = [SCRIPTS / name for name in ("harness.py", "flow.py")]

# Characters a role reads to start: the orchestrator its docs and the context its hook adds each turn, a worker the doc its
# brief names. Raise one only on purpose, and cut something first if you can: a session pays for these on every turn.
ROLE_DOCS = {**t3_hooks.ROLE_DOCS, "implementer": ("implementer.md",), "reviewer": ("reviewer.md",), "devops": ("devops.md",)}
BUDGET = {"orchestrator": 14930, "implementer": 2700, "reviewer": 2080, "devops": 3150}  # About 5% over 2026-11-10; raised 2026-11-23.
with patch.object(t3_hooks, "REFERENCES", Path("references")):  # Where the copy is installed is not the docs' length.
    FIXED_CONTEXT = {"orchestrator": t3_hooks.orchestrator_context("codex")}  # The longer of the two runtimes' contexts.


def read(path):
    return path.read_text(encoding="utf-8")


def dispatched(method):
    """The names a dispatcher maps to its handlers: {"next": self.next, ...}."""
    return set(re.findall(r'"(\w+)": self\.\w+', inspect.getsource(method)))


def known_ops():
    """Every operation the orchestrator can run, from its handler map and the ones it runs before the binding check."""
    source = inspect.getsource(flow.Flow.orchestrator)
    return {*re.findall(r'op == "(\w+)"', source), *dispatched(flow.Flow.orchestrator)}


def known_commands():
    """Every worker command."""
    return dispatched(flow.Flow.worker)


def known_fields():
    """Every key the code reads from a request, or names in one."""
    code = "\n".join(read(path) for path in OPERATIONS)
    return set(re.findall(r'"(\w+)"', code))


CALL = re.compile(r'\\?"op\\?"\s*:\s*\\?"(\w+)\\?"')
JSON_CALL = re.compile(r'\{"op":.*?\}(?=`|\s|\.|,|$)', re.M)


def named_ops(text):
    return set(CALL.findall(text))


def named_fields(text):
    """The keys of each `{"op":...}` call written in a document. A field starts in lower case; a plan's name (M36) is data."""
    return {key for call in JSON_CALL.findall(text) for key in re.findall(r'"([a-z]\w*)"\s*:', call)}


def heading_slugs(text):
    slugs = set()
    for heading in re.findall(r"^#+ (.+)$", text, re.M):
        slugs.add(re.sub(r"[^\w\- ]", "", heading.lower()).strip().replace(" ", "-"))
    return slugs


class ModelSurfaceTests(unittest.TestCase):
    def test_each_roles_docs_stay_within_their_budget(self):
        for role, budget in BUDGET.items():
            size = sum(len(read(REFERENCES / name)) for name in ROLE_DOCS[role]) + len(FIXED_CONTEXT.get(role, ""))
            self.assertLessEqual(size, budget, f"{role} reads {size} characters; its budget is {budget}")

    def test_every_op_the_docs_and_fixed_text_name_exists(self):
        ops = known_ops()
        for path in [*DOCS, *CODE]:
            with self.subTest(path.name):
                self.assertEqual(sorted(named_ops(read(path)) - ops), [])

    def test_every_worker_command_the_docs_name_exists(self):
        for path in DOCS:
            with self.subTest(path.name):
                self.assertEqual(sorted(set(re.findall(r"`worker (\w+)", read(path))) - known_commands()), [])

    def test_every_field_in_a_documented_call_exists(self):
        fields = known_fields()
        for path in DOCS:
            with self.subTest(path.name):
                self.assertEqual(sorted(named_fields(read(path)) - fields), [])

    def test_every_script_the_docs_name_exists(self):
        for path in DOCS:
            for name in re.findall(r"`(?:scripts/)?(\w+\.(?:py|mjs))\b", read(path)):
                with self.subTest(f"{path.name}: {name}"):
                    self.assertTrue((SCRIPTS / name).exists() or (ROOT / name).exists())

    def test_every_relative_link_and_its_heading_resolve(self):
        for path in DOCS:
            for target in re.findall(r"\]\(([^)\s]+)\)", read(path)):
                if re.match(r"\w+:", target):
                    continue
                file, _, anchor = target.partition("#")
                linked = (path.parent / file).resolve() if file else path
                with self.subTest(f"{path.name}: {target}"):
                    self.assertTrue(linked.exists())
                    if anchor:
                        self.assertIn(anchor, heading_slugs(read(linked)))

    def test_the_example_config_is_a_valid_binding(self):
        example = json.loads(read(ROOT / "assets" / "config.example.json"))
        with tempfile.TemporaryDirectory() as folder:
            stored = flow.validate_config({**example, "workspace": folder})
        self.assertEqual({**stored, "workspace": example["workspace"]}, example, "it shows every key init stores")

    def test_the_checks_can_fail(self):
        # They scan text, so a broken pattern would pass by finding nothing.
        self.assertLessEqual({"init", "next", "done", "status"}, known_ops())
        self.assertNotIn("pr", known_ops())
        self.assertLessEqual({"pr", "review", "take", "merge"}, known_commands())
        self.assertEqual(named_ops('Run {"op":"made_up"} and {\\"op\\":\\"also_made_up\\"}.'), {"made_up", "also_made_up"})
        self.assertEqual(named_fields('`{"op":"next","taskId":"t1","invented":1}`'), {"op", "taskId", "invented"})
        self.assertIn("every-wake", heading_slugs("## Every wake"))


if __name__ == "__main__":
    unittest.main()
