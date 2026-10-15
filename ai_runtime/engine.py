"""Request-local prompt/knowledge assembly and a generic chat API adapter."""

import logging

import httpx
from starlette.concurrency import run_in_threadpool

from .config import RuntimeConfig, RuntimeFailure, ServiceResolver


logger = logging.getLogger("ai_runtime")


def build_messages(config: RuntimeConfig, message: str, history=None) -> list[dict[str, str]]:
    messages = []
    instructions = [config.system_prompt, *config.reply_rules]
    if any(instructions):
        messages.append({"role": "system", "content": "\n".join(text for text in instructions if text)})
    if config.knowledge_source is not None:
        from .knowledge import read_runtime_knowledge
        context = read_runtime_knowledge(config)
        if context:
            # Reference material has a separate user role; it is not system policy.
            messages.append({"role": "user", "content": f"<knowledge_context>\n{context}\n</knowledge_context>"})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})
    return messages


class ChatProvider:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def reply(self, config: RuntimeConfig, credential: str, messages: list[dict]) -> str:
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=60, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.post(
                    config.api_base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + credential},
                    json={"model": config.model, "messages": messages},
                )
            if not response.is_success:
                raise RuntimeFailure("provider_request_failed", 502)
            reply = response.json()["choices"][0]["message"]["content"]
            if not isinstance(reply, str) or not reply:
                raise RuntimeFailure("provider_invalid_response", 502)
            return reply
        except httpx.TimeoutException:
            raise RuntimeFailure("provider_timeout", 504) from None
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise RuntimeFailure("provider_invalid_response", 502) from None


class RuntimeEngine:
    def __init__(self, resolver: ServiceResolver, provider: ChatProvider | None = None):
        self.resolver = resolver
        self.provider = provider or ChatProvider()

    async def respond(self, message: str, service_id: str | None = None, *, history=None, redact_values=()) -> dict[str, str]:
        config = await run_in_threadpool(self.resolver.resolve, service_id)
        credential = self.resolver.credential(config)
        messages = await run_in_threadpool(build_messages, config, message, history)
        from .config import RESERVED_CREDENTIALS
        protected = [credential, *redact_values, *(value for key, value in self.resolver.env.items() if key in RESERVED_CREDENTIALS and value)]
        for item in messages:
            for secret in protected:
                if secret:
                    item["content"] = item["content"].replace(secret, "[已隐藏凭据]")
        reply = await self.provider.reply(config, credential, messages)
        for secret in protected:
            if secret:
                reply = reply.replace(secret, "[已隐藏凭据]")
        logger.info("AI reply completed service_id=%s", config.service_id)
        return {"service_id": config.service_id, "reply": reply}
