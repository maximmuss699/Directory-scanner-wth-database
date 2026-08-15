import os
import stat as stat_module
from typing import Iterator

from models import EntryRecord


def scan_folder(root_path: str) -> Iterator[EntryRecord]:
    # LIFO stack of directories. Start with the root directory to scan.
    directories_to_scan = [root_path]

    while directories_to_scan:
        current_directory = directories_to_scan.pop()

        # A disappearing directory aborts this attempt so it can be retried.
        with os.scandir(current_directory) as entries:
            for entry in entries:
                entry_path = entry.path

                # Do not silently skip an entry that vanishes during the scan.
                entry_stat = os.stat(entry_path, follow_symlinks=False)

                if stat_module.S_ISLNK(entry_stat.st_mode):
                    entry_type = "symlink"
                elif stat_module.S_ISDIR(entry_stat.st_mode):
                    entry_type = "directory"
                elif stat_module.S_ISREG(entry_stat.st_mode):
                    entry_type = "file"
                else:
                    # Do not try to hash devices, sockets, or named pipes.
                    entry_type = "other"

                relative_path = os.path.relpath(entry_path, root_path)

                # Hashing is handled after metadata has been staged in SQLite.
                yield (
                    relative_path,
                    os.path.dirname(relative_path),
                    entry.name,
                    entry_type,
                    entry_stat.st_size,
                    entry_stat.st_mtime_ns,
                    entry_stat.st_dev,
                    entry_stat.st_ino,
                    None,
                )

                # Add directories to the explicit traversal stack.
                if entry_type == "directory":
                    directories_to_scan.append(entry_path)
