import os
import sqlite3
import stat as stat_module
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scanner import scan_folder
from tests.support import SnapshotTestCase


class DirectoryScannerTests(SnapshotTestCase):
    """Test traversal, relative paths, and link cycle protection."""

    def test_symlink_to_ancestor_is_stored_but_not_traversed(self):
        """Check that a directory symlink does not create a scan cycle."""
        subdirectory = self.folder / "subdirectory"
        subdirectory.mkdir()
        link_path = subdirectory / "back-to-root"
        try:
            os.symlink(self.folder, link_path, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"Directory symlinks are unavailable: {error}")

        result = self.scan()

        self.assert_change_counts(result, create=2)
        self.assertEqual(result.entry_count, 2)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            entry_type = connection.execute(
                "SELECT entry_type FROM entries WHERE path = ?",
                (os.path.join("subdirectory", "back-to-root"),),
            ).fetchone()[0]
        self.assertEqual(entry_type, "symlink")

    def test_windows_junction_is_stored_without_being_traversed(self):
        """Check that a Windows junction is saved but not followed."""
        root_stat = SimpleNamespace(st_dev=1, st_ino=10)
        junction_stat = SimpleNamespace(
            st_mode=stat_module.S_IFDIR | 0o755,
            st_size=0,
            st_mtime_ns=1,
            st_dev=1,
            st_ino=20,
            st_file_attributes=0x400,
        )
        junction_entry = SimpleNamespace(
            path=os.path.join(str(self.folder), "junction"),
            name="junction",
            is_junction=lambda: True,
        )
        scandir_context = MagicMock()
        scandir_context.__enter__.return_value = iter([junction_entry])
        scandir_context.__exit__.return_value = False

        with patch("scanner.os.name", "nt"):
            with patch(
                "scanner.os.stat", side_effect=[root_stat, junction_stat]
            ):
                with patch(
                    "scanner.os.scandir", return_value=scandir_context
                ) as scandir:
                    records = list(scan_folder(str(self.folder)))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][3], "junction")
        scandir.assert_called_once_with(str(self.folder))

    def test_scanner_builds_relative_paths_during_traversal(self):
        """Check nested paths do not require repeated path normalization."""
        nested_directory = self.folder / "outer" / "inner"
        nested_directory.mkdir(parents=True)
        nested_file = nested_directory / "file.txt"
        nested_file.write_text("content", encoding="utf-8")

        with patch(
            "scanner.os.path.relpath",
            side_effect=AssertionError("os.path.relpath should not be called"),
        ):
            records = list(scan_folder(str(self.folder)))

        records_by_path = {record[0]: record for record in records}
        relative_file_path = os.path.join("outer", "inner", "file.txt")
        self.assertEqual(
            records_by_path[relative_file_path][1],
            os.path.join("outer", "inner"),
        )
