"""Where the workflow keeps its data."""
from pathlib import Path
import tempfile
import unittest

import places


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


if __name__ == "__main__":
    unittest.main()
