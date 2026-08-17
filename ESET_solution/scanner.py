import os
import stat as stat_module
from typing import Iterator

from models import EntryRecord


WINDOWS_REPARSE_POINT = getattr(
    stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
)


def is_directory_junction(entry: os.DirEntry, entry_stat: os.stat_result) -> bool:
    """Return whether a Windows directory entry must not be traversed."""
    if os.name != "nt":
        return False

    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None and is_junction():
        return True

    file_attributes = getattr(entry_stat, "st_file_attributes", 0)
    return (
        stat_module.S_ISDIR(entry_stat.st_mode)
        and bool(file_attributes & WINDOWS_REPARSE_POINT)
    )


def scan_folder(root_path: str) -> Iterator[EntryRecord]:
    """Scan a directory tree and yield one record for each entry."""
    # 1. Keep absolute and relative directory paths together. Building child
    # paths directly avoids normalizing both paths again for every entry.
    directories_to_scan = [(root_path, "")]
    root_stat = os.stat(root_path)
    visited_directories = set()

    # Avoid scanning the same directory twice, which can happen with junctions and hard links.
    if root_stat.st_ino != 0:
        visited_directories.add((root_stat.st_dev, root_stat.st_ino))

    while directories_to_scan:
        # 2. Take the next directory from the stack.
        current_directory, current_relative_path = directories_to_scan.pop()

        # 3. Read all entries in the directory.
        with os.scandir(current_directory) as entries:
            for entry in entries:
                entry_path = entry.path

                # 4. Read metadata and find the entry type.
                entry_stat = os.stat(entry_path, follow_symlinks=False)

                if is_directory_junction(entry, entry_stat):
                    entry_type = "junction"
                elif stat_module.S_ISLNK(entry_stat.st_mode):
                    entry_type = "symlink"
                elif stat_module.S_ISDIR(entry_stat.st_mode):
                    entry_type = "directory"
                elif stat_module.S_ISREG(entry_stat.st_mode):
                    entry_type = "file"
                else:
                    # Do not try to hash devices, sockets, or named pipes.
                    entry_type = "other"

                relative_path = (
                    os.path.join(current_relative_path, entry.name)
                    if current_relative_path
                    else entry.name
                )

                # 5. Send the entry metadata to SQLite.
                yield (
                    relative_path,
                    current_relative_path,
                    entry.name,
                    entry_type,
                    entry_stat.st_size,
                    entry_stat.st_mtime_ns,
                    str(entry_stat.st_dev),
                    str(entry_stat.st_ino),
                    None,
                )

                # 6. Add new directories to the stack.
                if entry_type == "directory":
                    directory_identity = (entry_stat.st_dev, entry_stat.st_ino)
                    if (
                        entry_stat.st_ino == 0
                        or directory_identity not in visited_directories
                    ):
                        if entry_stat.st_ino != 0:
                            visited_directories.add(directory_identity)
                        directories_to_scan.append((entry_path, relative_path))
