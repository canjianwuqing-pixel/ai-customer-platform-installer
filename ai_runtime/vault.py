"""Encrypted credentials inside the existing protected, backed-up Runtime volume."""

import json
import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from .config import RuntimeFailure
from .management_store import ManagementStore


def validate_secret(value):
    if (not isinstance(value, str) or not 16 <= len(value) <= 4096
            or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in value)):
        raise RuntimeFailure("invalid_api_credential", 422)
    return value


def masked(value):
    return value[:min(8, max(0, len(value) - 8))] + "********" if value else ""


class CredentialVault:
    def __init__(self, resolver):
        self.store = ManagementStore(resolver)
        # Keep key material in the backed-up data volume even when an advanced
        # deployment overrides the management DB location.
        self.key_path = resolver.data_dir / "management" / "vault.key"

    def _cipher(self, *, create=False):
        path = self.key_path
        try:
            for candidate in (path, *path.parents):
                if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                    raise RuntimeFailure("credential_storage_unavailable")
            if create:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not path.exists():
                    # Writers hold the shared management transaction lock. If
                    # key material was lost, never replace it for existing rows.
                    try:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    except FileExistsError:
                        pass
                    else:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(Fernet.generate_key())
                            stream.flush()
                            os.fsync(stream.fileno())
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077)):
                    raise RuntimeFailure("credential_storage_unavailable")
                key = stream.read(45)
            return Fernet(key)
        except (OSError, ValueError):
            raise RuntimeFailure("credential_storage_unavailable") from None

    def seal(self, connection, value):
        if not self.key_path.exists() and connection.execute("SELECT 1 FROM runtime_credentials LIMIT 1").fetchone():
            raise RuntimeFailure("credential_storage_unavailable")
        return self._cipher(create=True).encrypt(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def unseal(self, value):
        try:
            return json.loads(self._cipher().decrypt(value))
        except (InvalidToken, ValueError, UnicodeError, TypeError):
            raise RuntimeFailure("credential_storage_unavailable") from None

    def put(self, connection, scope, key, value):
        sealed = self.seal(connection, value)
        connection.execute("INSERT INTO runtime_credentials VALUES (?,?,?) ON CONFLICT(scope,key) DO UPDATE SET sealed=excluded.sealed",
                           (scope, key, sealed))

    def get(self, scope, key, *, connection=None):
        if connection is None:
            with self.store._connection() as current:
                if current is None:
                    return None
                return self.get(scope, key, connection=current)
        if connection.execute("PRAGMA user_version").fetchone()[0] < 3:
            return None
        row = connection.execute("SELECT sealed FROM runtime_credentials WHERE scope=? AND key=?", (scope, key)).fetchone()
        return self.unseal(row[0]) if row else None
