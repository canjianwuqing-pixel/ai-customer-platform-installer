"""One public website inbox entry, separate from service/domain routing.

All these endpoints are under the existing management bearer dependency.
Only the website token (already public in Chatwoot's embed code) is exposed.
"""
import json
import re

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .chatwoot import ChatwootClient, server_url
from .config import RuntimeFailure, SERVICE_ID


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    service_id: str = Field(max_length=64)


class OriginChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    old_url: str = Field(max_length=2048)
    new_url: str = Field(max_length=2048)


async def add_local_alias(bridge, old_url, new_url):
    old_url, new_url = server_url(old_url), server_url(new_url)
    if not new_url.startswith("https://"):
        raise RuntimeFailure("chatwoot_https_required", 422)
    with bridge.store._connection(write=True) as db:
        row = db.execute("SELECT config FROM runtime_profiles WHERE kind='website-origin' AND key='local'").fetchone()
        local = set(json.loads(row[0])) if row else {bridge.resolver.env.get("CHATWOOT_LOCAL_ORIGIN", "")}
        if old_url not in local:
            raise RuntimeFailure("invalid_local_origin", 409)
        local.add(new_url)
        db.execute("INSERT INTO runtime_profiles VALUES ('website-origin','local',?) ON CONFLICT(kind,key) DO UPDATE SET config=excluded.config", (json.dumps(sorted(local)),))
    return {"status": "updated"}


class SupportEntry:
    def __init__(self, bridge):
        self.bridge, self.store = bridge, bridge.store

    def selected(self):
        with self.store._connection() as db:
            if not self.bridge._ready(db):
                return ""
            row = db.execute("SELECT config FROM runtime_profiles WHERE kind='support-entry' AND key='default'").fetchone()
        return json.loads(row[0])["service_id"] if row else ""

    async def options(self):
        result = []
        for sid, service in self.store.read_services().items():
            binding = self.bridge.binding(sid)
            if not service["enabled"] or not binding or not binding["inbox_id"]:
                continue
            try:
                inboxes = await self.bridge.inboxes(sid)
            except RuntimeFailure:
                # An unrelated external connection must not take down the
                # selected platform website inbox or other working options.
                continue
            inbox = next((row for row in inboxes if row["id"] == binding["inbox_id"]), None)
            if inbox:
                result.append({"service_id": sid, "name": service["name"], "inbox_name": inbox["name"]})
        return result

    async def select(self, service_id):
        if not SERVICE_ID.fullmatch(service_id) or service_id not in {r["service_id"] for r in await self.options()}:
            raise RuntimeFailure("invalid_support_inbox", 422)
        with self.store._connection(write=True) as db:
            db.execute("INSERT INTO runtime_profiles(kind,key,config) VALUES ('support-entry','default',?) ON CONFLICT(kind,key) DO UPDATE SET config=excluded.config", (json.dumps({"service_id": service_id}),))
        return {"status": "saved"}

    async def public(self):
        services = self.store.read_services()
        candidates = [sid for sid, service in services.items() if service["enabled"]
                      and (self.bridge.binding(sid) or {}).get("inbox_id")]
        selected = self.selected() or (candidates[0] if len(candidates) == 1 else "")
        if selected not in candidates:
            raise RuntimeFailure("support_inbox_not_selected", 409)
        binding = self.bridge.binding(selected)
        inbox = await self.bridge.client(binding).call("GET", f"/inboxes/{binding['inbox_id']}")
        token = inbox.get("website_token")
        if (inbox.get("id") != binding["inbox_id"] or inbox.get("channel_type") != "Channel::WebWidget"
                or not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,256}", token)):
            raise RuntimeFailure("invalid_support_inbox", 422)
        # No hmac_token, API/bot tokens, account IDs or connection URLs.
        return {"website_token": token}


async def change_origin(bridge, old_url, new_url, *, retire=False):
    old_url, new_url = server_url(old_url), server_url(new_url)
    if not new_url.startswith("https://"):
        raise RuntimeFailure("chatwoot_https_required", 422)
    async with bridge.setup_lock:
        changes = []
        for sid in bridge.store.read_services():
            binding = bridge.binding(sid)
            if not binding or not binding["bot_id"]:
                continue
            # Use the already-trusted local transport even if the old hostname
            # is down. Never recreate bots or send credentials to an unverified
            # browser-selected server.
            client = bridge.client(binding)
            bot = await client.call("GET", f"/agent_bots/{binding['bot_id']}")
            if not isinstance(bot, dict):
                raise RuntimeFailure("chatwoot_invalid_response", 502)
            outgoing = bot.get("outgoing_url", "")
            if not isinstance(outgoing, str):
                raise RuntimeFailure("chatwoot_invalid_response", 502)
            if outgoing.startswith(old_url + "/integrations/chatwoot/"):
                changes.append((sid, binding, client, outgoing))
        patched = []
        try:
            for sid, binding, client, outgoing in changes:
                # PATCH preserves identity, token, secret, inbox and enabled flag.
                # Include an ambiguous timeout in rollback as PATCH is
                # idempotent and may already have changed the remote record.
                patched.append((binding, client, outgoing))
                await client.call("PATCH", f"/agent_bots/{binding['bot_id']}", {"outgoing_url": new_url + outgoing[len(old_url):]})
            with bridge.store._connection(write=True) as db:
                db.execute("UPDATE chatwoot_bindings SET base_url=? WHERE base_url=?", (new_url, old_url))
                row = db.execute("SELECT config FROM runtime_profiles WHERE kind='website-origin' AND key='local'").fetchone()
                local = set(json.loads(row[0])) if row else {bridge.resolver.env.get("CHATWOOT_LOCAL_ORIGIN", "")}
                if old_url in local:
                    local.add(new_url)
                if retire:
                    local.discard(old_url)
                db.execute("INSERT INTO runtime_profiles VALUES ('website-origin','local',?) ON CONFLICT(kind,key) DO UPDATE SET config=excluded.config", (json.dumps(sorted(local)),))
        except Exception:
            rollback_failed = False
            for binding, client, outgoing in reversed(patched):
                try:
                    await client.call("PATCH", f"/agent_bots/{binding['bot_id']}", {"outgoing_url": outgoing})
                except RuntimeFailure:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeFailure("website_origin_recovery_required", 503) from None
            raise
    return {"status": "updated"}


def support_router(bridge):
    router = APIRouter()
    entry = SupportEntry(bridge)

    @router.get("/support-entry")
    async def state():
        return {"options": await entry.options(), "selected": entry.selected()}

    @router.put("/support-entry")
    async def save(body: Selection):
        return await entry.select(body.service_id)

    @router.get("/support-entry/public")
    async def public():
        return await entry.public()

    @router.post("/website-origin")
    async def origin(body: OriginChange):
        return await change_origin(bridge, body.old_url, body.new_url)

    @router.post("/website-alias")
    async def alias(body: OriginChange):
        return await add_local_alias(bridge, body.old_url, body.new_url)

    @router.post("/website-retire")
    async def retire(body: OriginChange):
        return await change_origin(bridge, body.old_url, body.new_url, retire=True)

    return router
