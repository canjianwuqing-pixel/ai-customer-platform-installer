"""Fixed, server-to-server endpoints. No browser-supplied URLs or tokens."""

import httpx

from .auth import AdminError


SAFE_ERRORS = {"invalid_service_binding", "bound_service_is_disabled", "multiple_default_domains",
    "default_domain_must_be_enabled", "service_has_domain_bindings", "record_already_exists",
    "record_not_found", "invalid_request", "invalid_profile_configuration", "invalid_runtime_configuration",
    "invalid_default_service", "default_service_must_remain_enabled", "maintenance_rejected",
    "unknown_service_id", "knowledge_not_configured", "unsafe_knowledge_path", "knowledge_too_large",
    "invalid_knowledge_content", "knowledge_conflict", "knowledge_storage_unavailable"}


class InternalClient:
    def __init__(self, base_url, token, transport=None):
        if not base_url.startswith(("http://", "https://")) or len(token) < 32:
            raise AdminError("internal_api_not_configured", 503)
        self.base_url, self.token, self.transport = base_url.rstrip("/"), token, transport

    async def authentication_enforced(self, path):
        try:
            async with httpx.AsyncClient(base_url=self.base_url, transport=self.transport,
                                         trust_env=False, timeout=5, follow_redirects=False) as client:
                response = await client.get(path)
            return response.status_code in {401, 403}
        except httpx.HTTPError:
            return False

    async def call(self, method, path, body=None):
        try:
            async with httpx.AsyncClient(base_url=self.base_url, transport=self.transport,
                                         trust_env=False, timeout=30, follow_redirects=False) as client:
                response = await client.request(method, path, json=body,
                                                headers={"Authorization": "Bearer " + self.token})
            if response.status_code >= 300:
                # Never forward an upstream error body, URL, header or traceback.
                code = "internal_operation_rejected"
                try:
                    upstream = response.json()
                    if isinstance(upstream, dict) and upstream.get("error") in SAFE_ERRORS:
                        code = upstream["error"]
                except (ValueError, TypeError):
                    pass
                raise AdminError(code, 409 if response.status_code < 500 else 503)
            return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError):
            raise AdminError("internal_api_unavailable", 503) from None
