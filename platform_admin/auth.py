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

from .legacy_auth import valid_legacy_hash, verify_legacy_password


class AdminError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


class AuthStore:
    # Recovery requires the revoked-prelogin guard in create_session(), not
    # just a compatible password schema. Older images must be upgraded first.
    recovery_protocol = 1

    def __init__(self, env):
        self.path = Path(env.get("ADMIN_DATA_PATH", "/app/data")) / "auth.sqlite3"
        self.username = "admin"
        self.secret = env.get("ADMIN_SESSION_SECRET", "")
        self.bootstrap_password = env.get("ADMIN_PASSWORD", "admin")
        self.legacy_hash = env.get("ADMIN_PASSWORD_HASH", "")
        self.ttl = int(env.get("ADMIN_SESSION_TTL_SECONDS", "3600"))
        if (len(self.secret) < 32
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
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise AdminError("unsupported_admin_storage", 503)
            connection.execute("CREATE TABLE IF NOT EXISTS admin_settings (name TEXT PRIMARY KEY CHECK(name='ADMIN_PASSWORD'), value TEXT NOT NULL)")
            self._initialize_password(connection)
            connection.execute("PRAGMA user_version=1")
            connection.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, authenticated INTEGER NOT NULL, expires INTEGER NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY CHECK(id=1), failures INTEGER NOT NULL, until INTEGER NOT NULL)")
            connection.execute("DELETE FROM sessions WHERE expires <= ?", (int(time.time()),))
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _legacy_record(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account'").fetchone():
            return None
        row = db.execute("SELECT hash FROM account WHERE id=1").fetchone()
        if row is None or not valid_legacy_hash(row[0]):
            raise AdminError("invalid_legacy_credentials", 503)
        return row[0]

    def _initialize_password(self, db):
        current = db.execute("SELECT value FROM admin_settings WHERE name='ADMIN_PASSWORD'").fetchone()
        if current is not None:
            if current[0] != "admin" and not 6 <= len(current[0]) <= 1024:
                raise AdminError("invalid_admin_configuration", 503)
            return
        if self._legacy_record(db) is not None:
            return  # An existing database always beats bootstrap environment.
        if self.legacy_hash:
            if not valid_legacy_hash(self.legacy_hash):
                raise AdminError("invalid_legacy_credentials", 503)
            db.execute("CREATE TABLE account (id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT NOT NULL)")
            db.execute("INSERT INTO account VALUES (1, ?)", (self.legacy_hash,))
            return
        if self.bootstrap_password != "admin" and not 6 <= len(self.bootstrap_password) <= 1024:
            raise AdminError("admin_not_configured", 503)
        self._save_password(db, self.bootstrap_password)

    @staticmethod
    def _save_password(db, password):
        # ADMIN_PASSWORD is intentionally direct configuration, not a hash.
        # This private persistent database is excluded from Git and builds.
        db.execute("INSERT INTO admin_settings VALUES ('ADMIN_PASSWORD', ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (password,))
        db.execute("DROP TABLE IF EXISTS account")

    def _matches(self, db, password):
        if not isinstance(password, str) or len(password) > 1024:
            return False
        setting = db.execute("SELECT value FROM admin_settings WHERE name='ADMIN_PASSWORD'").fetchone()
        if setting is not None:
            return hmac.compare_digest(password.encode(), setting[0].encode())
        return verify_legacy_password(password, self._legacy_record(db))

    def protected_values(self):
        with self.db() as db:
            setting = db.execute("SELECT value FROM admin_settings WHERE name='ADMIN_PASSWORD'").fetchone()
        return tuple(value for value in (self.legacy_hash, self.bootstrap_password, setting[0] if setting else "")
                     if value and value != "admin")

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
            if authenticated and old and not db.execute("SELECT 1 FROM sessions WHERE id=?", (self.digest(old),)).fetchone():
                # Recovery may have revoked the login session after password
                # verification but before this separate session transaction.
                raise AdminError("authentication_required", 401)
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
            good = self._matches(db, password)
            good = hmac.compare_digest(username.encode(), self.username.encode()) and good
            if good:
                self._save_password(db, password)  # Migrate only after a verified legacy login.
                db.execute("DELETE FROM attempts")
            else:
                failures = (row[0] if row and row[1] > time.time() else 0) + 1
                db.execute("INSERT OR REPLACE INTO attempts VALUES (1, ?, ?)", (failures, int(time.time()) + 300))
        if not good:
            raise AdminError("invalid_credentials", 401)

    def change_password(self, current, replacement):
        if not isinstance(replacement, str) or not 6 <= len(replacement) <= 1024:
            raise AdminError("password_must_be_6_to_1024_characters", 422)
        with self.db() as db:
            if not self._matches(db, current):
                raise AdminError("invalid_credentials", 401)
            self._save_password(db, replacement)
            db.execute("DELETE FROM sessions")
