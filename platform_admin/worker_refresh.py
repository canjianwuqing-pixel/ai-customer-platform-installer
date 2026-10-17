"""Fixed host-only one-shot: replace old executor only after its job ends.

Launched by systemd in a separate unit, never by browser-supplied commands.
An SQLite write lock closes the check/restart race with old job submissions.
"""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


def refresh(database, unit, *, execute=subprocess.run, deadline_seconds=1800):
    database = Path(database)
    if not database.is_absolute() or database.is_symlink():
        raise ValueError
    import re
    if not re.fullmatch(r"[A-Za-z0-9@._-]+\.service", unit):
        raise ValueError
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        db = sqlite3.connect(database, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                # No recorded running job. New INSERTs block until old process
                # exits; no destructive script can start in this interval.
                result = execute(["systemctl", "stop", unit], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
                if result.returncode:
                    raise ValueError
                db.rollback()
                execute(["systemctl", "start", unit], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
                return
            db.rollback()
        finally:
            db.close()
        time.sleep(2)
    raise ValueError


if __name__ == "__main__":
    try:
        refresh(*sys.argv[1:])
    except Exception:
        raise SystemExit("maintenance_refresh_failed") from None
