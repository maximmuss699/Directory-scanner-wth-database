import os
import sqlite3
from typing import Dict

from hashing import HASH_MODES, calculate_stable_hash
from models import Change, ScanResult
from scanner import scan_folder


def canonical_path(path: str) -> str:
    """Return a stable path spelling for storing the scanned root."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def paths_refer_to_same_location(first: str, second: str) -> bool:
    """Compare paths while respecting filesystem aliases and case rules."""
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def prepare_tables(
    connection: sqlite3.Connection, scanned_root: str
) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            path TEXT PRIMARY KEY,
            parent_path TEXT NOT NULL,
            name TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            device_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            content_hash BLOB
        ) WITHOUT ROWID
        """
    )
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(entries)")
    }
    if "content_hash" not in existing_columns:
        # Migrate databases created by the metadata-only version.
        connection.execute("ALTER TABLE entries ADD COLUMN content_hash BLOB")

    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_path)"
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_entries_file_identity
        ON entries(device_id, file_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )

    stored_root_row = connection.execute(
        "SELECT value FROM scan_metadata WHERE key = 'scanned_root'"
    ).fetchone()
    if stored_root_row is None:
        has_existing_snapshot = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM entries)"
        ).fetchone()[0]
        if has_existing_snapshot:
            raise ValueError(
                "Existing database has entries but no scanned-root metadata. "
                "Use a new database or reset it explicitly."
            )

        connection.execute(
            "INSERT INTO scan_metadata (key, value) VALUES ('scanned_root', ?)",
            (scanned_root,),
        )
    elif not paths_refer_to_same_location(stored_root_row[0], scanned_root):
        raise ValueError(
            "Database belongs to a different scanned directory: "
            f"{stored_root_row[0]}"
        )

    # Keep the new scan separate from the previous state during comparison.
    connection.execute(
        """
        CREATE TEMP TABLE entries_snapshot (
            path TEXT PRIMARY KEY,
            parent_path TEXT NOT NULL,
            name TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            device_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            content_hash BLOB
        ) WITHOUT ROWID
        """
    )


def hash_snapshot_files(
    connection: sqlite3.Connection, folder_path: str, hash_mode: str
) -> None:
    if hash_mode == "off":
        return

    if hash_mode == "changed":
        # Reuse hashes without loading the previous snapshot into Python memory.
        connection.execute(
            """
            UPDATE entries_snapshot
            SET content_hash = (
                SELECT previous.content_hash
                FROM entries AS previous
                WHERE previous.path = entries_snapshot.path
            )
            WHERE entry_type = 'file'
              AND EXISTS (
                  SELECT 1
                  FROM entries AS previous
                  WHERE previous.path = entries_snapshot.path
                    AND previous.entry_type = 'file'
                    AND previous.size = entries_snapshot.size
                    AND previous.modified_ns = entries_snapshot.modified_ns
                    AND previous.device_id = entries_snapshot.device_id
                    AND previous.file_id = entries_snapshot.file_id
                    AND previous.content_hash IS NOT NULL
              )
            """
        )

    connection.execute(
        """
        CREATE TEMP TABLE files_to_hash (
            path TEXT PRIMARY KEY
        ) WITHOUT ROWID
        """
    )
    if hash_mode == "always":
        connection.execute(
            """
            INSERT INTO files_to_hash (path)
            SELECT path
            FROM entries_snapshot
            WHERE entry_type = 'file'
            """
        )
    else:
        connection.execute(
            """
            INSERT INTO files_to_hash (path)
            SELECT path
            FROM entries_snapshot
            WHERE entry_type = 'file'
              AND content_hash IS NULL
            """
        )

    for (relative_path,) in connection.execute("SELECT path FROM files_to_hash"):
        file_path = os.path.join(folder_path, relative_path)

        try:
            content_hash, entry_stat = calculate_stable_hash(file_path)
        except FileNotFoundError:
            # The file disappeared after its metadata was staged.
            connection.execute(
                "DELETE FROM entries_snapshot WHERE path = ?", (relative_path,)
            )
            continue

        connection.execute(
            """
            UPDATE entries_snapshot
            SET size = ?,
                modified_ns = ?,
                device_id = ?,
                file_id = ?,
                content_hash = ?
            WHERE path = ?
            """,
            (
                entry_stat.st_size,
                entry_stat.st_mtime_ns,
                entry_stat.st_dev,
                entry_stat.st_ino,
                content_hash,
                relative_path,
            ),
        )


