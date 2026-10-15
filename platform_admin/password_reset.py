"""Private stdin-only recovery worker for the existing Admin data volume.

The host sends this source as code, never a password argument. stdout contains
only a fixed receipt; neither this module nor the web app exposes an HTTP reset.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys


class ResetError(Exception):
    pass


def validate_password(password):
    if (not isinstance(password, str) or not 6 <= len(password) <= 1024
            or any(character in password for character in ("\0", "\r", "\n"))):
        raise ResetError("Password must contain 6-1024 characters on one line.")


def reset_existing_password(password, env=None):
    from platform_admin.auth import AuthStore

    validate_password(password)
    env = os.environ if env is None else env
    if (getattr(AuthStore, "recovery_protocol", None) != 1 or not all(
            hasattr(AuthStore, method) for method in ("_save_password", "_initialize_password", "_legacy_record"))):
        raise ResetError("Upgrade this legacy Admin image before recovery; no data was changed.")
    # Match the running Admin's actual storage, not potentially stale host .env.
    path = Path(env.get("ADMIN_DATA_PATH", "/app/data")) / "auth.sqlite3"
    if (not path.is_absolute() or not path.is_file()
            or any(item.is_symlink() for item in (path, *path.parents))):
        raise ResetError("An existing, non-linked Admin authentication database is required.")
    # mode=rw refuses to create a replacement database, including on a race.
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)) as db:
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise ResetError("Unsupported Admin authentication schema.")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            setting = (db.execute("SELECT value FROM admin_settings WHERE name='ADMIN_PASSWORD'").fetchone()
                       if "admin_settings" in tables else None)
            if setting is None and AuthStore._legacy_record(db) is None:
                raise ResetError("No existing unified administrator account was found.")
            db.execute("CREATE TABLE IF NOT EXISTS admin_settings (name TEXT PRIMARY KEY CHECK(name='ADMIN_PASSWORD'), value TEXT NOT NULL)")
            AuthStore._save_password(db, password)
            for table in ("sessions", "attempts"):
                if table in tables:
                    db.execute("DELETE FROM " + table)
            db.execute("PRAGMA user_version=1")
            db.commit()
        except BaseException:
            db.rollback()
            raise


def main():
    try:
        payload = sys.stdin.buffer.read(16385)
        if len(payload) > 16384:
            raise ResetError("Invalid recovery input.")
        request = json.loads(payload)
        if not isinstance(request, dict) or set(request) != {"password"}:
            raise ResetError("Invalid recovery input.")
        reset_existing_password(request["password"])
    except Exception:
        # Never expose exception text, a traceback, payload or credentials.
        sys.stdout.write("reset-failed\n")
        return 1
    sys.stdout.write("reset-ok\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
