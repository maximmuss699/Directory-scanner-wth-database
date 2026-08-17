import hashlib
import io
import os
import sqlite3
import stat as stat_module
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli import main as cli_main, print_result
from database import update_snapshot
from hashing import (
    FileChangedDuringHashingError,
    calculate_stable_hash,
    same_file_state,
)
from scanner import scan_folder


class UpdateSnapshotTests(unittest.TestCase):
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

    def test_create_no_change_modify_rename_and_delete(self):
        """Check the main file change lifecycle."""
        old_path = self.folder / "old.txt"
        old_path.write_text("first", encoding="utf-8")

        result = self.scan()
        self.assert_change_counts(result, create=1)
        self.assertEqual(result.entry_count, 1)

        result = self.scan()
        self.assert_change_counts(result)

        old_path.write_text("modified content", encoding="utf-8")
        result = self.scan()
        self.assert_change_counts(result, modify=1)

        new_path = self.folder / "new.txt"
        old_path.rename(new_path)
        result = self.scan()
        self.assert_change_counts(result, rename=1)
        self.assertEqual(result.changes[0].previous_path, "old.txt")
        self.assertEqual(result.changes[0].path, "new.txt")

        new_path.unlink()
        result = self.scan()
        self.assert_change_counts(result, delete=1)

    def test_hard_links_are_not_guessed_as_renames(self):
        """Check that ambiguous hard links are not reported as renames."""
        original_path = self.folder / "original.txt"
        linked_path = self.folder / "linked.txt"
        renamed_path = self.folder / "renamed.txt"
        original_path.write_text("shared content", encoding="utf-8")

        try:
            os.link(original_path, linked_path)
        except OSError as error:
            self.skipTest(f"Hard links are unavailable: {error}")

        self.scan()
        linked_path.rename(renamed_path)

        result = self.scan()
        self.assert_change_counts(result, create=1, delete=1)

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

    def test_large_windows_file_identity_is_stored_without_overflow(self):
        """Check that large Windows file IDs fit in the database."""
        directory_path = self.folder / "large-identity"
        directory_path.mkdir()
        directory_stat = directory_path.stat()
        large_device_id = str(2**80 + 123)
        large_file_id = str(2**127 + 456)
        staged_record = (
            "large-identity",
            "",
            "large-identity",
            "directory",
            directory_stat.st_size,
            directory_stat.st_mtime_ns,
            large_device_id,
            large_file_id,
            None,
        )

        with patch("database.scan_folder", return_value=iter([staged_record])):
            initial_result = update_snapshot(str(self.folder), str(self.database))

        self.assert_change_counts(initial_result, create=1)
        renamed_path = self.folder / "renamed-large-identity"
        directory_path.rename(renamed_path)
        renamed_record = (
            "renamed-large-identity",
            "",
            "renamed-large-identity",
            "directory",
            directory_stat.st_size,
            directory_stat.st_mtime_ns,
            large_device_id,
            large_file_id,
            None,
        )
        with patch("database.scan_folder", return_value=iter([renamed_record])):
            rename_result = update_snapshot(str(self.folder), str(self.database))

        self.assert_change_counts(rename_result, rename=1)
        self.assertEqual(
            (
                rename_result.changes[0].previous_path,
                rename_result.changes[0].path,
            ),
            ("large-identity", "renamed-large-identity"),
        )
        with closing(sqlite3.connect(self.database)) as connection, connection:
            stored = connection.execute(
                """
                SELECT device_id, file_id, typeof(device_id), typeof(file_id)
                FROM entries
                WHERE path = 'renamed-large-identity'
                """
            ).fetchone()
        self.assertEqual(
            stored,
            (large_device_id, large_file_id, "text", "text"),
        )

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

    def test_rename_and_content_change_reports_both_changes(self):
        """Check that a renamed and modified file reports both changes."""
        old_path = self.folder / "old.txt"
        new_path = self.folder / "new.txt"
        old_path.write_text("AAAA", encoding="utf-8")
        self.scan()

        original_stat = old_path.stat()
        old_path.rename(new_path)
        new_path.write_text("BBBB changed", encoding="utf-8")
        os.utime(
            new_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        result = self.scan()
        self.assert_change_counts(result, modify=1, rename=1)

    def test_failed_scan_keeps_previous_snapshot(self):
        """Check that a scan error does not replace the valid snapshot."""
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()

        with patch("scanner.os.scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.scan()

        result = self.scan()
        self.assert_change_counts(result)

    def test_missing_directory_is_rejected(self):
        """Check that a missing input directory is rejected."""
        missing_folder = self.folder / "missing"

        with self.assertRaisesRegex(ValueError, "Directory does not exist"):
            update_snapshot(str(missing_folder), str(self.database))

    def test_equal_hash_with_changed_mtime_is_not_a_modification(self):
        """Check that unchanged content is not reported as modified."""
        file_path = self.folder / "same-content.txt"
        file_path.write_text("unchanged content", encoding="utf-8")
        self.scan()

        original_stat = file_path.stat()
        self.assert_mtime_changed(file_path, original_stat)

        result = self.scan()
        self.assert_change_counts(result)

    def test_creating_nested_file_does_not_modify_parent_directory(self):
        """Check that a new child does not mark its directory as modified."""
        subdirectory = self.folder / "subdirectory"
        subdirectory.mkdir()
        self.scan()

        previous_directory_stat = subdirectory.stat()
        (subdirectory / "new.txt").write_text("content", encoding="utf-8")
        self.assert_mtime_changed(subdirectory, previous_directory_stat)

        result = self.scan()
        self.assert_change_counts(result, create=1)
        self.assertEqual(
            result.changes[0].path, os.path.join("subdirectory", "new.txt")
        )

    def test_rename_and_new_file_at_old_path_reports_rename_and_create(self):
        """Check a rename followed by creating a new file at the old path."""
        old_path = self.folder / "old.txt"
        new_path = self.folder / "new.txt"
        old_path.write_text("original entity", encoding="utf-8")
        self.scan()

        old_path.rename(new_path)
        old_path.write_text("new entity", encoding="utf-8")

        result = self.scan()
        self.assert_change_counts(result, create=1, rename=1)
        self.assertEqual(
            {
                (change.change_type, change.path, change.previous_path)
                for change in result.changes
            },
            {
                ("create", "old.txt", None),
                ("rename", "new.txt", "old.txt"),
            },
        )

    def test_swapping_two_paths_reports_only_two_renames(self):
        """Check that swapping two files reports only two renames."""
        first_path = self.folder / "first.txt"
        second_path = self.folder / "second.txt"
        temporary_path = self.folder / "temporary.txt"
        first_path.write_text("first entity", encoding="utf-8")
        second_path.write_text("second entity", encoding="utf-8")
        self.scan()

        first_path.rename(temporary_path)
        second_path.rename(first_path)
        temporary_path.rename(second_path)

        result = self.scan()
        self.assert_change_counts(result, rename=2)
        self.assertEqual(
            {
                (change.previous_path, change.path)
                for change in result.changes
            },
            {("first.txt", "second.txt"), ("second.txt", "first.txt")},
        )

    def test_directory_rename_collapse_handles_interleaved_sibling_names(self):
        """Check that a directory move hides implied child renames."""
        old_directory = self.folder / "a"
        old_directory.mkdir()
        (old_directory / "child.txt").write_text("child", encoding="utf-8")
        sibling = self.folder / "a space"
        sibling.write_text("sibling", encoding="utf-8")
        self.scan()

        old_directory.rename(self.folder / "b")
        sibling.rename(self.folder / "z")
        result = self.scan()

        self.assert_change_counts(result, rename=2)
        self.assertEqual(
            {
                (change.previous_path, change.path)
                for change in result.changes
            },
            {("a", "b"), ("a space", "z")},
        )

    def test_same_path_identity_replacement_reports_delete_and_create(self):
        """Check that replacing an entity at one path is delete plus create."""
        file_path = self.folder / "replaced.txt"
        replacement_path = Path(self.temporary_directory.name) / "replacement.txt"
        file_path.write_text("old entity", encoding="utf-8")
        self.scan()
        previous_stat = file_path.stat()

        replacement_path.write_text("new entity", encoding="utf-8")
        os.replace(replacement_path, file_path)
        replacement_stat = file_path.stat()
        if (
            previous_stat.st_dev == replacement_stat.st_dev
            and previous_stat.st_ino == replacement_stat.st_ino
        ):
            self.skipTest("Filesystem reused the same identity for the replacement")

        result = self.scan()
        self.assert_change_counts(result, create=1, delete=1)
        self.assertEqual(
            {(change.change_type, change.path) for change in result.changes},
            {("create", "replaced.txt"), ("delete", "replaced.txt")},
        )

    def test_unchanged_file_reuses_its_hash(self):
        """Check that an unchanged file is not hashed again."""
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        update_snapshot(str(self.folder), str(self.database))

        with patch(
            "database.calculate_stable_hash",
            side_effect=AssertionError("unchanged file was rehashed"),
        ):
            result = update_snapshot(str(self.folder), str(self.database))

        self.assert_change_counts(result)

    def test_hash_is_reused_across_rename(self):
        """Check that a uniquely renamed file keeps its stored hash."""
        old_path = self.folder / "old.txt"
        new_path = self.folder / "new.txt"
        old_path.write_text("content", encoding="utf-8")
        update_snapshot(str(self.folder), str(self.database))
        old_path.rename(new_path)

        with patch(
            "database.calculate_stable_hash",
            side_effect=AssertionError("renamed file was rehashed"),
        ):
            result = update_snapshot(str(self.folder), str(self.database))

        self.assert_change_counts(result, rename=1)

    def test_scan_retries_if_a_reused_file_disappears(self):
        """Check retry behavior when a reused file disappears."""
        file_path = self.folder / "disappearing.txt"
        file_path.write_text("content", encoding="utf-8")
        update_snapshot(str(self.folder), str(self.database))

        def disappearing_scan(folder_path):
            for record in scan_folder(folder_path):
                yield record
                if record[0] == "disappearing.txt" and file_path.exists():
                    file_path.unlink()

        with patch("database.scan_folder", side_effect=disappearing_scan):
            result = update_snapshot(str(self.folder), str(self.database))

        self.assert_change_counts(result, delete=1)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            entry_count = connection.execute(
                "SELECT COUNT(*) FROM entries"
            ).fetchone()[0]
        self.assertEqual(entry_count, 0)

    def test_hash_permission_error_keeps_previous_snapshot(self):
        """Check rollback when file content cannot be read."""
        file_path = self.folder / "protected.txt"
        file_path.write_text("content", encoding="utf-8")
        self.scan()
        file_path.write_text("changed content", encoding="utf-8")

        with patch(
            "database.calculate_stable_hash",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(PermissionError):
                self.scan()

        result = self.scan()
        self.assert_change_counts(result, modify=1)

    def test_database_inside_scanned_folder_is_rejected(self):
        """Check that the database cannot scan its own files."""
        database_inside_folder = self.folder / "snapshot.db"

        with self.assertRaises(ValueError):
            update_snapshot(str(self.folder), str(database_inside_folder))

        self.assertFalse(database_inside_folder.exists())

    def test_database_cannot_be_reused_for_a_different_root(self):
        """Check that one database is bound to one root directory."""
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()
        other_folder = Path(self.temporary_directory.name) / "other-folder"
        other_folder.mkdir()

        with self.assertRaises(ValueError):
            update_snapshot(str(other_folder), str(self.database))

        result = self.scan()
        self.assert_change_counts(result)

    def test_partial_snapshot_is_rolled_back_before_retry(self):
        """Check that retry starts with an empty temporary snapshot."""
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()
        attempt_count = 0

        def flaky_scan(folder_path):
            nonlocal attempt_count
            attempt_count += 1
            entries = scan_folder(folder_path)
            if attempt_count == 1:
                yield next(entries)
                raise FileNotFoundError("changed during scan")
            yield from entries

        with patch("database.scan_folder", side_effect=flaky_scan):
            result = self.scan()

        self.assertEqual(attempt_count, 2)
        self.assertEqual(result.entry_count, 1)
        self.assert_change_counts(result)

    def test_unchanged_scan_does_not_rewrite_persistent_entries(self):
        """Check that unchanged rows are not written again."""
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE entry_write_audit (operation TEXT)")
            for operation in ("INSERT", "UPDATE", "DELETE"):
                connection.execute(
                    f"""
                    CREATE TRIGGER audit_entries_{operation.lower()}
                    AFTER {operation} ON entries
                    BEGIN
                        INSERT INTO entry_write_audit VALUES ('{operation}');
                    END
                    """
                )

        result = self.scan()
        self.assert_change_counts(result)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            write_count = connection.execute(
                "SELECT COUNT(*) FROM entry_write_audit"
            ).fetchone()[0]
        self.assertEqual(write_count, 0)

    def test_zero_change_display_limit_does_not_claim_there_are_no_changes(self):
        """Check terminal output when change details are hidden."""
        (self.folder / "created.txt").write_text("content", encoding="utf-8")
        result = update_snapshot(
            str(self.folder), str(self.database), change_limit=0
        )

        output = io.StringIO()
        with redirect_stdout(output):
            print_result(
                result,
                str(self.folder),
                str(self.database),
                0.1,
            )

        self.assertIn("Created  : 1", output.getvalue())
        self.assertNotIn("No changes detected.", output.getvalue())

    def test_cli_prints_a_short_error_and_returns_failure(self):
        """Check that an expected CLI error is shown without a traceback."""
        output = io.StringIO()

        with patch("cli.run", side_effect=ValueError("invalid input")):
            with redirect_stderr(output):
                exit_code = cli_main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), "Error: invalid input\n")

    def test_stable_hash_rejects_changed_device_at_final_path_check(self):
        """Check that hashing rejects a file replaced on another device."""
        file_path = self.folder / "moving.txt"
        file_path.write_text("content", encoding="utf-8")
        actual_stat = os.stat(file_path, follow_symlinks=False)
        changed_device_stat = SimpleNamespace(
            st_mode=actual_stat.st_mode,
            st_dev=actual_stat.st_dev + 1,
            st_ino=actual_stat.st_ino,
            st_size=actual_stat.st_size,
            st_mtime_ns=actual_stat.st_mtime_ns,
            st_ctime_ns=actual_stat.st_ctime_ns,
        )

        with patch(
            "hashing.os.stat",
            side_effect=[
                actual_stat,
                changed_device_stat,
                actual_stat,
                changed_device_stat,
            ],
        ) as mocked_stat:
            with self.assertRaises(FileChangedDuringHashingError):
                calculate_stable_hash(str(file_path))

        # Two attempts, each with a stat before and after reading the path.
        self.assertEqual(mocked_stat.call_count, 4)

    def test_stable_hash_reads_exactly_the_stat_size(self):
        """Check hashing does not issue an extra read to discover EOF."""
        file_path = self.folder / "sized.txt"
        file_path.write_bytes(b"abcdef")

        class TrackingFile:
            def __init__(self, path):
                self.file = open(path, "rb")
                self.read_sizes = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.file.close()

            def fileno(self):
                return self.file.fileno()

            def read(self, size):
                self.read_sizes.append(size)
                return self.file.read(size)

        tracked_file = TrackingFile(file_path)
        with patch("hashing.open", return_value=tracked_file, create=True):
            with patch("hashing.HASH_CHUNK_SIZE", 4):
                content_hash, _ = calculate_stable_hash(str(file_path))

        self.assertEqual(tracked_file.read_sizes, [4, 2])
        self.assertEqual(
            content_hash,
            hashlib.blake2b(b"abcdef", digest_size=32).digest(),
        )

    def test_windows_stable_hash_ignores_inconsistent_ctime(self):
        """Check Windows path and descriptor ctime differences are harmless."""
        first = SimpleNamespace(
            st_mode=stat_module.S_IFREG,
            st_dev=1,
            st_ino=2,
            st_size=3,
            st_mtime_ns=4,
            st_ctime_ns=5,
        )
        second = SimpleNamespace(
            st_mode=stat_module.S_IFREG,
            st_dev=1,
            st_ino=2,
            st_size=3,
            st_mtime_ns=4,
            st_ctime_ns=6,
        )

        with patch("hashing.os.name", "nt"):
            self.assertTrue(same_file_state(first, second))

        with patch("hashing.os.name", "posix"):
            self.assertFalse(same_file_state(first, second))


if __name__ == "__main__":
    unittest.main()
