import argparse
import os
import time

from database import update_snapshot
from models import ScanResult


solution_directory = os.path.dirname(os.path.abspath(__file__))
default_folder = os.path.abspath(os.path.join(solution_directory, "..", "folder"))
default_database = os.path.join(solution_directory, "scan_result.db")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Store the current state of a directory in SQLite."
    )
    # By default, scan the sample folder in the parent directory of this script.
    parser.add_argument(
        "folder",
        nargs="?",
        default=default_folder,
        help="directory to scan (default: supplied sample folder)",
    )
    # Database path is optional, with a default in the same directory as this script.
    parser.add_argument(
        "--database",
        "-d",
        default=default_database,
        help="SQLite database path",
    )
    # Limit the number of changes to display, with a default of 20.
    parser.add_argument(
        "--show-changes",
        type=int,
        default=20,
        metavar="N",
        help="show at most N detected changes (default: 20)",
    )
    return parser.parse_args()


def print_result(
    result: ScanResult,
    folder_path: str,
    database_path: str,
    elapsed_seconds: float,
) -> None:
    total_changes = sum(result.change_counts.values())

    print("\nDirectory snapshot updated")
    print("=" * 26)
    print(f"Folder   : {os.path.abspath(folder_path)}")
    print(f"Database : {os.path.abspath(database_path)}")
    print(f"Duration : {elapsed_seconds:.2f} s")
    print(f"Entries  : {result.entry_count:,}")

    print("\nChanges")
    print("-" * 26)
    print(f"Created  : {result.change_counts['create']:,}")
    print(f"Deleted  : {result.change_counts['delete']:,}")
    print(f"Modified : {result.change_counts['modify']:,}")
    print(f"Renamed  : {result.change_counts['rename']:,}")
    print(f"Total    : {total_changes:,}")

    if result.changes:
        print(f"\nDetected changes (showing {len(result.changes)} of {total_changes:,})")
        print("-" * 26)
        for change in result.changes:
            if change.change_type == "rename":
                print(f"[RENAME] {change.previous_path} -> {change.path}")
            else:
                print(f"[{change.change_type.upper()}] {change.path}")

        if total_changes > len(result.changes):
            print(f"... {total_changes - len(result.changes):,} more not shown")
    elif total_changes == 0:
        print("\nNo changes detected.")
    else:
        print("\nChanges detected, but their details are hidden by the display limit.")


def run() -> None:
    arguments = parse_arguments()
    started_at = time.perf_counter()
    result = update_snapshot(
        arguments.folder,
        arguments.database,
        arguments.show_changes,
    )
    elapsed_seconds = time.perf_counter() - started_at
    print_result(
        result,
        arguments.folder,
        arguments.database,
        elapsed_seconds,
    )
