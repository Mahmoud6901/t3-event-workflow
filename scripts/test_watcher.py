"""The watcher's lock tells whether it lives, and a bound session's hook restarts a dead one."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watcher


class WatcherLifeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)

    def test_a_held_lock_means_running_and_nothing_is_started(self):
        lock = watcher.hold(self.state)
        self.addCleanup(lock.close)
        self.assertIsNone(watcher.hold(self.state))  # A second watcher exits.
        self.assertTrue(watcher.running(self.state))
        with patch.object(watcher, "start") as start:
            self.assertFalse(watcher.ensure(self.state))
        start.assert_not_called()

    def test_a_free_lock_means_not_running_and_one_is_started(self):
        self.assertFalse(watcher.running(self.state))
        with patch.object(watcher, "start") as start:
            self.assertTrue(watcher.ensure(self.state))
        start.assert_called_once_with()

    def test_asking_leaves_the_lock_free_for_the_watcher_it_starts(self):
        watcher.running(self.state)
        lock = watcher.hold(self.state)
        self.addCleanup(lock.close)
        self.assertIsNotNone(lock)

    def test_the_lock_frees_when_its_watcher_ends(self):
        watcher.hold(self.state).close()
        self.assertFalse(watcher.running(self.state))


if __name__ == "__main__":
    unittest.main()
