"""Instance-scoped UTF-8 knowledge files; no credentials or global content store.

Ubuntu uses directory descriptors and O_NOFOLLOW for every path component.
The portable branch is for development on Windows, not the deployment target.
"""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import stat

from .config import RuntimeFailure, SERVICE_ID, validate_services


MAX_CONTENT_BYTES = 1_048_576
DENIED_NAMES = {"config", "configuration", "settings", "credential", "credentials", "secret", "secrets",
                "token", "tokens", "password", "passwords", "auth", "passwd", "shadow", "core"}
DENIED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".crt", ".p12", ".pfx", ".env",
                   ".ini", ".toml", ".conf", ".cfg"}


def knowledge_path(data_dir, service_id, source):
    if not SERVICE_ID.fullmatch(service_id) or not isinstance(source, str) or len(source) > 1024:
        raise RuntimeFailure("unsafe_knowledge_path", 422)
    parts = source.split("/")
    if (not source or "\\" in source or ":" in source or any(ord(c) < 32 for c in source)
            or any(not part or part.startswith(".") or part.split(".")[0].lower() in DENIED_NAMES
                   or Path(part).suffix.lower() in DENIED_SUFFIXES for part in parts)):
        raise RuntimeFailure("unsafe_knowledge_path", 422)
    path = Path(data_dir).absolute() / "services" / service_id / Path(*parts)
    for candidate in (path, *path.parents):
        _not_link(candidate)
    return path


