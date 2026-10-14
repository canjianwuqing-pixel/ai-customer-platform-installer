"""One account, revocable server-side sessions, and persisted password changes."""

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .passwords import hash_password, valid_hash, verify_password


class AdminError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


class AuthStore:
    def __init__(self, env):
        self.path = Path(env.get("ADMIN_DATA_PATH", "/app/data")) / "auth.sqlite3"
        self.username = env.get("ADMIN_USERNAME", "admin")
        self.secret = env.get("ADMIN_SESSION_SECRET", "")
        self.bootstrap_hash = env.get("ADMIN_PASSWORD_HASH", "")
        self.ttl = int(env.get("ADMIN_SESSION_TTL_SECONDS", "3600"))
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}", self.username)
                or len(self.secret) < 32 or not valid_hash(self.bootstrap_hash)
                or not 60 <= self.ttl <= 86400 or not self.path.is_absolute()):
            raise AdminError("admin_not_configured", 503)

    @contextmanager
    def db(self):
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise AdminError("unsafe_admin_storage", 503)
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        if os.name == "posix":
            os.chmod(self.path.parent, 0o700)
            os.chmod(self.path, 0o600)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO account VALUES (1, ?)", (self.bootstrap_hash,))
            connection.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, authenticated INTEGER NOT NULL, expires INTEGER NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY CHECK(id=1), failures INTEGER NOT NULL, until INTEGER NOT NULL)")
            connection.execute("DELETE FROM sessions WHERE expires <= ?", (int(time.time()),))
            yield connection
            connection.commit()
        finally:
            connection.close()

    def digest(self, token):
        return hmac.new(self.secret.encode(), token.encode(), hashlib.sha256).hexdigest()

    def session(self, token):
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        with self.db() as db:
            return db.execute("SELECT csrf, authenticated FROM sessions WHERE id=?", (self.digest(token),)).fetchone()

    def create_session(self, authenticated=False, old=""):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (self.digest(old),))
            # Bound abandoned login sessions without interfering with valid logins.
            db.execute("DELETE FROM sessions WHERE authenticated=0 AND rowid NOT IN (SELECT rowid FROM sessions WHERE authenticated=0 ORDER BY rowid DESC LIMIT 100)")
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                       (self.digest(token), csrf, int(authenticated), int(time.time()) + self.ttl))
        return token, csrf

    def logout(self, token):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (self.digest(token),))

    def login(self, username, password):
        with self.db() as db:
            row = db.execute("SELECT failures, until FROM attempts WHERE id=1").fetchone()
            if row and row[0] >= 5 and row[1] > time.time():
                raise AdminError("login_throttled", 429)
            stored = db.execute("SELECT hash FROM account WHERE id=1").fetchone()[0]
            good = verify_password(password, stored)
            good = hmac.compare_digest(username.encode(), self.username.encode()) and good
            if good:
                db.execute("DELETE FROM attempts")
            else:
                failures = (row[0] if row and row[1] > time.time() else 0) + 1
                db.execute("INSERT OR REPLACE INTO attempts VALUES (1, ?, ?)", (failures, int(time.time()) + 300))
        if not good:
            raise AdminError("invalid_credentials", 401)

    def change_password(self, current, replacement):
        if not 16 <= len(replacement) <= 1024:
            raise AdminError("password_must_be_16_to_1024_characters", 422)
        with self.db() as db:
            stored = db.execute("SELECT hash FROM account WHERE id=1").fetchone()[0]
            if not verify_password(current, stored):
                raise AdminError("invalid_credentials", 401)
            db.execute("UPDATE account SET hash=? WHERE id=1", (hash_password(replacement),))
            db.execute("DELETE FROM sessions")
