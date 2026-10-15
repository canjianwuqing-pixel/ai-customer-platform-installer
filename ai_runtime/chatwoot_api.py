"""Internal management operations and the separately HMAC-authenticated webhook."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .config import RuntimeFailure


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Connection(Input):
    base_url: str = Field(max_length=2048)
    account_id: int = Field(gt=0, le=2_147_483_647)
    api_token: str = Field(default="", max_length=4096, repr=False)


class Binding(Input):
    inbox_id: int = Field(gt=0, le=2_147_483_647)
    public_url: str = Field(max_length=2048)


class Enabled(Input):
    enabled: bool


def management_routes(bridge):
    router = APIRouter(prefix="/chatwoot")

    @router.get("/{service_id}")
    def state(service_id: str):
        return bridge.state(service_id)

    @router.get("/{service_id}/inboxes")
    async def inboxes(service_id: str):
        return await bridge.inboxes(service_id)

    @router.put("/{service_id}/connection")
    async def connect(service_id: str, body: Connection):
        return await bridge.connect(service_id, body.base_url, body.account_id, body.api_token)

    @router.post("/{service_id}/bind")
    async def bind(service_id: str, body: Binding):
        return await bridge.bind(service_id, body.inbox_id, body.public_url)

    @router.put("/{service_id}/enabled")
    def enabled(service_id: str, body: Enabled):
        return bridge.toggle(service_id, body.enabled)

    @router.post("/{service_id}/conversations/{conversation_id}/resume")
    async def resume(service_id: str, conversation_id: int):
        from .chatwoot import identifier
        return await bridge.resume(service_id, identifier(conversation_id))

    return router


def webhook_routes(bridge):
    router = APIRouter()

    @router.post("/integrations/chatwoot/{service_id}/webhook")
    async def webhook(service_id: str, request: Request):
        if (len(request.headers.getlist("x-chatwoot-timestamp")) != 1
                or len(request.headers.getlist("x-chatwoot-signature")) != 1):
            raise RuntimeFailure("unauthorized", 401)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1_048_576:
                raise RuntimeFailure("invalid_chatwoot_event", 413)
        try:
            result = await run_in_threadpool(bridge.accept, service_id, bytes(raw), request.headers["x-chatwoot-timestamp"], request.headers["x-chatwoot-signature"])
        except RuntimeFailure as error:
            # 4.18 AgentBots::WebhookJob retries 429/500, not a generic 503.
            # A failed durable intake must request that bounded upstream retry.
            if error.status >= 500:
                raise RuntimeFailure(error.code, 500) from None
            raise
        bridge.wakeup.set()
        return result

    return router
