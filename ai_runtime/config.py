"""Resolve the existing services.yaml schema into request-local runtime data."""

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
import os
import re
import stat
from urllib.parse import urlsplit

import yaml


SERVICE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
REFERENCE = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
SERVICE_FIELDS = {
    "id", "name", "enabled", "ai_config", "prompt_config",
    "knowledge_config", "channel_config",
}


class RuntimeFailure(Exception):
    """Only stable error codes may cross the HTTP/log boundary."""

    def __init__(self, code: str, status: int = 503):
        self.code = code
        self.status = status
        super().__init__(code)


class UniqueSafeLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently accepting a second value."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise RuntimeFailure("invalid_configuration")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping
)


def _load_yaml(path: Path, *, private: bool = False) -> dict:
    try:
        if private:
            if path.is_symlink():
                raise RuntimeFailure("unsafe_configuration_permissions")
            if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise RuntimeFailure("unsafe_configuration_permissions")
        with path.open(encoding="utf-8") as stream:
            text = stream.read(1_048_577)
        if len(text) > 1_048_576:
            raise RuntimeFailure("configuration_too_large")
        data = yaml.load(text, Loader=UniqueSafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError, ValueError):
        raise RuntimeFailure("configuration_unavailable") from None
    if not isinstance(data, dict):
        raise RuntimeFailure("invalid_configuration")
    return data


def _text(value, *, allow_empty=False) -> str:
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise RuntimeFailure("invalid_configuration")
    return value


def _boolean(value) -> bool:
    # Match lib/services.sh, including quoted true/false scalars.
    if type(value) is bool:
        return value
    if value in ("true", "false"):
        return value == "true"
    raise RuntimeFailure("invalid_configuration")


