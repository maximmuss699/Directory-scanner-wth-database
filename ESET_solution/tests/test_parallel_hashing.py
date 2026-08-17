import os
import sqlite3
import threading
from contextlib import closing
from unittest.mock import patch

from hashing import FileChangedDuringHashingError, calculate_stable_hash
from scanner import scan_folder
from tests.support import SnapshotTestCase


class ParallelHashingTests(SnapshotTestCase):
    """Test workload thresholds, worker isolation, and failure rollback."""

    def test_small_hash_workload_stays_sequential(self):
        """Check that small scans do not create a worker pool."""
        (self.folder / "small.txt").write_text("content", encoding="utf-8")

        with patch("database.ThreadPoolExecutor") as executor:
            result = self.scan()

        executor.assert_not_called()
        self.assert_change_counts(result, create=1)

    def test_parallel_hashing_succeeds_on_worker_threads(self):
        """Check workers only hash while the main thread updates SQLite."""
        for index in range(4):
            (self.folder / f"file-{index}.txt").write_text(
                f"content-{index}", encoding="utf-8"
            )

        main_thread_id = threading.get_ident()
        worker_thread_ids = set()
        worker_ids_lock = threading.Lock()

        def record_worker(file_path):
            with worker_ids_lock:
                worker_thread_ids.add(threading.get_ident())
            return calculate_stable_hash(file_path)

        with patch("database.PARALLEL_HASH_THRESHOLD", 1):
            with patch("database.HASH_BATCH_SIZE", 2):
                with patch(
                    "database.calculate_stable_hash",
                    side_effect=record_worker,
                ) as mocked_hash:
                    result = self.scan()

        self.assert_change_counts(result, create=4)
        self.assertEqual(mocked_hash.call_count, 4)
        self.assertTrue(worker_thread_ids)
        self.assertNotIn(main_thread_id, worker_thread_ids)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            hashed_count = connection.execute(
                "SELECT COUNT(*) FROM entries WHERE content_hash IS NOT NULL"
            ).fetchone()[0]
        self.assertEqual(hashed_count, 4)

    def test_parallel_worker_failure_keeps_previous_snapshot(self):
        """Check worker failures retry and roll back the snapshot transaction."""
        stable_path = self.folder / "stable.txt"
        stable_path.write_text("original", encoding="utf-8")
        self.scan()

        stable_path.write_text("modified content", encoding="utf-8")
        (self.folder / "created.txt").write_text("new", encoding="utf-8")

        def fail_modified_file(file_path):
            if os.path.basename(file_path) == "stable.txt":
                raise FileChangedDuringHashingError("worker failed")
            return calculate_stable_hash(file_path)

        with patch("database.PARALLEL_HASH_THRESHOLD", 1):
            with patch("database.HASH_BATCH_SIZE", 1):
                with patch(
                    "database.calculate_stable_hash",
                    side_effect=fail_modified_file,
                ) as mocked_hash:
                    with self.assertRaisesRegex(
                        FileChangedDuringHashingError, "worker failed"
                    ):
                        self.scan()

        # A completed batch must also disappear when a later worker fails.
        self.assertGreaterEqual(mocked_hash.call_count, 4)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            stored_paths = connection.execute(
                "SELECT path FROM entries ORDER BY path"
            ).fetchall()
        self.assertEqual(stored_paths, [("stable.txt",)])

        result = self.scan()
        self.assert_change_counts(result, create=1, modify=1)

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