def load_snapshot(
    connection: sqlite3.Connection, folder_path: str, hash_mode: str
) -> int:
    connection.executemany(
        """
        INSERT INTO entries_snapshot (
            path, parent_path, name, entry_type, size, modified_ns,
            device_id, file_id, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        scan_folder(folder_path),
    )
    hash_snapshot_files(connection, folder_path, hash_mode)
    connection.execute(
        """
        CREATE INDEX idx_entries_snapshot_identity
        ON entries_snapshot(device_id, file_id)
        """
    )

    return connection.execute(
        "SELECT COUNT(*) FROM entries_snapshot"
    ).fetchone()[0]


def detect_changes(
    connection: sqlite3.Connection, compare_content_hashes: bool
) -> Dict[str, int]:
    connection.execute(
        """
        CREATE TEMP TABLE detected_changes (
            change_type TEXT NOT NULL,
            path TEXT NOT NULL,
            previous_path TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_detected_changes_path
        ON detected_changes(change_type, path)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_detected_changes_previous_path
        ON detected_changes(change_type, previous_path)
        """
    )

    # Hard links make an identity ambiguous, so only unique identities are renames.
    connection.execute(
        """
        INSERT INTO detected_changes (change_type, path, previous_path)
        WITH previous_identities AS (
            SELECT device_id, file_id
            FROM entries
            GROUP BY device_id, file_id
            HAVING COUNT(*) = 1
        ),
        snapshot_identities AS (
            SELECT device_id, file_id
            FROM entries_snapshot
            GROUP BY device_id, file_id
            HAVING COUNT(*) = 1
        )
        SELECT 'rename', snapshot.path, previous.path
        FROM entries AS previous
        JOIN entries_snapshot AS snapshot
          ON snapshot.device_id = previous.device_id
         AND snapshot.file_id = previous.file_id
        JOIN previous_identities
          ON previous_identities.device_id = previous.device_id
         AND previous_identities.file_id = previous.file_id
        JOIN snapshot_identities
          ON snapshot_identities.device_id = snapshot.device_id
         AND snapshot_identities.file_id = snapshot.file_id
        WHERE previous.path != snapshot.path
          AND previous.entry_type = snapshot.entry_type
        """
    )
    connection.execute(
        """
        INSERT INTO detected_changes (change_type, path)
        SELECT 'create', snapshot.path
        FROM entries_snapshot AS snapshot
        LEFT JOIN entries AS previous ON previous.path = snapshot.path
        WHERE previous.path IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM detected_changes AS change
              WHERE change.change_type = 'rename'
                AND change.path = snapshot.path
          )
        """
    )
    connection.execute(
        """
        INSERT INTO detected_changes (change_type, path)
        SELECT 'delete', previous.path
        FROM entries AS previous
        LEFT JOIN entries_snapshot AS snapshot ON snapshot.path = previous.path
        WHERE snapshot.path IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM detected_changes AS change
              WHERE change.change_type = 'rename'
                AND change.previous_path = previous.path
          )
        """
    )
    connection.execute(
        """
        INSERT INTO detected_changes (change_type, path)
        SELECT 'modify', snapshot.path
        FROM entries_snapshot AS snapshot
        JOIN entries AS previous ON previous.path = snapshot.path
        WHERE previous.entry_type != snapshot.entry_type
           OR previous.size != snapshot.size
           OR previous.modified_ns != snapshot.modified_ns
           OR previous.device_id != snapshot.device_id
           OR previous.file_id != snapshot.file_id
           OR (
               ?
               AND previous.content_hash IS NOT NULL
               AND snapshot.content_hash IS NOT NULL
               AND previous.content_hash IS NOT snapshot.content_hash
           )
        """,
        (compare_content_hashes,),
    )
    connection.execute(
        """
        INSERT INTO detected_changes (change_type, path, previous_path)
        SELECT 'modify', snapshot.path, previous.path
        FROM detected_changes AS rename
        JOIN entries AS previous ON previous.path = rename.previous_path
        JOIN entries_snapshot AS snapshot ON snapshot.path = rename.path
        WHERE rename.change_type = 'rename'
          AND (
              previous.size != snapshot.size
              OR previous.modified_ns != snapshot.modified_ns
              OR (
                  ?
                  AND previous.content_hash IS NOT NULL
                  AND snapshot.content_hash IS NOT NULL
                  AND previous.content_hash IS NOT snapshot.content_hash
              )
          )
        """,
        (compare_content_hashes,),
    )

    change_counts = {"create": 0, "delete": 0, "modify": 0, "rename": 0}
    for change_type, count in connection.execute(
        """
        SELECT change_type, COUNT(*)
        FROM detected_changes
        GROUP BY change_type
        """
    ):
        change_counts[change_type] = count

    return change_counts


def replace_current_entries(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM entries")
    connection.execute(
        """
        INSERT INTO entries (
            path, parent_path, name, entry_type, size, modified_ns,
            device_id, file_id, content_hash
        )
        SELECT
            path, parent_path, name, entry_type, size, modified_ns,
            device_id, file_id, content_hash
        FROM entries_snapshot
        """
    )


def update_snapshot(
    folder_path: str,
    database_path: str,
    change_limit: int = 20,
    hash_mode: str = "always", ) -> ScanResult:
    
    folder_path = os.path.abspath(folder_path)
    database_path = os.path.abspath(database_path)

    if not os.path.isdir(folder_path):
        raise ValueError(f"Directory does not exist: {folder_path}")
    if change_limit < 0:
        raise ValueError("Change limit cannot be negative")
    if hash_mode not in HASH_MODES:
        raise ValueError(f"Unknown hash mode: {hash_mode}")

    scanned_root = canonical_path(folder_path)
    with sqlite3.connect(database_path) as connection:
        prepare_tables(connection, scanned_root)
        entry_count = load_snapshot(connection, folder_path, hash_mode)
        change_counts = detect_changes(connection, hash_mode != "off")

        change_rows = connection.execute(
            """
            SELECT change_type, path, previous_path
            FROM detected_changes
            ORDER BY change_type, path
            LIMIT ?
            """,
            (change_limit,),
        ).fetchall()
        changes = [Change(*row) for row in change_rows]

        # This runs in the same transaction as the scan and comparison.
        replace_current_entries(connection)

    return ScanResult(entry_count, change_counts, changes)
