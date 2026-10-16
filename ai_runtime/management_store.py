"""Transactional API overrides over the unchanged, read-only YAML layers."""

from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .config import RuntimeFailure, _boolean, _load_yaml, _text, validate_services


SCHEMA_VERSION = 4
MIGRATIONS = {
    1: (
        """CREATE TABLE management_records (
            kind TEXT NOT NULL CHECK (kind IN ('service', 'domain')),
            key TEXT NOT NULL,
            config TEXT NOT NULL,
            deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
            PRIMARY KEY (kind, key)
        )""",
    ),
    2: ("""CREATE TABLE runtime_profiles (
        kind TEXT NOT NULL, key TEXT NOT NULL, config TEXT NOT NULL,
        PRIMARY KEY (kind, key))""",),
    3: (
        """CREATE TABLE runtime_credentials (scope TEXT NOT NULL, key TEXT NOT NULL,
            sealed BLOB NOT NULL, PRIMARY KEY(scope,key))""",
        """CREATE TABLE chatwoot_bindings (service_id TEXT PRIMARY KEY, base_url TEXT NOT NULL,
            account_id INTEGER NOT NULL, inbox_id INTEGER, bot_id INTEGER, enabled INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', UNIQUE(base_url,account_id,inbox_id))""",
        """CREATE TABLE chatwoot_conversations (service_id TEXT NOT NULL, conversation_id INTEGER NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0, resume_after INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(service_id,conversation_id))""",
        """CREATE TABLE chatwoot_events (service_id TEXT NOT NULL, conversation_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', attempts INTEGER NOT NULL DEFAULT 0,
            next_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            reply BLOB, error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            PRIMARY KEY(service_id,conversation_id,message_id))""",
        "CREATE INDEX chatwoot_event_queue ON chatwoot_events(status,next_at)",
    ),
    4: (
        "ALTER TABLE chatwoot_conversations ADD COLUMN lifecycle_at REAL NOT NULL DEFAULT 0",
        "ALTER TABLE chatwoot_conversations ADD COLUMN awaiting_reopen INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chatwoot_conversations ADD COLUMN generation INTEGER NOT NULL DEFAULT 0",
    ),
}
DOMAIN_FIELDS = {"domain", "service_id", "enabled", "default_entry"}
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def valid_domain(domain: str) -> bool:
    if not isinstance(domain, str) or len(domain) > 253 or domain != domain.lower():
        return False
    labels = domain.split(".")
    if len(labels) < 2 or labels[-1].isdigit() or not all(DNS_LABEL.fullmatch(label) for label in labels):
        return False
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return True
    return False


def validate_domains(domains: dict[str, dict], services: dict[str, dict]) -> None:
    default_count = 0
    for domain, entry in domains.items():
        if set(entry) != DOMAIN_FIELDS or entry["domain"] != domain or not valid_domain(domain):
            raise RuntimeFailure("invalid_domain_configuration", 422)
        service_id = _text(entry["service_id"])
        if service_id not in services:
            raise RuntimeFailure("invalid_service_binding", 422)
        entry["enabled"] = _boolean(entry["enabled"])
        entry["default_entry"] = _boolean(entry["default_entry"])
        if entry["default_entry"] and not entry["enabled"]:
            raise RuntimeFailure("default_domain_must_be_enabled", 409)
        if entry["enabled"] and not services[service_id]["enabled"]:
            raise RuntimeFailure("bound_service_is_disabled", 409)
        default_count += int(entry["default_entry"])
    if default_count > 1:
        raise RuntimeFailure("multiple_default_domains", 409)


