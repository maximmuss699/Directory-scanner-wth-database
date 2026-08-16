# Directory Snapshot Scanner

## Assignment

Create a Python script that scans a given directory and stores its structure in
a relational database so that the database always represents the latest state.
The script must support repeated execution and detect:

- file creation (`create`);
- file deletion (`delete`);
- file renaming (`rename`);
- file content modification (`modify`).

The solution should be designed with performance in mind because a real
directory can contain millions of files and grow dynamically. Change history is
not required; only the current state must be preserved. The preferred target
operating system is Windows.

## Solution

A Python script that recursively scans a directory and stores its current state
in SQLite. Repeated runs compare the previous state with the latest scan and
detect created, deleted, modified, and renamed entries.

The project uses only the Python standard library and is intended to work on
Windows, macOS, and Linux.

## Project structure

```text
ESET_solution/
├── main.py       # Small command-line entry point
├── cli.py        # Arguments and terminal output
├── scanner.py    # Iterative filesystem traversal
├── hashing.py    # Stable BLAKE2b content hashing
├── database.py   # SQLite snapshots and transactions
├── changes.py    # Change detection and entity matching
├── models.py     # Shared result dataclasses and types
└── tests/        # Automated behavior tests
```

```mermaid
flowchart LR
    A[main.py] --> B[cli.py]
    B --> C[database.py]
    C --> G[changes.py]
    C --> D[scanner.py]
    C --> E[hashing.py]
    B --> F[models.py]
    C --> F
    D --> F
```

The implementation is functional. Small immutable dataclasses represent
results, while each module has one focused responsibility. This keeps the code
modular without adding unnecessary class hierarchies.

## How it works

```mermaid
flowchart TD
    A[Directory] --> B[Iterative scan with os.scandir]
    B --> H[Reuse valid hashes or hash file content]
    H --> C[(Temporary entries_snapshot)]
    D[(Previous entries)] --> M[Entity matching]
    C --> M
    M --> E[SQL comparison]
    C --> E
    E --> F[Detected changes]
    C --> G[Incrementally synchronize entries]
    G --> D
```

The scanner uses an explicit LIFO stack instead of recursion.
Symbolic links and Windows junction/reparse directories are stored as entries
but never traversed. A visited-directory identity set provides an additional
cycle guard.
The database update runs in a transaction. An observed filesystem race causes
one complete retry. If the retry or another operation fails, the previous valid
snapshot remains unchanged.

## Change detection

```mermaid
flowchart LR
    A[Previous entries] --> C{Compare}
    B[New snapshot] --> C
    C -->|Unmatched new entity| D[Create]
    C -->|Unmatched old entity| E[Delete]
    C -->|Matched file, changed content| F[Modify]
    C -->|Same unique identity, new path| G[Rename]
```

- **Create:** a new entity cannot be matched to a previous entity.
- **Delete:** a previous entity cannot be matched to a new entity.
- **Modify:** a matched regular file has different content. When both hashes
  are available, they are authoritative; otherwise size and modification time
  are used as a fallback.
- **Rename:** one unique, non-zero filesystem identity has a different path.

Rename detection is intentionally conservative. If an identity appears more
than once, as can happen with hard links, the script reports path-based create
and delete changes instead of guessing which path was renamed.

A reliable identity changing at the same path is represented as a delete and a
create. A renamed file whose content also changed produces both rename and
modify. A directory-tree move is shown as one directory rename; descendant path
changes implied by that move are not repeated in the terminal result.

## Database

The persistent `entries` table stores:

| Column | Description |
|---|---|
| `path` | Path relative to the scanned root |
| `parent_path` | Relative parent path |
| `name` | File or directory name |
| `entry_type` | `file`, `directory`, `symlink`, `junction`, or another type |
| `size` | Size in bytes |
| `modified_ns` | Modification time in nanoseconds |
| `device_id` | Filesystem device identifier stored as decimal text |
| `file_id` | Filesystem entry identifier stored as decimal text |
| `content_hash` | BLAKE2b-256 digest for regular files, stored as a BLOB |

`entries_snapshot`, `entity_matches`, and `detected_changes` are temporary
SQLite tables used only during a scan. `scan_metadata` binds the database to one
canonical scanned root. The database keeps the latest directory state, not a
history of changes.

The persistent table is synchronized incrementally. An unchanged scan compares
all staged rows but does not delete and reinsert all persistent rows.
Text storage safely represents Windows file IDs up to 128 bits without SQLite
integer overflow. Existing databases from older development versions are not
supported; use a new database file with this submission.

## Setup and usage

Create and activate a virtual environment on macOS or Linux:

```bash
python3 -m venv ESET_solution/.venv
source ESET_solution/.venv/bin/activate
```

On Windows PowerShell:

```powershell
py -m venv ESET_solution\.venv
ESET_solution\.venv\Scripts\Activate.ps1
```

Scan the supplied sample directory using the default database:

```bash
python ESET_solution/main.py
```

Scan another directory:

```bash
python ESET_solution/main.py /path/to/folder --database /path/to/state.db
```

The SQLite database must be outside the scanned directory. Reusing an existing
database for a different scanned root is rejected.

Show at most five detected changes:

```bash
python ESET_solution/main.py --show-changes 5
```

### Hashing strategy

The first scan hashes every regular file. Later scans reuse a stored hash when
the file identity, size, and modification time are unchanged. A hash can also
be reused after a rename when the filesystem identity is unique. New and
metadata-changed files are hashed again.

This single strategy balances content-based modification detection with the
performance required for large directory trees. Like any metadata shortcut, it
cannot detect a same-size content change when the modification time is also
deliberately restored to its previous value.

Example summary:

```text
Directory snapshot updated
==========================
Folder   : /path/to/folder
Database : /path/to/state.db
Duration : 2.50 s
Entries  : 49,102

Changes
--------------------------
Created  : 0
Deleted  : 0
Modified : 0
Renamed  : 0
Total    : 0

No changes detected.
```

Display all command-line options:

```bash
python ESET_solution/main.py --help
```

## Tests

Run the tests from the solution directory:

```bash
cd ESET_solution
python -m unittest discover -s tests -v
```

Tests cover create, delete, content modification, rename collisions and swaps,
same-path replacement, directory-tree renames, hard-link ambiguity, hash reuse,
database root binding, incremental writes, retry behavior, input validation,
stable hashing, Windows-sized file IDs, junction handling, and transaction
rollback.

## Stable hashing and performance

Regular files are read in 1 MiB chunks, so hashing uses constant memory. Before
and after reading, the hasher verifies file type, device ID, file ID, size,
modification time, and `ctime` using both `os.fstat()` and a final
`os.stat(path)`. The final path check catches a file being replaced while its
old open descriptor remains readable. An unstable file is retried once; the
complete directory scan is then retried once before the transaction is aborted.

No userspace scanner can eliminate a filesystem race after its final metadata
check. The first scan takes approximately
`O(number of entries + total file bytes)`. Later scans take approximately
`O(number of entries + bytes of files that need a new hash)`. Unchanged files
are checked with metadata and do not need to be read again.