def _not_link(path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise RuntimeFailure("unsafe_knowledge_path", 422)


@contextmanager
def _parent(path, *, create=False):
    """Anchor all operations to a non-linked parent, even during path renames."""
    descriptor = None
    try:
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(path.anchor, flags)
            for part in path.parent.parts[1:]:
                try:
                    next_fd = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_fd
            yield descriptor
        else:
            for parent in reversed((path.parent, *path.parent.parents)):
                _not_link(parent)
                if create:
                    parent.mkdir(mode=0o700, exist_ok=True)
                else:
                    try:
                        info = parent.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    # Windows may report ENOENT for a child of a regular
                    # file. Do not mistake that invalid path for a new body.
                    if not stat.S_ISDIR(info.st_mode):
                        raise NotADirectoryError
            _not_link(path)
            yield None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open(path, parent):
    if parent is None:
        _not_link(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path.name if parent is not None else path, flags,
                 **({"dir_fd": parent} if parent is not None else {}))
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise RuntimeFailure("unsafe_knowledge_path", 422)
    return fd


def _read(path, parent):
    try:
        with os.fdopen(_open(path, parent), "rb") as stream:
            raw = stream.read(MAX_CONTENT_BYTES + 1)
        if len(raw) > MAX_CONTENT_BYTES:
            raise RuntimeFailure("knowledge_too_large", 413)
        content = raw.decode("utf-8")
        if "\x00" in content:
            raise RuntimeFailure("invalid_knowledge_content", 422)
        return True, content
    except FileNotFoundError:
        return False, ""
    except UnicodeError:
        raise RuntimeFailure("invalid_knowledge_content", 422) from None


def read_runtime_knowledge(config):
    # Read fresh on each request. Large legacy text files can still be read up
    # to max_characters; the editor never silently truncates an oversized file.
    try:
        with _parent(config.knowledge_source) as parent:
            with io.TextIOWrapper(os.fdopen(_open(config.knowledge_source, parent), "rb"),
                                  encoding="utf-8") as stream:
                return stream.read(config.knowledge_max_characters)
    except FileNotFoundError:
        # A validated instance path may not have a body (or parent) yet.
        # Association/runtime reads are read-only; the editor creates it on
        # first save. Do not treat permission, link, type or UTF-8 errors as
        # empty knowledge: they must still fail closed below.
        return ""
    except (OSError, UnicodeError, RuntimeFailure):
        raise RuntimeFailure("knowledge_source_unavailable") from None


@contextmanager
def _storage_errors():
    # Translate file errors before the surrounding SQLite transaction handles
    # its own errors, so a disk/path failure is not misreported as a DB failure.
    try:
        yield
    except OSError:
        raise RuntimeFailure("knowledge_storage_unavailable", 503) from None


class KnowledgeStore:
    def __init__(self, resolver):
        from .management_store import ManagementStore
        self.resolver = resolver
        self.store = ManagementStore(resolver)

    def _binding(self, service_id, connection):
        if not SERVICE_ID.fullmatch(service_id):
            raise RuntimeFailure("invalid_service_id", 422)
        services = self.store._apply(connection, "service", self.resolver._file_services())
        validate_services(services)
        if service_id not in services:
            raise RuntimeFailure("unknown_service_id", 404)
        reference = services[service_id]["knowledge_config"]
        if not reference:
            raise RuntimeFailure("knowledge_not_configured", 409)
        profile = self.resolver._profile("knowledge", reference, {"source", "max_characters"},
                                         profiles=self.store._profiles(connection))
        source, limit = profile.get("source", ""), profile.get("max_characters", 12000)
        if not source:
            raise RuntimeFailure("knowledge_not_configured", 409)
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise RuntimeFailure("invalid_configuration", 422)
        path = knowledge_path(self.resolver.data_dir, service_id, source)
        return path, {"service_id": service_id, "knowledge_config": reference, "source": source,
                      "max_characters": limit, "max_bytes": MAX_CONTENT_BYTES}

    @staticmethod
    def _record(binding, exists, content):
        revision = hashlib.sha256(json.dumps([binding, exists, content], ensure_ascii=False,
                                            sort_keys=True).encode("utf-8")).hexdigest()
        return {**binding, "exists": exists, "content": content, "revision": revision}

    def content(self, service_id, *, content=None, revision=None):
        writing = content is not None
        if writing:
            if not isinstance(content, str) or "\x00" in content:
                raise RuntimeFailure("invalid_knowledge_content", 422)
            try:
                raw = content.encode("utf-8")
            except UnicodeError:
                raise RuntimeFailure("invalid_knowledge_content", 422) from None
            if len(raw) > MAX_CONTENT_BYTES:
                raise RuntimeFailure("knowledge_too_large", 413)
        try:
            # BEGIN IMMEDIATE serializes writers across workers, including
            # service/profile edits. No new database schema or content rows.
            with self.store._connection(write=writing) as connection, _storage_errors():
                path, binding = self._binding(service_id, connection)
                try:
                    with _parent(path, create=writing) as parent:
                        exists, previous = _read(path, parent)
                        current = self._record(binding, exists, previous)
                        if not writing:
                            return current
                        if revision != current["revision"]:
                            raise RuntimeFailure("knowledge_conflict", 409)
                        self._replace(path, parent, raw)
                        return self._record(binding, True, content)
                except FileNotFoundError:
                    if writing:
                        raise
                    return self._record(binding, False, "")
        except UnicodeError:
            raise RuntimeFailure("invalid_knowledge_content", 422) from None
        except OSError:
            raise RuntimeFailure("knowledge_storage_unavailable", 503) from None

    @staticmethod
    def _replace(path, parent, raw):
        name = ".knowledge-" + secrets.token_hex(16) + ".tmp"
        target = name if parent is not None else path.parent / name
        kwargs = {"dir_fd": parent} if parent is not None else {}
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, **kwargs)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Refuse a concurrently introduced link; replacement never follows
            # one, but failing instead of replacing it makes tampering visible.
            try:
                check = _open(path, parent)
            except FileNotFoundError:
                pass
            else:
                os.close(check)
            os.replace(target, path.name if parent is not None else path,
                       **({"src_dir_fd": parent, "dst_dir_fd": parent} if parent is not None else {}))
            if parent is not None:
                # The atomic replacement is already committed. Try to persist
                # the directory entry without reporting a misleading rollback
                # if this filesystem does not support directory fsync.
                try:
                    os.fsync(parent)
                except OSError:
                    pass
        finally:
            try:
                os.unlink(target, **kwargs)
            except FileNotFoundError:
                pass
