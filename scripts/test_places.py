"""Where the workflow keeps its data."""
import os
from pathlib import Path
import re
import tempfile
import unittest

import places

# A path part made straight from an id T3 or GitHub gives: such an id can hold characters no folder name may.
RAW_ID_IN_PATH = re.compile(r"\s/\s+(?:project|thread|thread_id|task_id|child|head)\b")
DEVICES ={"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


class PlacesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.code, self.home = Path(temp.name, "code"), Path(temp.name, "home")
        self.code.mkdir()

    def test_a_state_folder_beside_the_code_keeps_the_data_there(self):
        # The owner's clone holds live bindings in state/: they are never moved (2026-11-16).
        (self.code / "state").mkdir()
        self.assertEqual(places.base(self.code, self.home), self.code)

    def test_a_fresh_install_keeps_its_data_in_the_home_folder_not_in_its_package(self):
        self.assertEqual(places.base(self.code, self.home), self.home / ".t3-workflow")

    def test_a_name_from_outside_becomes_one_every_system_accepts_and_a_safe_one_stays_as_it_is(self):
        # T3 gave a project an id of the form mcp:<uuid> (2026-11-23), and Windows refused it as a folder name.
        for safe in ("00000000-1111-4222-8333-444444444444", "pipeline-95", "t3", "v1.2_x"):
            self.assertEqual(places.segment(safe), safe)
        unsafe = ["mcp:00000000-1111-4222-8333-444444444444", 'a<b>c"d', "a/b\\c", "../up", "a|b?c*", "..", ".", "",
                  "CON", "nul", "com1", "LPT9.txt", "name.", "name ", "tab\there", "ünïcode", "x" * 300]
        names = [places.segment(value) for value in unsafe]
        for value, name in zip(unsafe, names):
            with self.subTest(value=value):
                self.assertRegex(name, r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
                self.assertFalse(name.endswith("."), "Windows drops a trailing dot")
                self.assertNotIn(name.split(".")[0].upper(), DEVICES)
                self.assertLessEqual(len(name), 80)
                (self.code / name).mkdir()  # The system itself is the judge, and a name used twice would fail here.
                self.assertIn(name, os.listdir(self.code), "the system kept the name as given")
        self.assertNotEqual(places.segment("mcp:00000000"), places.segment("mcp-00000000"), "two ids never share a folder")

    def test_the_code_makes_no_path_part_straight_from_an_outside_id(self):
        code = [*sorted((places.CODE / "scripts").glob("[!t]*.py")), *sorted((places.CODE / "scripts").glob("t3_*.py")),
                places.CODE / "install.py"]
        for path in code:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                with self.subTest(f"{path.name}:{number}"):
                    self.assertIsNone(RAW_ID_IN_PATH.search(line), f"pass the id through places.segment(): {line.strip()}")
        self.assertTrue(RAW_ID_IN_PATH.search('folder = state / "harness-artifacts" / project / head'), "the check can fail")
        self.assertIsNone(RAW_ID_IN_PATH.search("# each session/thread ID"))


if __name__ == "__main__":
    unittest.main()
