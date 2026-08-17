import hashlib
import stat as stat_module
from types import SimpleNamespace
from unittest.mock import patch

from hashing import calculate_stable_hash, same_file_state
from tests.support import SnapshotTestCase


class StableHashTests(SnapshotTestCase):
    """Test bounded reads and platform-specific stability checks."""

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