def _inside(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in relative:
        raise RuntimeFailure("invalid_configuration_reference")
    try:
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise RuntimeFailure("invalid_configuration_reference")
    except (OSError, RuntimeError, ValueError):
        raise RuntimeFailure("invalid_configuration_reference") from None
    return resolved


def validate_services(services: dict[str, dict]) -> None:
    """Shared validation for YAML and API overrides; never accept credentials."""
    for service_id, entry in services.items():
        if not SERVICE_ID.fullmatch(service_id) or set(entry) != SERVICE_FIELDS:
            raise RuntimeFailure("invalid_configuration")
        if entry["id"] != service_id or len(_text(entry["name"])) > 128:
            raise RuntimeFailure("invalid_configuration")
        entry["enabled"] = _boolean(entry["enabled"])
        for key in ("ai_config", "prompt_config", "knowledge_config", "channel_config"):
            ref = _text(entry[key], allow_empty=True)
            if ref and (
                not REFERENCE.fullmatch(ref)
                or any(part in {".", ".."} for part in ref.split("/"))
            ):
                raise RuntimeFailure("invalid_configuration_reference")


@dataclass(frozen=True, repr=False)
class RuntimeConfig:
    service_id: str
    model: str
    api_base_url: str
    api_key_env: str
    system_prompt: str = field(repr=False)
    reply_rules: tuple[str, ...] = field(repr=False)
    knowledge_source: Path | None = field(repr=False)
    knowledge_max_characters: int


class ServiceResolver:
    """No mutable current-service, credentials, conversations, or global cache."""

    def __init__(self, config_dir: Path, data_dir: Path, env: Mapping[str, str]):
        self.config_dir = config_dir.resolve()
        self.data_dir = data_dir.resolve()
        self.env = dict(env)

    @classmethod
    def from_environment(cls):
        return cls(
            Path(os.environ.get("AI_RUNTIME_CONFIG_PATH", "/app/config/services")),
            Path(os.environ.get("AI_SERVICE_DATA_PATH", "/app/data")),
            os.environ,
        )

    def _file_services(self) -> dict[str, dict]:
        services = {}
        default_file = Path(self.env.get(
            "SERVICE_INSTANCES_DEFAULT_FILE", str(self.config_dir / "services.yaml")
        ))
        user_file = Path(self.env.get(
            "SERVICE_INSTANCES_FILE", str(self.config_dir / "local.yaml")
        ))
        layers = [(default_file, False)]
        if user_file.exists() or user_file.is_symlink() or self.env.get("SERVICE_INSTANCES_FILE"):
            layers.append((user_file, True))
        for path, private in layers:
            data = _load_yaml(path, private=private)
            if set(data) != {"version", "services"} or type(data["version"]) is not int or data["version"] != 1:
                raise RuntimeFailure("invalid_configuration")
            if not isinstance(data["services"], list):
                raise RuntimeFailure("invalid_configuration")
            seen = set()
            for entry in data["services"]:
                if not isinstance(entry, dict) or not set(entry) <= SERVICE_FIELDS:
                    raise RuntimeFailure("invalid_configuration")
                service_id = _text(entry.get("id"))
                if not SERVICE_ID.fullmatch(service_id) or service_id in seen:
                    raise RuntimeFailure("invalid_configuration")
                seen.add(service_id)
                services[service_id] = {**services.get(service_id, {}), **entry}
        validate_services(services)
        return services

    def _services(self) -> dict[str, dict]:
        # The store is optional. With no database this is the original YAML
        # flow, and a runtime read never creates or migrates a database.
        from .management_store import ManagementStore

        return ManagementStore(self).read_services()

    def _profile(self, kind: str, reference: str, allowed: set[str]) -> dict:
        if not reference:
            return {}
        path = _inside(self.config_dir / kind, reference + ".yaml")
        data = _load_yaml(path)
        if not set(data) <= allowed:
            # An API key/password field is never accepted in a profile.
            raise RuntimeFailure("invalid_configuration")
        return data

    def resolve(self, service_id: str | None = None) -> RuntimeConfig:
        if service_id is not None and not SERVICE_ID.fullmatch(service_id):
            raise RuntimeFailure("invalid_service_id", 422)
        services = self._services()
        selected = service_id or self.env.get("AI_DEFAULT_SERVICE_ID", "")
        if not selected:
            enabled = [key for key, value in services.items() if value["enabled"]]
            if len(enabled) == 1:
                selected = enabled[0]
            elif not services:
                return self._runtime_config("default", {}, {}, {}, legacy=True)
            else:
                raise RuntimeFailure("default_service_not_configured")
        if selected not in services:
            raise RuntimeFailure("unknown_service_id", 404)
        entry = services[selected]
        if not entry["enabled"]:
            raise RuntimeFailure("service_disabled", 409)
        return self.runtime_for_instance(entry)

    def runtime_for_instance(self, entry: dict) -> RuntimeConfig:
        """Resolve a validated record, including a not-yet-committed API edit."""
        if not entry["ai_config"]:
            raise RuntimeFailure("ai_configuration_missing")
        ai = self._profile("ai", entry["ai_config"], {"model", "api_base_url", "api_key_env"})
        prompt = self._profile("prompts", entry["prompt_config"], {"system_prompt", "reply_rules"})
        knowledge = self._profile("knowledge", entry["knowledge_config"], {"source", "max_characters"})
        return self._runtime_config(entry["id"], ai, prompt, knowledge)

    def _runtime_config(self, service_id, ai, prompt, knowledge, *, legacy=False):
        model = _text(self.env.get("AI_MODEL", "") if legacy else ai.get("model"))
        base_url = _text(ai.get("api_base_url", self.env.get("AI_API_BASE_URL", ""))).rstrip("/")
        try:
            parsed_url = urlsplit(base_url)
            parsed_url.port
        except ValueError:
            raise RuntimeFailure("invalid_provider_url") from None
        if parsed_url.scheme not in {"https", "http"} or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise RuntimeFailure("invalid_provider_url")
        api_key_env = _text(ai.get("api_key_env", "OPENAI_API_KEY"))
        if not ENV_NAME.fullmatch(api_key_env):
            raise RuntimeFailure("invalid_configuration")
        system_prompt = _text(
            self.env.get("AI_SYSTEM_PROMPT", "") if legacy else prompt.get("system_prompt", ""),
            allow_empty=True,
        )
        rules = prompt.get("reply_rules", [])
        if not isinstance(rules, list) or len(rules) > 64:
            raise RuntimeFailure("invalid_configuration")
        reply_rules = tuple(_text(rule) for rule in rules)
        source = _text(knowledge.get("source", ""), allow_empty=True)
        service_root = self.data_dir / "services" / service_id
        if source and (service_root.is_symlink() or service_root.parent.is_symlink()):
            raise RuntimeFailure("invalid_configuration_reference")
        knowledge_path = _inside(service_root, source) if source else None
        limit = knowledge.get("max_characters", 12000)
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise RuntimeFailure("invalid_configuration")
        return RuntimeConfig(
            service_id, model, base_url, api_key_env, system_prompt,
            reply_rules, knowledge_path, limit,
        )

    def credential(self, config: RuntimeConfig) -> str:
        key = self.env.get(config.api_key_env, "")
        if not key or "\n" in key or "\r" in key:
            raise RuntimeFailure("provider_credentials_missing")
        return key