class ManagementStore:
    def __init__(self, resolver):
        self.resolver = resolver
        configured = resolver.env.get("SERVICE_MANAGEMENT_DATABASE", "")
        self.path = Path(configured) if configured else resolver.data_dir / "management" / "core.sqlite3"
        if not self.path.is_absolute():
            raise RuntimeFailure("invalid_management_database_path")
        self.domain_dir = Path(resolver.env.get(
            "AI_RUNTIME_DOMAINS_PATH", str(resolver.config_dir.parent / "domains")
        ))

    def _check_path(self, write: bool):
        # Reject symlink components, including a linked management directory.
        if any(parent.is_symlink() for parent in (self.path, *self.path.parents)):
            raise RuntimeFailure("unsafe_management_storage")
        if write:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name == "posix" and stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
                raise RuntimeFailure("unsafe_management_storage_permissions")
            if not self.path.exists():
                try:
                    descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
        if self.path.exists():
            if not self.path.is_file():
                raise RuntimeFailure("unsafe_management_storage")
            if os.name == "posix" and (
                stat.S_IMODE(self.path.stat().st_mode) & 0o077
                or stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077
            ):
                raise RuntimeFailure("unsafe_management_storage_permissions")

    @staticmethod
    def _schema(connection, *, migrate=False):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 0 or version > SCHEMA_VERSION:
            raise RuntimeFailure("unsupported_management_schema")
        if version == 0:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                raise RuntimeFailure("unsupported_management_schema")
        if migrate:
            for next_version in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[next_version]:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {next_version}")
            return True
        # A first writer can be initializing a new database. Its uncommitted
        # migration is invisible to readers, which can still use base YAML.
        return version >= 1

    @contextmanager
    def _connection(self, *, write=False):
        connection = None
        try:
            self._check_path(write)
            if not self.path.exists():
                yield None
                return
            uri = self.path.as_uri() + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            initialized = self._schema(connection, migrate=write)
            yield connection if initialized else None
            connection.commit()
        except (OSError, sqlite3.Error, ValueError):
            raise RuntimeFailure("management_storage_unavailable") from None
        finally:
            if connection is not None:
                connection.close()  # An uncommitted mutation is rolled back.

    def _file_domains(self):
        domains = {}
        default_file = Path(self.resolver.env.get(
            "DOMAIN_BINDINGS_DEFAULT_FILE", str(self.domain_dir / "domains.yaml")
        ))
        user_file = Path(self.resolver.env.get(
            "DOMAIN_BINDINGS_FILE", str(self.domain_dir / "local.yaml")
        ))
        layers = []
        if default_file.exists() or default_file.is_symlink() or self.resolver.env.get("DOMAIN_BINDINGS_DEFAULT_FILE"):
            layers.append((default_file, False))
        if user_file.exists() or user_file.is_symlink() or self.resolver.env.get("DOMAIN_BINDINGS_FILE"):
            layers.append((user_file, True))
        for path, private in layers:
            data = _load_yaml(path, private=private)
            if set(data) != {"version", "domains"} or type(data["version"]) is not int or data["version"] != 1:
                raise RuntimeFailure("invalid_domain_configuration")
            if not isinstance(data["domains"], list):
                raise RuntimeFailure("invalid_domain_configuration")
            seen = set()
            for entry in data["domains"]:
                if not isinstance(entry, dict) or not set(entry) <= DOMAIN_FIELDS:
                    raise RuntimeFailure("invalid_domain_configuration")
                domain = _text(entry.get("domain"))
                if not valid_domain(domain) or domain in seen:
                    raise RuntimeFailure("invalid_domain_configuration")
                seen.add(domain)
                domains[domain] = {**domains.get(domain, {}), **entry}
        return domains

    @staticmethod
    def _apply(connection, kind, baseline):
        if connection is None:
            return baseline
        for key, text, deleted in connection.execute(
            "SELECT key, config, deleted FROM management_records WHERE kind=? ORDER BY key", (kind,)
        ):
            if deleted:
                baseline.pop(key, None)
            else:
                try:
                    record = json.loads(text)
                except (TypeError, ValueError):
                    raise RuntimeFailure("invalid_management_record") from None
                if not isinstance(record, dict):
                    raise RuntimeFailure("invalid_management_record")
                baseline[key] = record
        return baseline

    def _state(self, connection):
        services = self._apply(connection, "service", self.resolver._file_services())
        domains = self._apply(connection, "domain", self._file_domains())
        validate_services(services)
        validate_domains(domains, services)
        return services, domains

    def read_services(self):
        with self._connection() as connection:
            services = self._apply(connection, "service", self.resolver._file_services())
            validate_services(services)
            return services

    def runtime_state(self):
        # One SQLite snapshot for a request's service and all its profiles.
        with self._connection() as connection:
            services = self._apply(connection, "service", self.resolver._file_services())
            validate_services(services)
            return services, self._profiles(connection)

    def read_state(self):
        with self._connection() as connection:
            return self._state(connection)

    def _profiles(self, connection):
        if connection is None or connection.execute("PRAGMA user_version").fetchone()[0] < 2:
            return {}
        return {(kind, key): json.loads(text) for kind, key, text in connection.execute(
            "SELECT kind, key, config FROM runtime_profiles")}

    def profile_overrides(self):
        with self._connection() as connection:
            return self._profiles(connection)

    def get_profile(self, kind, key):
        from .profile_api import MODELS
        from .config import _inside
        if kind not in MODELS:
            raise RuntimeFailure("invalid_profile_kind", 422)
        # Validate path even when the record comes from SQLite.
        _inside(self.resolver.config_dir / kind, key + ".yaml")
        overrides = self.profile_overrides()
        if (kind, key) in overrides:
            return overrides[(kind, key)]
        if kind == "runtime":
            if key != "default":
                raise RuntimeFailure("record_not_found", 404)
            return {"default_service_id": self.resolver.env.get("AI_DEFAULT_SERVICE_ID", "")}
        path = _inside(self.resolver.config_dir / kind, key + ".yaml")
        if not path.is_file() or key.endswith(".example"):
            raise RuntimeFailure("record_not_found", 404)
        record = _load_yaml(path)
        if not set(record) <= set(MODELS[kind].model_fields):
            raise RuntimeFailure("invalid_profile_configuration", 422)
        return record

    def list_profiles(self, kind):
        keys = {key for record_kind, key in self.profile_overrides() if record_kind == kind}
        if kind == "runtime":
            keys.add("default")
        else:
            root = self.resolver.config_dir / kind
            if root.is_dir():
                keys.update(str(path.relative_to(root).with_suffix("")).replace("\\", "/")
                            for path in root.rglob("*.yaml") if not path.name.endswith(".example.yaml"))
        return [{"id": key, **self.get_profile(kind, key)} for key in sorted(keys)]

    def save_profile(self, kind, key, body, *, create=False, credential=None):
        from .profile_api import validate_profile
        record = validate_profile(self.resolver, kind, key, body)
        with self._connection(write=True) as connection:
            if kind == "ai":
                from .vault import CredentialVault
                existing_key = CredentialVault(self.resolver).get("model", key, connection=connection)
                protected = [value for value in (credential, existing_key, self.resolver.env.get(record["api_key_env"])) if value]
                if any(value in key or any(value in str(field) for field in record.values()) for value in protected):
                    raise RuntimeFailure("invalid_profile_configuration", 422)
            profiles = self._profiles(connection)
            path = self.resolver.config_dir / kind / (key + ".yaml")
            exists = (kind, key) in profiles or path.is_file() or (kind == "runtime" and key == "default")
            if create and exists:
                raise RuntimeFailure("record_already_exists", 409)
            if not create and not exists:
                raise RuntimeFailure("record_not_found", 404)
            profiles[(kind, key)] = record
            services, _ = self._state(connection)
            if kind == "runtime" and record["default_service_id"]:
                selected = services.get(record["default_service_id"])
                if not selected or not selected["enabled"]:
                    raise RuntimeFailure("invalid_default_service", 422)
            # Reject updates that would invalidate any currently enabled service.
            for entry in services.values():
                if entry["enabled"]:
                    self._check_runtime(entry, profiles)
            connection.execute("INSERT INTO runtime_profiles VALUES (?, ?, ?) ON CONFLICT(kind,key) DO UPDATE SET config=excluded.config",
                               (kind, key, json.dumps(record, ensure_ascii=False)))
            if credential is not None:
                from .vault import CredentialVault
                CredentialVault(self.resolver).put(connection, "model", key, credential)
        return {"id": key, **record}

    def _check_runtime(self, record, profiles):
        try:
            config = self.resolver.runtime_for_instance(record, profiles=profiles)
            if config.knowledge_source is not None:
                from .knowledge import read_runtime_knowledge
                read_runtime_knowledge(config)
        except (RuntimeFailure, OSError, UnicodeError):
            raise RuntimeFailure("invalid_runtime_configuration", 422) from None

    @staticmethod
    def _save(connection, kind, key, record, *, deleted=False):
        connection.execute(
            """INSERT INTO management_records (kind, key, config, deleted) VALUES (?, ?, ?, ?)
               ON CONFLICT(kind, key) DO UPDATE SET config=excluded.config, deleted=excluded.deleted""",
            (kind, key, json.dumps(record, ensure_ascii=False), int(deleted)),
        )

    def mutate(self, kind, action, key, fields=None):
        if kind not in {"service", "domain"} or action not in {"create", "update", "delete"}:
            raise RuntimeFailure("invalid_management_operation", 422)
        with self._connection(write=True) as connection:
            services, domains = self._state(connection)
            collection = services if kind == "service" else domains
            if action == "create" and key in collection:
                raise RuntimeFailure("record_already_exists", 409)
            if action != "create" and key not in collection:
                raise RuntimeFailure("record_not_found", 404)
            if action == "delete":
                if kind == "service" and connection.execute("PRAGMA user_version").fetchone()[0] >= 3:
                    if connection.execute("SELECT 1 FROM chatwoot_bindings WHERE service_id=? AND bot_id IS NOT NULL", (key,)).fetchone():
                        raise RuntimeFailure("service_has_chatwoot_binding", 409)
                if kind == "service" and any(value["service_id"] == key for value in domains.values()):
                    raise RuntimeFailure("service_has_domain_bindings", 409)
                del collection[key]
                record = {}
            else:
                record = {**collection.get(key, {}), **(fields or {})}
                collection[key] = record
            validate_services(services)
            validate_domains(domains, services)
            profiles = self._profiles(connection)
            default = profiles.get(("runtime", "default"), {}).get("default_service_id", "")
            if default and (default not in services or not services[default]["enabled"]):
                raise RuntimeFailure("default_service_must_remain_enabled", 409)
            if kind == "service" and action != "delete" and record["enabled"]:
                self._check_runtime(record, profiles)
            self._save(connection, kind, key, record, deleted=action == "delete")
            return record

    def migrate_yaml(self):
        """Idempotently import effective YAML without replacing API records."""
        with self._connection(write=True) as connection:
            services, domains = self._state(connection)
            for kind, collection in (("service", services), ("domain", domains)):
                for key, record in collection.items():
                    connection.execute(
                        "INSERT OR IGNORE INTO management_records (kind, key, config, deleted) VALUES (?, ?, ?, 0)",
                        (kind, key, json.dumps(record, ensure_ascii=False)),
                    )
            return {"services": len(services), "domains": len(domains), "schema_version": SCHEMA_VERSION}
