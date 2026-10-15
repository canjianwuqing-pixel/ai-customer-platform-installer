"""Fixture-only check around the real install/upgrade entry points.

The fixture writes no new password into argv, environment, output or marker
files: only the existing authentication database contains it. The marker is a
digest used to compare the stored value after installation/upgrade.
"""

import hashlib
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from platform_admin.auth import AdminError, AuthStore
from platform_admin.password_reset import reset_existing_password


def main():
    mode, data_path = sys.argv[1:]
    path = Path(data_path).resolve()
    env = {**os.environ, "ADMIN_DATA_PATH": str(path),
           "ADMIN_SESSION_SECRET": os.environ.get("ADMIN_SESSION_SECRET") or "fixture-persistence-session-secret-123456789"}
    auth = AuthStore(env)
    marker = path / "fixture.digest"
    if mode == "prepare":
        auth.login("admin", env.get("ADMIN_PASSWORD", "admin"))
        password = secrets.token_urlsafe(32)
        reset_existing_password(password, env)
        marker.write_text(hashlib.sha256(password.encode()).hexdigest(), encoding="ascii")
    else:
        with auth.db() as db:
            password = db.execute("SELECT value FROM admin_settings WHERE name='ADMIN_PASSWORD'").fetchone()[0]
        if not secrets.compare_digest(hashlib.sha256(password.encode()).hexdigest(), marker.read_text(encoding="ascii")):
            raise SystemExit("Persistent recovery credentials changed")
        auth.login("admin", password)
        try:
            auth.login("admin", env.get("ADMIN_PASSWORD", "admin"))
        except AdminError as error:
            if error.status == 401:
                return
        raise SystemExit("Stale bootstrap password was restored")


if __name__ == "__main__":
    main()
