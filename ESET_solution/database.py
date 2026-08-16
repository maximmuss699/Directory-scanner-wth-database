import os
import sqlite3
import stat as stat_module
from typing import Tuple

from changes import build_entity_matches, detect_changes
from hashing import FileChangedDuringHashingError, calculate_stable_hash
from models import Change, ScanResult
from scanner import scan_folder


SCAN_ATTEMPTS = 2
UNSTABLE_SCAN_ERRORS = (
    FileNotFoundError,
    NotADirectoryError,
    IsADirectoryError,
    FileChangedDuringHashingError,
)


def canonical_path(path: str) -> str:
    """Normalize a path."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def paths_refer_to_same_location(first: str, second: str) -> bool:
    """Check whether two paths point to one place."""
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def path_is_inside(path: str, directory: str) -> bool:
    """Check whether a path is inside a folder."""
    try:
        if os.path.commonpath((path, directory)) == directory:
            return True
    except ValueError:
        return False

    candidate = path if os.path.isdir(path) else os.path.dirname(path)
    while candidate:
        try:
            if os.path.samefile(candidate, directory):
                return True
        except OSError:
            pass

        parent = os.path.dirname(candidate)
        if parent == candidate:
            return False
        candidate = parent

    return False


def root_identity(folder_path: str) -> Tuple[int, int]:
    """Return the root folder identity."""
    root_stat = os.stat(folder_path)
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise NotADirectoryError(folder_path)
    return root_stat.st_dev, root_stat.st_ino


def prepare_tables(connection: sqlite3.Connection, scanned_root: str) -> None:
    """Create the database tables."""

    # Table for consistently storing the current snapshot of the scanned directory.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            path TEXT PRIMARY KEY,
            parent_path TEXT NOT NULL,
            name TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            content_hash BLOB
        ) WITHOUT ROWID
        """
    )

    # Store which root directory belongs to this database.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )

    # Take first row from scan_metadata
    stored_root_row = connection.execute(
        "SELECT value FROM scan_metadata WHERE key = 'scanned_root'").fetchone()

    # Initialize the database root on first use and reject snapshots from a different directory.
    if stored_root_row is None:
        has_existing_snapshot = connection.execute("SELECT EXISTS(SELECT 1 FROM entries)").fetchone()[0]

        if has_existing_snapshot:
            raise ValueError(
                "Existing database has entries but no scanned-root metadata. "
                "Use a new database or reset it explicitly."
            )
        connection.execute("INSERT INTO scan_metadata (key, value) VALUES ('scanned_root', ?)",(scanned_root,),)
    # If the database already has a scanned_root, check if it matches the current scanned_root.
    elif not paths_refer_to_same_location(stored_root_row[0], scanned_root):
        raise ValueError(
            "Database belongs to a different scanned directory: "
            f"{stored_root_row[0]}"
        )

    
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_entries_file_identity
        ON entries(device_id, file_id)
        """
    )

    # Create a temporary table to hold the current snapshot of the scanned directory.
    connection.execute(
        """
        CREATE TEMP TABLE entries_snapshot (
            path TEXT PRIMARY KEY,
            parent_path TEXT NOT NULL,
            name TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            content_hash BLOB
        ) WITHOUT ROWID
        """
    )


def reuse_snapshot_hashes(connection: sqlite3.Connection) -> None:
    """Reuse valid file hashes."""
    connection.execute(
        """
        CREATE TEMP TABLE reusable_hashes (
            path TEXT PRIMARY KEY,
            content_hash BLOB NOT NULL
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        INSERT INTO reusable_hashes (path, content_hash)
        SELECT snapshot.path, previous.content_hash
        FROM entries_snapshot AS snapshot
        JOIN entries AS previous ON previous.path = snapshot.path
        WHERE snapshot.entry_type = 'file'
          AND previous.entry_type = 'file'
          AND previous.size = snapshot.size
          AND previous.modified_ns = snapshot.modified_ns
          AND previous.device_id = snapshot.device_id
          AND previous.file_id = snapshot.file_id
          AND previous.content_hash IS NOT NULL
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO reusable_hashes (path, content_hash)
        WITH previous_unique AS (
            SELECT device_id, file_id, MIN(path) AS path
            FROM entries
            WHERE entry_type = 'file' AND file_id != '0'
            GROUP BY device_id, file_id
            HAVING COUNT(*) = 1
        ),
        snapshot_unique AS (
            SELECT device_id, file_id, MIN(path) AS path
            FROM entries_snapshot
            WHERE entry_type = 'file' AND file_id != '0'
            GROUP BY device_id, file_id
            HAVING COUNT(*) = 1
        )
        SELECT snapshot.path, previous.content_hash
        FROM previous_unique
        JOIN snapshot_unique USING (device_id, file_id)
        JOIN entries AS previous ON previous.path = previous_unique.path
        JOIN entries_snapshot AS snapshot ON snapshot.path = snapshot_unique.path
        WHERE previous.size = snapshot.size
          AND previous.modified_ns = snapshot.modified_ns
          AND previous.content_hash IS NOT NULL
        """
    )
    connection.execute(
        """
        UPDATE entries_snapshot
        SET content_hash = (
            SELECT reusable.content_hash
            FROM reusable_hashes AS reusable
            WHERE reusable.path = entries_snapshot.path
        )
        WHERE EXISTS (
            SELECT 1 FROM reusable_hashes AS reusable
            WHERE reusable.path = entries_snapshot.path
        )
        """
    )
    connection.execute("DROP TABLE reusable_hashes")


def validate_reused_files(connection: sqlite3.Connection, folder_path: str) -> None:
    """Check files whose old hashes were reused."""
    query = """
        SELECT path, size, modified_ns, device_id, file_id
        FROM entries_snapshot
        WHERE entry_type = 'file' AND content_hash IS NOT NULL
    """

    for relative_path, size, modified_ns, device_id, file_id in connection.execute(
        query
    ):
        file_path = os.path.join(folder_path, relative_path)
        current_stat = os.stat(file_path, follow_symlinks=False)
        if (
            not stat_module.S_ISREG(current_stat.st_mode)
            or current_stat.st_size != size
            or current_stat.st_mtime_ns != modified_ns
            or str(current_stat.st_dev) != device_id
            or str(current_stat.st_ino) != file_id
        ):
            raise FileChangedDuringHashingError(
                f"File changed after it was scanned: {file_path}"
            )


def hash_snapshot_files(connection: sqlite3.Connection, folder_path: str) -> None:
    """Reuse stable hashes and hash the remaining files."""
    reuse_snapshot_hashes(connection)
    validate_reused_files(connection, folder_path)

    connection.execute(
        """
        CREATE TEMP TABLE files_to_hash (
            path TEXT PRIMARY KEY
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        INSERT INTO files_to_hash (path)
        SELECT path FROM entries_snapshot
        WHERE entry_type = 'file' AND content_hash IS NULL
        """
    )

    for (relative_path,) in connection.execute("SELECT path FROM files_to_hash"):
        file_path = os.path.join(folder_path, relative_path)
        content_hash, entry_stat = calculate_stable_hash(file_path)
        connection.execute(
            """
            UPDATE entries_snapshot
            SET size = ?, modified_ns = ?, device_id = ?, file_id = ?,
                content_hash = ?
            WHERE path = ?
            """,
            (
                entry_stat.st_size,
                entry_stat.st_mtime_ns,
                str(entry_stat.st_dev),
                str(entry_stat.st_ino),
                content_hash,
                relative_path,
            ),
        )

    connection.execute("DROP TABLE files_to_hash")


def load_snapshot(connection: sqlite3.Connection, folder_path: str) -> int:
    """Scan the folder into a new snapshot."""
    connection.executemany(
        """
        INSERT INTO entries_snapshot (
            path, parent_path, name, entry_type, size, modified_ns,
            device_id, file_id, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        scan_folder(folder_path),
    )
    connection.execute(
        """
        CREATE INDEX idx_entries_snapshot_identity
        ON entries_snapshot(device_id, file_id)
        """
    )
    hash_snapshot_files(connection, folder_path)
    build_entity_matches(connection)
    return connection.execute(
        "SELECT COUNT(*) FROM entries_snapshot"
    ).fetchone()[0]


def synchronize_current_entries(connection: sqlite3.Connection) -> None:
    """Save the new snapshot."""
    connection.execute(
        """
        CREATE TEMP TABLE entries_to_write (
            path TEXT PRIMARY KEY
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        INSERT INTO entries_to_write (path)
        SELECT snapshot.path
        FROM entries_snapshot AS snapshot
        LEFT JOIN entries AS previous ON previous.path = snapshot.path
        WHERE previous.path IS NULL
           OR previous.parent_path IS NOT snapshot.parent_path
           OR previous.name IS NOT snapshot.name
           OR previous.entry_type IS NOT snapshot.entry_type
           OR previous.size IS NOT snapshot.size
           OR previous.modified_ns IS NOT snapshot.modified_ns
           OR previous.device_id IS NOT snapshot.device_id
           OR previous.file_id IS NOT snapshot.file_id
           OR previous.content_hash IS NOT snapshot.content_hash
        """
    )
    connection.execute(
        """
        DELETE FROM entries
        WHERE NOT EXISTS (
            SELECT 1 FROM entries_snapshot AS snapshot
            WHERE snapshot.path = entries.path
        )
        """
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO entries (
            path, parent_path, name, entry_type, size, modified_ns,
            device_id, file_id, content_hash
        )
        SELECT
            snapshot.path, snapshot.parent_path, snapshot.name,
            snapshot.entry_type, snapshot.size, snapshot.modified_ns,
            snapshot.device_id, snapshot.file_id, snapshot.content_hash
        FROM entries_snapshot AS snapshot
        JOIN entries_to_write AS write ON write.path = snapshot.path
        """
    )
    connection.execute("DROP TABLE entries_to_write")


def load_stable_snapshot(connection: sqlite3.Connection,folder_path: str,) -> int:
    """Load a stable snapshot with one retry."""
    for attempt in range(SCAN_ATTEMPTS):
        connection.execute("SAVEPOINT scan_attempt")
        try:
            identity_before = root_identity(folder_path)
            entry_count = load_snapshot(connection, folder_path)
            identity_after = root_identity(folder_path)
            if identity_before != identity_after:
                raise FileChangedDuringHashingError(
                    "Root directory changed while it was being scanned"
                )
        except UNSTABLE_SCAN_ERRORS:
            connection.execute("ROLLBACK TO scan_attempt")
            connection.execute("RELEASE scan_attempt")
            if attempt + 1 == SCAN_ATTEMPTS:
                raise
        else:
            connection.execute("RELEASE scan_attempt")
            return entry_count

    raise RuntimeError("Snapshot scan did not finish")


def update_snapshot(
    folder_path: str,
    database_path: str,
    change_limit: int = 20,
) -> ScanResult:
    """Scan a folder and update its snapshot."""
    # 1. Prepare and check input.
    folder_path = os.path.abspath(folder_path)
    database_path = os.path.abspath(database_path)

    if not os.path.isdir(folder_path):
        raise ValueError(f"Directory does not exist: {folder_path}")
    if change_limit < 0:
        raise ValueError("Change limit cannot be negative")

    # 2. Keep the database outside the scanned folder.
    scanned_root = canonical_path(folder_path)
    canonical_database = canonical_path(database_path)
    if path_is_inside(canonical_database, scanned_root):
        raise ValueError("SQLite database must be outside the scanned directory")

    # 3. Open the database and prepare its tables.
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        prepare_tables(connection, scanned_root)

        # 4. Load a stable snapshot.
        entry_count = load_stable_snapshot(connection, folder_path)

        # 5. Detect changes and load a short result list.
        change_counts = detect_changes(connection)
        change_rows = connection.execute(
            """
            SELECT change_type, path, previous_path
            FROM detected_changes
            ORDER BY change_type, path, previous_path
            LIMIT ?
            """,
            (change_limit,),
        ).fetchall()
        changes = [Change(*row) for row in change_rows]

        # 6. Save the new current state.
        synchronize_current_entries(connection)
        connection.commit()
    except Exception:
        # 7. Keep the old state if anything fails.
        connection.rollback()
        raise
    finally:
        # 8. Always close the database.
        connection.close()

    # 9. Return the summary to the CLI.
    return ScanResult(entry_count, change_counts, changes)
