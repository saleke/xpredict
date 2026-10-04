"""Create a consistent SQLite snapshot, including committed WAL contents.

Usage: python3 scripts/backup_database.py data/lisa.db backups/lisa-20261003.db
The destination must not exist. Copy the completed snapshot off the host.
"""
import argparse
import os
from pathlib import Path
import sqlite3
import time


def backup_database(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    reader = writer = None
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("database backup exceeded 60 seconds")

    try:
        reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)
        writer = sqlite3.connect(destination, timeout=10)
        reader.backup(writer, pages=1000, progress=progress)
        if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup failed its integrity check")
    except BaseException:
        if writer is not None:
            writer.close()
            writer = None
        destination.unlink(missing_ok=True)
        raise
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    backup_database(args.source, args.destination)
    print(f"Verified backup: {args.destination}")
