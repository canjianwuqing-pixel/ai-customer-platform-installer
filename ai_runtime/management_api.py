"""Authenticated service/domain administration, with no credential fields."""

import hmac
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from starlette.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import REFERENCE, RuntimeFailure
from .management_store import ManagementStore, valid_domain


ServiceId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ServicePath = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    @field_validator("*", mode="after")
    @classmethod
    def validate_field(cls, value, info):
        if value is None:
            return value
        if info.field_name == "name" and not value.strip():
            raise ValueError("invalid_name")
        if info.field_name.endswith("_config") and value:
            if not REFERENCE.fullmatch(value) or any(part in {".", ".."} for part in value.split("/")):
                raise ValueError("invalid_reference")
        return value


class ServiceFields(InputModel):
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = False
    ai_config: str = ""
    prompt_config: str = ""
    knowledge_config: str = ""
    channel_config: str = ""


class KnowledgeWrite(InputModel):
    content: str = Field(max_length=1_048_576)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class KnowledgeClear(InputModel):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ServiceCreate(ServiceFields):
    id: ServiceId


class ServicePatch(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    ai_config: str | None = None
    prompt_config: str | None = None
    knowledge_config: str | None = None
    channel_config: str | None = None

    @model_validator(mode="after")
    def disallow_null_fields(self):
        if not self.model_fields_set or any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("invalid_patch")
        return self


class DomainFields(InputModel):
    service_id: ServiceId
    enabled: bool = False
    default_entry: bool = False


class DomainCreate(DomainFields):
    domain: str

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value):
        value = value.lower()
        if not valid_domain(value):
            raise ValueError("invalid_domain")
        return value


class DomainPatch(InputModel):
    service_id: ServiceId | None = None
    enabled: bool | None = None
    default_entry: bool | None = None

    @model_validator(mode="after")
    def disallow_null_fields(self):
        if not self.model_fields_set or any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("invalid_patch")
        return self


def management_router(resolver, *, provider=None, bridge=None) -> APIRouter:
    store = ManagementStore(resolver)
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        expected = resolver.env.get("SERVICE_MANAGEMENT_TOKEN", "")
        if len(expected) < 32:
            raise RuntimeFailure("management_auth_not_configured")
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), expected.encode()
        ):
            raise RuntimeFailure("unauthorized", 401)

    router = APIRouter(prefix="/management", dependencies=[Depends(authenticate)])
    from .profile_api import profile_router
    router.include_router(profile_router(resolver))
    from .model_api import model_router
    router.include_router(model_router(resolver, provider))
    if bridge is not None:
        from .chatwoot_api import management_routes
        router.include_router(management_routes(bridge))
        from .support_api import support_router
        router.include_router(support_router(bridge))
        from .support_entries import entries_router
        router.include_router(entries_router(bridge))
    from .knowledge import KnowledgeStore, MAX_CONTENT_BYTES
    knowledge = KnowledgeStore(resolver)

    @router.get("/services/{service_id}/knowledge")
    def read_knowledge(service_id: ServicePath, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return knowledge.content(service_id)

    @router.api_route("/services/{service_id}/knowledge", methods=["PUT", "DELETE"])
    async def write_knowledge(service_id: ServicePath, request: Request, response: Response):
        # Bound raw JSON as well as decoded UTF-8. Authentication has already
        # run; error responses never echo validation input or document text.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_CONTENT_BYTES * 6 + 1024:
                raise RuntimeFailure("knowledge_too_large", 413)
        try:
            model = KnowledgeClear if request.method == "DELETE" else KnowledgeWrite
            body = model.model_validate(json.loads(raw))
        except (ValueError, UnicodeError, ValidationError, RecursionError):
            raise RuntimeFailure("invalid_request", 422) from None
        response.headers["Cache-Control"] = "no-store"
        return await run_in_threadpool(knowledge.content, service_id,
                                      content="" if request.method == "DELETE" else body.content,
                                      revision=body.revision)

    def domain_key(domain):
        domain = domain.lower()
        if not valid_domain(domain):
            raise RuntimeFailure("invalid_domain", 422)
        return domain

    def get_record(kind, key):
        services, domains = store.read_state()
        records = services if kind == "service" else domains
        if key not in records:
            raise RuntimeFailure("record_not_found", 404)
        return records[key]

    @router.get("/services", response_model=list[ServiceCreate])
    def list_services():
        services, _ = store.read_state()
        return [services[key] for key in sorted(services)]

    @router.post("/migrations/import-yaml")
    def import_yaml():
        return store.migrate_yaml()

    @router.post("/services", status_code=201, response_model=ServiceCreate)
    def create_service(body: ServiceCreate):
        return store.mutate("service", "create", body.id, body.model_dump())

    @router.get("/services/{service_id}", response_model=ServiceCreate)
    def get_service(service_id: ServicePath):
        return get_record("service", service_id)

    @router.put("/services/{service_id}", response_model=ServiceCreate)
    def replace_service(service_id: ServicePath, body: ServiceFields):
        return store.mutate("service", "update", service_id, body.model_dump())

    @router.patch("/services/{service_id}", response_model=ServiceCreate)
    def update_service(service_id: ServicePath, body: ServicePatch):
        return store.mutate("service", "update", service_id, body.model_dump(exclude_unset=True))

    @router.post("/services/{service_id}/enable", response_model=ServiceCreate)
    def enable_service(service_id: ServicePath):
        return store.mutate("service", "update", service_id, {"enabled": True})

    @router.post("/services/{service_id}/disable", response_model=ServiceCreate)
    def disable_service(service_id: ServicePath):
        return store.mutate("service", "update", service_id, {"enabled": False})

    @router.delete("/services/{service_id}", status_code=204)
    def delete_service(service_id: ServicePath):
        store.mutate("service", "delete", service_id)
        return Response(status_code=204)

    @router.get("/domains", response_model=list[DomainCreate])
    def list_domains():
        _, domains = store.read_state()
        return [domains[key] for key in sorted(domains)]

    @router.post("/domains", status_code=201, response_model=DomainCreate)
    def create_domain(body: DomainCreate):
        return store.mutate("domain", "create", body.domain, body.model_dump())

    @router.get("/domains/{domain}", response_model=DomainCreate)
    def get_domain(domain: str):
        return get_record("domain", domain_key(domain))

    @router.put("/domains/{domain}", response_model=DomainCreate)
    def replace_domain(domain: str, body: DomainFields):
        return store.mutate("domain", "update", domain_key(domain), body.model_dump())

    @router.patch("/domains/{domain}", response_model=DomainCreate)
    def update_domain(domain: str, body: DomainPatch):
        return store.mutate("domain", "update", domain_key(domain), body.model_dump(exclude_unset=True))

    @router.post("/domains/{domain}/enable", response_model=DomainCreate)
    def enable_domain(domain: str):
        return store.mutate("domain", "update", domain_key(domain), {"enabled": True})

    @router.post("/domains/{domain}/disable", response_model=DomainCreate)
    def disable_domain(domain: str):
        # Removing an active default also removes its default-entry flag.
        return store.mutate("domain", "update", domain_key(domain), {"enabled": False, "default_entry": False})

    @router.delete("/domains/{domain}", status_code=204)
    def delete_domain(domain: str):
        store.mutate("domain", "delete", domain_key(domain))
        return Response(status_code=204)

    return router
