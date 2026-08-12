import os
import sqlite3
import stat as stat_module


solution_directory = os.path.dirname(os.path.abspath(__file__))
folder_to_scan = os.path.abspath(os.path.join(solution_directory, "..", "folder"))
database_path = os.path.join(solution_directory, "scan_result.db")


def scan_folder(root_path):
    # Use our own stack so deeply nested folders do not require recursion
    directories_to_scan = [root_path]

    while directories_to_scan:
        current_directory = directories_to_scan.pop()

        with os.scandir(current_directory) as entries:
            for entry in entries:
                entry_path = entry.path

                try:
                    entry_stat = os.stat(entry_path, follow_symlinks=False)
                except FileNotFoundError:
                    # The entry disappeared after os.scandir() returned it
                    continue

                if stat_module.S_ISLNK(entry_stat.st_mode):
                    entry_type = "symlink"
                elif stat_module.S_ISDIR(entry_stat.st_mode):
                    entry_type = "directory"
                else:
                    entry_type = "file"

                # Store portable paths relative to the scanned root
                relative_path = os.path.relpath(entry_path, root_path)

                yield (
                    relative_path,
                    os.path.dirname(relative_path),
                    entry.name,
                    entry_type,
                    entry_stat.st_size,
                    entry_stat.st_mtime_ns,
                    entry_stat.st_dev,
                    entry_stat.st_ino,
                )

                if entry_type == "directory":
                    directories_to_scan.append(entry_path)


def main():
    if not os.path.isdir(folder_to_scan):
        raise ValueError(f"Directory does not exist: {folder_to_scan}")

    with sqlite3.connect(database_path) as connection:
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
                file_id INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        # Indexes to speed up queries on parent_path and (device_id, file_id)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_path)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_file_identity
            ON entries(device_id, file_id)
            """
        )

        # Replace the old snapshot with the current scan in one transaction
        connection.execute("DELETE FROM entries")
        connection.executemany(
            """
            INSERT INTO entries (
                path, parent_path, name, entry_type, size, modified_ns,
                device_id, file_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            scan_folder(folder_to_scan),
        )

        entry_count = connection.execute(
            "SELECT COUNT(*) FROM entries"
        ).fetchone()[0]

    print(f"Scanned {entry_count} entries")
    print(f"SQLite database saved to: {database_path}")


if __name__ == "__main__":
    main()
