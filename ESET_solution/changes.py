import os
import sqlite3
from typing import Dict


def build_entity_matches(connection: sqlite3.Connection) -> None:
    """Match old and new entries."""
    connection.execute("DROP TABLE IF EXISTS entity_matches")
    connection.execute(
        """
        CREATE TEMP TABLE entity_matches (
            previous_path TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        INSERT INTO entity_matches (previous_path, path)
        WITH previous_unique AS (
            SELECT device_id, file_id, MIN(path) AS path
            FROM entries
            WHERE file_id != '0'
            GROUP BY device_id, file_id
            HAVING COUNT(*) = 1
        ),
        snapshot_unique AS (
            SELECT device_id, file_id, MIN(path) AS path
            FROM entries_snapshot
            WHERE file_id != '0'
            GROUP BY device_id, file_id
            HAVING COUNT(*) = 1
        )
        SELECT previous.path, snapshot.path
        FROM previous_unique
        JOIN snapshot_unique USING (device_id, file_id)
        JOIN entries AS previous ON previous.path = previous_unique.path
        JOIN entries_snapshot AS snapshot ON snapshot.path = snapshot_unique.path
        WHERE previous.entry_type = snapshot.entry_type
        """
    )
    connection.execute(
        """
        INSERT INTO entity_matches (previous_path, path)
        SELECT previous.path, snapshot.path
        FROM entries AS previous
        JOIN entries_snapshot AS snapshot ON snapshot.path = previous.path
        WHERE previous.entry_type = snapshot.entry_type
          AND NOT EXISTS (
              SELECT 1 FROM entity_matches AS match
              WHERE match.previous_path = previous.path
          )
          AND NOT EXISTS (
              SELECT 1 FROM entity_matches AS match
              WHERE match.path = snapshot.path
          )
          AND (
              (
                  previous.device_id = snapshot.device_id
                  AND previous.file_id = snapshot.file_id
              )
              OR previous.file_id = '0'
              OR snapshot.file_id = '0'
          )
        """
    )


def insert_reported_renames(connection: sqlite3.Connection) -> None:
    """Add rename changes to the result."""
    candidates = connection.execute(
        """
        SELECT match.previous_path, match.path, previous.entry_type
        FROM entity_matches AS match
        JOIN entries AS previous ON previous.path = match.previous_path
        WHERE match.previous_path != match.path
        ORDER BY match.previous_path
        """
    )
    directory_mappings = {}
    rows_to_insert = []

    for previous_path, path, entry_type in candidates:
        implied = False
        ancestor = os.path.dirname(previous_path)
        while ancestor:
            mapped_ancestor = directory_mappings.get(ancestor)
            if mapped_ancestor is not None:
                suffix = previous_path[len(ancestor) :]
                if path == mapped_ancestor + suffix:
                    implied = True
                    break
            ancestor = os.path.dirname(ancestor)

        if implied:
            continue

        rows_to_insert.append(("rename", path, previous_path))
        if entry_type == "directory":
            directory_mappings[previous_path] = path

        if len(rows_to_insert) == 1000:
            connection.executemany(
                """
                INSERT INTO detected_changes (
                    change_type, path, previous_path
                ) VALUES (?, ?, ?)
                """,
                rows_to_insert,
            )
            rows_to_insert.clear()

    if rows_to_insert:
        connection.executemany(
            """
            INSERT INTO detected_changes (
                change_type, path, previous_path
            ) VALUES (?, ?, ?)
            """,
            rows_to_insert,
        )


def detect_changes(connection: sqlite3.Connection) -> Dict[str, int]:
    """Find changes between two snapshots."""
    connection.execute(
        """
        CREATE TEMP TABLE detected_changes (
            change_type TEXT NOT NULL,
            path TEXT NOT NULL,
            previous_path TEXT
        )
        """
    )
    insert_reported_renames(connection)
    connection.execute(
        """
        INSERT INTO detected_changes (change_type, path)
        SELECT 'create', snapshot.path
        FROM entries_snapshot AS snapshot
        WHERE NOT EXISTS (
            SELECT 1 FROM entity_matches AS match
            WHERE match.path = snapshot.path
        )
        """
    )
    connection.execute(
        """
        INSERT INTO detected_changes (change_type, path)
        SELECT 'delete', previous.path
        FROM entries AS previous
        WHERE NOT EXISTS (
            SELECT 1 FROM entity_matches AS match
            WHERE match.previous_path = previous.path
        )
        """
    )
    connection.execute(
        """
        INSERT INTO detected_changes (change_type, path, previous_path)
        SELECT
            'modify',
            snapshot.path,
            CASE
                WHEN match.previous_path != match.path THEN match.previous_path
            END
        FROM entity_matches AS match
        JOIN entries AS previous ON previous.path = match.previous_path
        JOIN entries_snapshot AS snapshot ON snapshot.path = match.path
        WHERE snapshot.entry_type = 'file'
          AND CASE
              WHEN previous.content_hash IS NOT NULL
               AND snapshot.content_hash IS NOT NULL
              THEN previous.content_hash IS NOT snapshot.content_hash
              ELSE previous.size != snapshot.size
                OR previous.modified_ns != snapshot.modified_ns
          END
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_detected_changes_path
        ON detected_changes(change_type, path)
        """
    )

    counts = {"create": 0, "delete": 0, "modify": 0, "rename": 0}
    for change_type, count in connection.execute(
        """
        SELECT change_type, COUNT(*)
        FROM detected_changes
        GROUP BY change_type
        """
    ):
        counts[change_type] = count
    return counts
