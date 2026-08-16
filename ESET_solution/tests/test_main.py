import io
import os
import sqlite3
import stat as stat_module
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli import print_result
from database import update_snapshot
from hashing import FileChangedDuringHashingError, calculate_stable_hash
from scanner import is_directory_junction, scan_folder


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
        with sqlite3.connect(self.database) as connection:
            entry_type = connection.execute(
                "SELECT entry_type FROM entries WHERE path = ?",
                (os.path.join("subdirectory", "back-to-root"),),
            ).fetchone()[0]
        self.assertEqual(entry_type, "symlink")

    def test_scanner_serializes_filesystem_identity_as_text(self):
        (self.folder / "identity.txt").write_text("content", encoding="utf-8")

        record = next(scan_folder(str(self.folder)))

        self.assertIsInstance(record[6], str)
        self.assertIsInstance(record[7], str)

    def test_large_windows_file_identity_is_stored_without_overflow(self):
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
            initial_result = update_snapshot(
                str(self.folder), str(self.database), hash_mode="off"
            )

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
            rename_result = update_snapshot(
                str(self.folder), str(self.database), hash_mode="off"
            )

        self.assert_change_counts(rename_result, rename=1)
        self.assertEqual(
            (
                rename_result.changes[0].previous_path,
                rename_result.changes[0].path,
            ),
            ("large-identity", "renamed-large-identity"),
        )
        with sqlite3.connect(self.database) as connection:
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

    def test_windows_reparse_attribute_fallback_detects_junction(self):
        entry = SimpleNamespace()
        junction_stat = SimpleNamespace(
            st_mode=stat_module.S_IFDIR | 0o755,
            st_file_attributes=0x400,
        )

        with patch("scanner.os.name", "nt"):
            self.assertTrue(is_directory_junction(entry, junction_stat))

    def test_rename_and_content_change_reports_both_changes(self):
        old_path = self.folder / "old.txt"
        new_path = self.folder / "new.txt"
        old_path.write_text("AAAA", encoding="utf-8")
        self.scan()

        original_stat = old_path.stat()
        old_path.rename(new_path)
        new_path.write_text("BBBB", encoding="utf-8")
        os.utime(
            new_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        result = self.scan()
        self.assert_change_counts(result, modify=1, rename=1)

    def test_failed_scan_keeps_previous_snapshot(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()

        with patch("scanner.os.scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.scan()

        result = self.scan()
        self.assert_change_counts(result)

    def test_missing_directory_is_rejected(self):
        missing_folder = self.folder / "missing"

        with self.assertRaisesRegex(ValueError, "Directory does not exist"):
            update_snapshot(str(missing_folder), str(self.database))

    def test_always_mode_detects_same_size_change_with_restored_mtime(self):
        file_path = self.folder / "same-size.txt"
        file_path.write_text("AAAA", encoding="utf-8")
        self.scan()

        original_stat = file_path.stat()
        file_path.write_text("BBBB", encoding="utf-8")
        os.utime(
            file_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        result = self.scan()
        self.assert_change_counts(result, modify=1)

    def test_equal_hash_with_changed_mtime_is_not_a_modification(self):
        file_path = self.folder / "same-content.txt"
        file_path.write_text("unchanged content", encoding="utf-8")
        self.scan()

        original_stat = file_path.stat()
        self.assert_mtime_changed(file_path, original_stat)

        result = self.scan()
        self.assert_change_counts(result)

    def test_rename_with_only_mtime_change_is_not_a_modification(self):
        old_path = self.folder / "old.txt"
        new_path = self.folder / "new.txt"
        old_path.write_text("unchanged content", encoding="utf-8")
        self.scan()

        original_stat = old_path.stat()
        old_path.rename(new_path)
        self.assert_mtime_changed(new_path, original_stat)

        result = self.scan()
        self.assert_change_counts(result, rename=1)
        self.assertEqual(
            [(change.previous_path, change.path) for change in result.changes],
            [("old.txt", "new.txt")],
        )

    def test_creating_nested_file_does_not_modify_parent_directory(self):
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

    def test_rename_over_existing_destination_reports_displaced_delete(self):
        source_path = self.folder / "source.txt"
        destination_path = self.folder / "destination.txt"
        source_path.write_text("source entity", encoding="utf-8")
        destination_path.write_text("destination entity", encoding="utf-8")
        self.scan()

        os.replace(source_path, destination_path)

        result = self.scan()
        self.assert_change_counts(result, delete=1, rename=1)
        self.assertEqual(
            {
                (change.change_type, change.path, change.previous_path)
                for change in result.changes
            },
            {
                ("delete", "destination.txt", None),
                ("rename", "destination.txt", "source.txt"),
            },
        )

    def test_swapping_two_paths_reports_only_two_renames(self):
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

    def test_directory_rename_collapses_implied_descendant_renames(self):
        old_directory = self.folder / "old-directory"
        new_directory = self.folder / "new-directory"
        nested_directory = old_directory / "nested"
        nested_directory.mkdir(parents=True)
        (old_directory / "first.txt").write_text("first", encoding="utf-8")
        (nested_directory / "second.txt").write_text("second", encoding="utf-8")
        update_snapshot(
            str(self.folder), str(self.database), hash_mode="changed"
        )

        old_directory.rename(new_directory)
        with patch(
            "database.calculate_stable_hash",
            side_effect=AssertionError("renamed descendants were rehashed"),
        ):
            result = update_snapshot(
                str(self.folder), str(self.database), hash_mode="changed"
            )

        self.assert_change_counts(result, rename=1)
        self.assertEqual(
            [(change.previous_path, change.path) for change in result.changes],
            [("old-directory", "new-directory")],
        )

    def test_directory_rename_collapse_handles_interleaved_sibling_names(self):
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

    def test_modified_file_inside_renamed_directory_reports_modify(self):
        old_directory = self.folder / "old-directory"
        new_directory = self.folder / "new-directory"
        old_directory.mkdir()
        old_file = old_directory / "file.txt"
        old_file.write_text("before", encoding="utf-8")
        self.scan()

        old_directory.rename(new_directory)
        (new_directory / "file.txt").write_text("after", encoding="utf-8")
        result = self.scan()

        self.assert_change_counts(result, modify=1, rename=1)
        self.assertEqual(
            {
                (change.change_type, change.path, change.previous_path)
                for change in result.changes
            },
            {
                ("rename", "new-directory", "old-directory"),
                (
                    "modify",
                    os.path.join("new-directory", "file.txt"),
                    os.path.join("old-directory", "file.txt"),
                ),
            },
        )

    def test_same_path_identity_replacement_reports_delete_and_create(self):
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

    def test_file_to_directory_replacement_reports_delete_and_create(self):
        entry_path = self.folder / "entry"
        entry_path.write_text("file entity", encoding="utf-8")
        self.scan()

        entry_path.unlink()
        entry_path.mkdir()

        result = self.scan()
        self.assert_change_counts(result, create=1, delete=1)

    def test_changed_mode_reuses_an_unchanged_hash(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        update_snapshot(
            str(self.folder), str(self.database), hash_mode="changed"
        )

        with patch(
            "database.calculate_stable_hash",
            side_effect=AssertionError("unchanged file was rehashed"),
        ):
            result = update_snapshot(
                str(self.folder), str(self.database), hash_mode="changed"
            )

        self.assert_change_counts(result)

    def test_changed_mode_reuses_hash_across_rename(self):
        old_path = self.folder / "old.txt"
        new_path = self.folder / "new.txt"
        old_path.write_text("content", encoding="utf-8")
        update_snapshot(
            str(self.folder), str(self.database), hash_mode="changed"
        )
        old_path.rename(new_path)

        with patch(
            "database.calculate_stable_hash",
            side_effect=AssertionError("renamed file was rehashed"),
        ):
            result = update_snapshot(
                str(self.folder), str(self.database), hash_mode="changed"
            )

        self.assert_change_counts(result, rename=1)

    def test_changed_mode_retries_if_a_reused_file_disappears(self):
        file_path = self.folder / "disappearing.txt"
        file_path.write_text("content", encoding="utf-8")
        update_snapshot(
            str(self.folder), str(self.database), hash_mode="changed"
        )

        def disappearing_scan(folder_path):
            for record in scan_folder(folder_path):
                yield record
                if record[0] == "disappearing.txt" and file_path.exists():
                    file_path.unlink()

        with patch("database.scan_folder", side_effect=disappearing_scan):
            result = update_snapshot(
                str(self.folder), str(self.database), hash_mode="changed"
            )

        self.assert_change_counts(result, delete=1)
        with sqlite3.connect(self.database) as connection:
            entry_count = connection.execute(
                "SELECT COUNT(*) FROM entries"
            ).fetchone()[0]
        self.assertEqual(entry_count, 0)

    def test_off_mode_does_not_hash_files(self):
        (self.folder / "metadata-only.txt").write_text("content", encoding="utf-8")

        with patch(
            "database.calculate_stable_hash",
            side_effect=AssertionError("off mode attempted to hash a file"),
        ):
            result = update_snapshot(
                str(self.folder), str(self.database), hash_mode="off"
            )

        self.assert_change_counts(result, create=1)
        with sqlite3.connect(self.database) as connection:
            content_hash = connection.execute(
                "SELECT content_hash FROM entries WHERE path = 'metadata-only.txt'"
            ).fetchone()[0]
        self.assertIsNone(content_hash)

    def test_off_mode_preserves_a_still_valid_hash(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()

        with sqlite3.connect(self.database) as connection:
            original_hash = connection.execute(
                "SELECT content_hash FROM entries WHERE path = 'stable.txt'"
            ).fetchone()[0]

        result = update_snapshot(
            str(self.folder), str(self.database), hash_mode="off"
        )
        self.assert_change_counts(result)

        with sqlite3.connect(self.database) as connection:
            preserved_hash = connection.execute(
                "SELECT content_hash FROM entries WHERE path = 'stable.txt'"
            ).fetchone()[0]
        self.assertEqual(preserved_hash, original_hash)

    def test_off_mode_invalidates_hash_when_metadata_changes(self):
        file_path = self.folder / "changed.txt"
        file_path.write_text("content", encoding="utf-8")
        self.scan()

        original_stat = file_path.stat()
        self.assert_mtime_changed(file_path, original_stat)
        result = update_snapshot(
            str(self.folder), str(self.database), hash_mode="off"
        )

        self.assert_change_counts(result, modify=1)
        with sqlite3.connect(self.database) as connection:
            stored_hash = connection.execute(
                "SELECT content_hash FROM entries WHERE path = 'changed.txt'"
            ).fetchone()[0]
        self.assertIsNone(stored_hash)

    def test_always_mode_can_audit_hidden_change_after_off_mode(self):
        file_path = self.folder / "hidden-change.txt"
        file_path.write_text("AAAA", encoding="utf-8")
        self.scan()
        original_stat = file_path.stat()

        file_path.write_text("BBBB", encoding="utf-8")
        os.utime(
            file_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        off_result = update_snapshot(
            str(self.folder), str(self.database), hash_mode="off"
        )
        self.assert_change_counts(off_result)

        always_result = self.scan()
        self.assert_change_counts(always_result, modify=1)

    def test_enabling_hashing_does_not_report_a_false_modification(self):
        (self.folder / "legacy.txt").write_text("content", encoding="utf-8")
        update_snapshot(
            str(self.folder), str(self.database), hash_mode="off"
        )

        result = update_snapshot(
            str(self.folder), str(self.database), hash_mode="always"
        )

        self.assert_change_counts(result)

    def test_hash_permission_error_keeps_previous_snapshot(self):
        (self.folder / "protected.txt").write_text("content", encoding="utf-8")
        self.scan()

        with patch(
            "database.calculate_stable_hash",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(PermissionError):
                self.scan()

        result = self.scan()
        self.assert_change_counts(result)

    def test_database_inside_scanned_folder_is_rejected(self):
        database_inside_folder = self.folder / "snapshot.db"

        with self.assertRaises(ValueError):
            update_snapshot(str(self.folder), str(database_inside_folder))

        self.assertFalse(database_inside_folder.exists())

    def test_inside_database_with_alternate_case_is_rejected(self):
        alternate_folder = self.folder.with_name(self.folder.name.upper())
        if not alternate_folder.is_dir() or alternate_folder == self.folder:
            self.skipTest("Filesystem is case-sensitive")
        database_inside_folder = alternate_folder / "snapshot.db"

        with self.assertRaises(ValueError):
            update_snapshot(str(self.folder), str(database_inside_folder))

        self.assertFalse(database_inside_folder.exists())

    def test_database_cannot_be_reused_for_a_different_root(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()
        other_folder = Path(self.temporary_directory.name) / "other-folder"
        other_folder.mkdir()

        with self.assertRaises(ValueError):
            update_snapshot(str(other_folder), str(self.database))

        result = self.scan()
        self.assert_change_counts(result)

    def test_populated_database_without_root_metadata_is_rejected(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "DELETE FROM scan_metadata WHERE key = 'scanned_root'"
            )

        with self.assertRaisesRegex(ValueError, "no scanned-root metadata"):
            self.scan()

        with sqlite3.connect(self.database) as connection:
            stored_paths = connection.execute(
                "SELECT path FROM entries ORDER BY path"
            ).fetchall()
        self.assertEqual(stored_paths, [("stable.txt",)])

    def test_database_stores_its_canonical_scanned_root(self):
        self.scan()

        with sqlite3.connect(self.database) as connection:
            stored_root = connection.execute(
                "SELECT value FROM scan_metadata WHERE key = 'scanned_root'"
            ).fetchone()[0]

        expected_root = os.path.normcase(os.path.realpath(self.folder))
        self.assertEqual(stored_root, expected_root)

    def test_equivalent_case_spelling_of_root_is_accepted(self):
        self.scan()
        alternate_folder = self.folder.with_name(self.folder.name.upper())
        if not alternate_folder.is_dir() or alternate_folder == self.folder:
            self.skipTest("Filesystem is case-sensitive")

        result = update_snapshot(str(alternate_folder), str(self.database))
        self.assert_change_counts(result)

    def test_disappearing_root_aborts_and_keeps_previous_snapshot(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()

        with patch("scanner.os.scandir", side_effect=FileNotFoundError("gone")):
            with self.assertRaises(FileNotFoundError):
                self.scan()

        result = self.scan()
        self.assert_change_counts(result)

    def test_one_disappearing_root_attempt_is_retried(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()
        real_scandir = os.scandir
        call_count = 0

        def flaky_scandir(path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise FileNotFoundError("temporary disappearance")
            return real_scandir(path)

        with patch("scanner.os.scandir", side_effect=flaky_scandir):
            result = self.scan()

        self.assertGreaterEqual(call_count, 2)
        self.assert_change_counts(result)

    def test_partial_snapshot_is_rolled_back_before_retry(self):
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

    def test_changed_root_identity_retries_the_complete_scan(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()
        actual_stat = self.folder.stat()
        actual_identity = (actual_stat.st_dev, actual_stat.st_ino)
        changed_identity = (actual_stat.st_dev, actual_stat.st_ino + 1)

        with patch(
            "database.root_identity",
            side_effect=[
                actual_identity,
                changed_identity,
                actual_identity,
                actual_identity,
            ],
        ) as mocked_identity:
            result = self.scan()

        self.assertEqual(mocked_identity.call_count, 4)
        self.assert_change_counts(result)

    def test_unchanged_scan_does_not_rewrite_persistent_entries(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()
        with sqlite3.connect(self.database) as connection:
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
        with sqlite3.connect(self.database) as connection:
            write_count = connection.execute(
                "SELECT COUNT(*) FROM entry_write_audit"
            ).fetchone()[0]
        self.assertEqual(write_count, 0)

    def test_disappearing_hash_target_aborts_and_keeps_previous_snapshot(self):
        (self.folder / "stable.txt").write_text("content", encoding="utf-8")
        self.scan()

        with patch(
            "database.calculate_stable_hash",
            side_effect=FileNotFoundError("gone"),
        ):
            with self.assertRaises(FileNotFoundError):
                self.scan()

        result = self.scan()
        self.assert_change_counts(result)

    def test_zero_change_display_limit_does_not_claim_there_are_no_changes(self):
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
                "always",
                0.1,
            )

        self.assertIn("Created  : 1", output.getvalue())
        self.assertNotIn("No changes detected.", output.getvalue())

    def test_stable_hash_rejects_changed_device_at_final_path_check(self):
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


if __name__ == "__main__":
    unittest.main()
