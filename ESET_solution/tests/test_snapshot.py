import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database import update_snapshot
from tests.support import SnapshotTestCase


class SnapshotBehaviorTests(SnapshotTestCase):
    """Test change detection, hash reuse, and database safety."""

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

    def test_equal_hash_with_changed_mtime_is_not_a_modification(self):
        """Check that unchanged content is not reported as modified."""
        file_path = self.folder / "same-content.txt"
        file_path.write_text("unchanged content", encoding="utf-8")
        self.scan()

        original_stat = file_path.stat()
        self.assert_mtime_changed(file_path, original_stat)

        result = self.scan()
        self.assert_change_counts(result)

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

    def test_nested_directory_rename_keeps_independent_file_rename(self):
        """Check that the nearest directory rename controls child collapsing."""
        old_directory = self.folder / "a"
        nested_directory = old_directory / "b"
        nested_directory.mkdir(parents=True)
        (nested_directory / "file.txt").write_text("content", encoding="utf-8")
        self.scan()

        new_directory = self.folder / "x"
        old_directory.rename(new_directory)
        (new_directory / "b").rename(new_directory / "c")
        (new_directory / "b").mkdir()
        (new_directory / "c" / "file.txt").rename(
            new_directory / "b" / "file.txt"
        )
        result = self.scan()

        self.assert_change_counts(result, create=1, rename=3)
        self.assertEqual(
            {
                (change.previous_path, change.path)
                for change in result.changes
                if change.change_type == "rename"
            },
            {
                ("a", "x"),
                (os.path.join("a", "b"), os.path.join("x", "c")),
                (
                    os.path.join("a", "b", "file.txt"),
                    os.path.join("x", "b", "file.txt"),
                ),
            },
        )

    def test_same_path_file_replacement_reports_modify(self):
        """Check that replacing a file at one path is one modification."""
        file_path = self.folder / "replaced.txt"
        replacement_path = Path(self.temporary_directory.name) / "replacement.txt"
        file_path.write_text("same content", encoding="utf-8")
        self.scan()
        previous_stat = file_path.stat()

        replacement_path.write_text("same content", encoding="utf-8")
        os.replace(replacement_path, file_path)
        replacement_stat = file_path.stat()
        if (
            previous_stat.st_dev == replacement_stat.st_dev
            and previous_stat.st_ino == replacement_stat.st_ino
        ):
            self.skipTest("Filesystem reused the same identity for the replacement")

        result = self.scan()
        self.assert_change_counts(result, modify=1)
        self.assertEqual(
            [
                (change.change_type, change.path, change.previous_path)
                for change in result.changes
            ],
            [("modify", "replaced.txt", None)],
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
