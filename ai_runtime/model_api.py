"""Write-only model credentials and live OpenAI-compatible discovery/probes."""

import asyncio
from dataclasses import replace

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
import httpx
from starlette.concurrency import run_in_threadpool

from .config import RuntimeFailure, REFERENCE, RESERVED_CREDENTIALS
from .engine import ChatProvider
from .management_store import ManagementStore
from .profile_api import ModelProfile
from .vault import CredentialVault, masked, validate_secret


class PrivateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ModelSave(PrivateInput):
    id: str
    profile: ModelProfile
    api_key: str = Field(default="", max_length=4096, repr=False)


class ModelProbe(PrivateInput):
    profile_id: str = ""
    model: str = Field(default="", max_length=256)
    api_base_url: str = Field(default="", max_length=2048)
    api_key_env: str = "OPENAI_API_KEY"
    api_key: str = Field(default="", max_length=4096, repr=False)


def model_router(resolver, provider=None):
    router = APIRouter(prefix="/models")
    store, vault = ManagementStore(resolver), CredentialVault(resolver)
    provider = provider or ChatProvider()

    def reference(key):
        if not REFERENCE.fullmatch(key) or any(part in {"", ".", ".."} for part in key.split("/")):
            raise RuntimeFailure("invalid_profile_identifier", 422)

    def configuration(body):
        if body.profile_id:
            reference(body.profile_id)
            store.get_profile("ai", body.profile_id)
        config = resolver._runtime_config("validation", {"model": body.model or "connection-probe",
            "api_base_url": body.api_base_url, "api_key_env": body.api_key_env}, {}, {})
        config = replace(config, credential_ref=body.profile_id)
        credential = validate_secret(body.api_key) if body.api_key else resolver.credential(config)
        if credential in config.model or credential in config.api_base_url:
            raise RuntimeFailure("invalid_profile_configuration", 422)
        if any(credential == resolver.env.get(key) for key in RESERVED_CREDENTIALS):
            raise RuntimeFailure("invalid_api_credential", 422)
        return config, credential

    @router.post("/save")
    def save(body: ModelSave, create: bool = False):
        reference(body.id)
        credential = validate_secret(body.api_key) if body.api_key else None
        protected = credential or vault.get("model", body.id)
        if protected and (protected in body.id or any(protected in str(value) for value in body.profile.model_dump().values())):
            raise RuntimeFailure("invalid_profile_configuration", 422)
        if credential and credential in resolver.env.values():
            # Provider environment keys are explicitly reusable; installation
            # control tokens/passwords must never be accepted as model keys.
            if any(credential == resolver.env.get(key) for key in RESERVED_CREDENTIALS):
                raise RuntimeFailure("invalid_api_credential", 422)
        return store.save_profile("ai", body.id, body.profile.model_dump(), create=create, credential=credential)

    @router.post("/discover")
    async def discover(body: ModelProbe):
        config, credential = await run_in_threadpool(configuration, body)
        try:
            async with httpx.AsyncClient(transport=provider.transport, timeout=20, trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", config.api_base_url + "/models", headers={"Authorization": "Bearer " + credential}) as response:
                    if response.status_code != 200:
                        raise RuntimeFailure("model_list_unavailable", 422)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1_048_576:
                            raise RuntimeFailure("model_list_unavailable", 422)
            import json
            items = json.loads(raw)["data"]
            if not isinstance(items, list):
                raise ValueError
            ids = sorted({item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)
                          and 0 < len(item["id"]) <= 256})
            if any(credential in model_id for model_id in ids):
                raise RuntimeFailure("model_list_unavailable", 422)
            return {"models": ids[:1000]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise RuntimeFailure("model_list_unavailable", 422) from None

    @router.post("/test")
    async def test(body: ModelProbe):
        if not body.model.strip():
            raise RuntimeFailure("invalid_profile_configuration", 422)
        config, credential = await run_in_threadpool(configuration, body)
        try:
            reply = await asyncio.wait_for(provider.reply(config, credential, [{"role": "user", "content": "Reply with OK."}]), timeout=20)
        except TimeoutError:
            raise RuntimeFailure("provider_timeout", 504) from None
        if credential in reply:
            raise RuntimeFailure("provider_invalid_response", 502)
        return {"status": "connected", "model": config.model}

    @router.get("/{key:path}/credential")
    def credential_status(key: str):
        reference(key)
        profile = store.get_profile("ai", key)
        config = replace(resolver._runtime_config("validation", profile, {}, {}), credential_ref=key)
        stored = vault.get("model", key)
        value = stored if stored is not None else resolver.env.get(config.api_key_env, "")
        if value and any(value == resolver.env.get(name) for name in RESERVED_CREDENTIALS):
            value = ""  # A misconfigured alias must not even reveal a control-token prefix.
        return {"configured": bool(value), "masked": masked(value), "source": "stored" if stored is not None else "environment"}

    return router
