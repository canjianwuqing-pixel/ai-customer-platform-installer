"""Non-secret runtime profile management using the existing profile schema."""

from typing import Literal
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .config import REFERENCE, RuntimeFailure
from .management_store import ManagementStore


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ModelProfile(Profile):
    model: str = Field(min_length=1, max_length=256)
    api_base_url: str = Field(default="", max_length=2048)
    api_key_env: str = Field(default="OPENAI_API_KEY", pattern=r"^[A-Z_][A-Z0-9_]*$")
    supports_vision: bool = False


class PromptProfile(Profile):
    system_prompt: str = Field(default="", max_length=32000)
    reply_rules: list[str] = Field(default_factory=list, max_length=64)


class KnowledgeProfile(Profile):
    source: str = Field(default="", max_length=1024)
    max_characters: int = Field(default=12000, ge=1, le=100000)


class ChannelProfile(Profile):
    enabled: bool = False
    provider: str = Field(default="", max_length=128)
    config_ref: str = Field(default="", max_length=128)


class RuntimeProfile(Profile):
    default_service_id: str = Field(default="", pattern=r"^(?:[a-z][a-z0-9_-]{0,63})?$")


MODELS = {"ai": ModelProfile, "prompts": PromptProfile, "knowledge": KnowledgeProfile,
          "channels": ChannelProfile, "runtime": RuntimeProfile}


def validate_profile(resolver, kind, key, body):
    if kind not in MODELS or not REFERENCE.fullmatch(key) or any(p in {".", ".."} for p in key.split("/")):
        raise RuntimeFailure("invalid_profile_identifier", 422)
    if kind == "runtime" and key != "default":
        raise RuntimeFailure("invalid_runtime_profile", 422)
    try:
        record = MODELS[kind].model_validate(body).model_dump()
        if kind == "ai":
            if not record["api_base_url"]:
                record["api_base_url"] = resolver.env.get("AI_API_BASE_URL") or "https://api.openai.com/v1"
            resolver._runtime_config("validation", record, {}, {})
        elif kind == "prompts":
            if any(not rule.strip() or len(rule) > 4000 for rule in record["reply_rules"]):
                raise ValueError
        elif kind == "knowledge":
            from .knowledge import knowledge_path
            if record["source"]:
                knowledge_path(resolver.data_dir, "validation", record["source"])
        elif kind == "channels":
            if record["config_ref"] and (not REFERENCE.fullmatch(record["config_ref"])
                    or any(part in {".", ".."} for part in record["config_ref"].split("/"))):
                raise ValueError
    except (ValueError, RuntimeFailure):
        raise RuntimeFailure("invalid_profile_configuration", 422) from None
    return record


def profile_router(resolver):
    store = ManagementStore(resolver)
    router = APIRouter()
    Kind = Literal["ai", "prompts", "knowledge", "channels", "runtime"]

    @router.get("/profiles/{kind}")
    def profiles(kind: Kind):
        return store.list_profiles(kind)

    @router.get("/profiles/{kind}/{key:path}")
    def profile(kind: Kind, key: str):
        return {"id": key, **store.get_profile(kind, key)}

    @router.post("/profiles/{kind}", status_code=201)
    def create(kind: Kind, body: dict):
        body = dict(body)
        key = body.pop("id", None)
        if not isinstance(key, str):
            raise RuntimeFailure("invalid_profile_identifier", 422)
        return store.save_profile(kind, key, body, create=True)

    @router.put("/profiles/{kind}/{key:path}")
    def update(kind: Kind, key: str, body: dict):
        return store.save_profile(kind, key, body)

    return router
