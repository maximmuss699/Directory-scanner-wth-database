import os
import tempfile
import unittest
from pathlib import Path

from database import update_snapshot


class SnapshotTestCase(unittest.TestCase):
    """Provide one isolated directory and database for each test."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        temporary_path = Path(self.temporary_directory.name)
        self.folder = temporary_path / "folder"
        self.folder.mkdir()
        self.database = temporary_path / "snapshot.db"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def assert_change_counts(self, result, **expected):
        counts = {"create": 0, "delete": 0, "modify": 0, "rename": 0}
        counts.update(expected)
        self.assertEqual(result.change_counts, counts)

    def scan(self):
        return update_snapshot(str(self.folder), str(self.database))

    def assert_mtime_changed(self, path, previous_stat):
        os.utime(
            path,
            ns=(
                previous_stat.st_atime_ns,
                previous_stat.st_mtime_ns + 2_000_000_000,
            ),
        )
        self.assertNotEqual(path.stat().st_mtime_ns, previous_stat.st_mtime_ns)
