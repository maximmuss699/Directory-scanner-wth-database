import hashlib
import os
import stat as stat_module
from typing import Tuple

HASH_CHUNK_SIZE = 1024 * 1024
HASH_DIGEST_SIZE = 32
HASH_ATTEMPTS = 2
HASH_MODES = ("always", "changed", "off")


class FileChangedDuringHashingError(RuntimeError):
    pass


def same_file_state(first: os.stat_result, second: os.stat_result) -> bool:
    """Return whether two stat results describe one stable regular file."""
    return (
        stat_module.S_ISREG(first.st_mode)
        and stat_module.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def calculate_stable_hash(file_path: str) -> Tuple[bytes, os.stat_result]:
    """Hash a file only when its identity and metadata stay stable."""
    # Retry once if the file changes during hashing.
    for attempt in range(HASH_ATTEMPTS):
        try:
            path_before = os.stat(file_path, follow_symlinks=False)
            if not stat_module.S_ISREG(path_before.st_mode):
                continue

            # Open the file and hash its contents using chunks.
            with open(file_path, "rb") as file:
                descriptor_before = os.fstat(file.fileno())
                if not stat_module.S_ISREG(descriptor_before.st_mode):
                    continue

                digest = hashlib.blake2b(digest_size=HASH_DIGEST_SIZE)
                while True:
                    chunk = file.read(HASH_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)

                # Recheck the open file after all bytes have been read.
                descriptor_after = os.fstat(file.fileno())

            # Confirm that the path still points to the file we just read.
            path_after = os.stat(file_path, follow_symlinks=False)
        except (FileNotFoundError, IsADirectoryError):
            if attempt + 1 == HASH_ATTEMPTS:
                raise
            continue

        if (
            same_file_state(path_before, descriptor_before)
            and same_file_state(descriptor_before, descriptor_after)
            and same_file_state(descriptor_after, path_after)
        ):
            return digest.digest(), path_after

    raise FileChangedDuringHashingError(
        f"File changed while it was being hashed: {file_path}"
    )
