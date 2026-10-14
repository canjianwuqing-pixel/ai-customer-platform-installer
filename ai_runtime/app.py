"""FastAPI entry point. The internal token guards every generation request."""

import hmac
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .config import RuntimeFailure, ServiceResolver
from .engine import RuntimeEngine
from .management_api import management_router


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    message: str = Field(min_length=1, max_length=32000)
    service_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class ChatResponse(BaseModel):
    service_id: str
    reply: str


def create_app(engine: RuntimeEngine | None = None) -> FastAPI:
    engine = engine or RuntimeEngine(ServiceResolver.from_environment())
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    bearer = HTTPBearer(auto_error=False)
    logger = logging.getLogger("ai_runtime")
    # Third-party HTTP debug logging can include provider URLs and headers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        expected = engine.resolver.env.get("INTERNAL_API_TOKEN", "")
        if not expected:
            raise RuntimeFailure("internal_auth_not_configured")
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), expected.encode()
        ):
            raise RuntimeFailure("unauthorized", 401)

    @application.exception_handler(RuntimeFailure)
    async def runtime_failure(_request: Request, error: RuntimeFailure):
        logger.warning("AI request rejected code=%s", error.code)
        return JSONResponse(status_code=error.status, content={"error": error.code})

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError):
        # FastAPI's default detail includes input values; never echo them here.
        return JSONResponse(status_code=422, content={"error": "invalid_request"})

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    @application.post("/chat", response_model=ChatResponse, dependencies=[Depends(authenticate)])
    async def chat(body: ChatRequest):
        return await engine.respond(body.message, body.service_id)

    application.include_router(management_router(engine.resolver))
    return application


app = create_app()
