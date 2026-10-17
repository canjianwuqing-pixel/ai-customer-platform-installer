"""Chatwoot 4.18 Agent Bot adapter, durable inbox routing and safe reply queue.

No payload text or credentials go to logs. Delivery with an ambiguous result is
reconciled, never blindly posted again: upstream has no idempotency contract.
"""

import asyncio
import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

from .config import RuntimeFailure, SERVICE_ID
from .config import RESERVED_CREDENTIALS
from .management_store import ManagementStore
from .vault import CredentialVault, masked, validate_secret


MAX_ATTEMPTS = 3
MAX_HISTORY = 20
MAX_CONTEXT_CHARS = 24000
TERMINAL = {"sent", "ignored", "handoff", "failed", "uncertain"}


def identifier(value):
    if type(value) is not int or not 0 < value <= 2_147_483_647:
        raise RuntimeFailure("invalid_chatwoot_identifier", 422)
    return value


def server_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or any(char.isspace() or char in "\\" for char in value)):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise RuntimeFailure("invalid_chatwoot_url", 422) from None
    return value.rstrip("/")


def origin_identity(value):
    parsed = urlsplit(value)
    return parsed.scheme, parsed.hostname, parsed.port or {"http": 80, "https": 443}[parsed.scheme]


def is_incoming(message, *, webhook=False):
    sender = message.get("sender") or {}
    content = message.get("content")
    valid_content = content is None or isinstance(content, str)
    has_text = isinstance(content, str) and bool(content.strip())
    has_attachments = isinstance(message.get("attachments"), list) and bool(message["attachments"])
    contact = ((message.get("conversation") or {}).get("meta") or {}).get("sender") or {}
    # Contact.webhook_data in 4.18 has no `type`, unlike message list API.
    # Verify it against the conversation's typed contact before queuing IDs.
    contact_sender = sender.get("type") in {"contact", "Contact"}
    if webhook and sender.get("type") is None:
        contact_sender = (contact.get("type") == "contact" and type(sender.get("id")) is int
                          and sender["id"] == contact.get("id"))
    return (message.get("message_type") in {0, "incoming"} and message.get("private") is False
            and message.get("content_type", "text") in {0, "text"}
            and not message.get("content_attributes", {}).get("deleted")
            and contact_sender
            and valid_content and (has_text or has_attachments))


def is_human(message):
    return (message.get("message_type") in {1, "outgoing"} and message.get("private") is False
            and message.get("sender", {}).get("type") in {"user", "User"})


def wants_human(text):
    if re.search(r"(?:不需要|不要|不想).{0,4}(?:人工|真人)", text):
        return False
    return bool(re.search(r"人工客服|真人客服|转人工|转接人工|(?:联系|需要|找|我要|想要).{0,6}(?:人工|真人)|\b(?:human|live)\s+(?:agent|support)\b", text, re.I)
                or text.strip() in {"人工", "真人"})


class ChatwootClient:
    def __init__(self, binding, credentials, transport=None, *, attachment_origins=None, attachment_private=False):
        self.binding, self.credentials, self.transport = binding, credentials, transport
        self.attachment_origins = attachment_origins or {origin_identity(binding["base_url"])}
        self.attachment_private = attachment_private

    async def call(self, method, suffix, data=None, *, bot=False, params=None):
        token = self.credentials["bot_token" if bot else "api_token"]
        url = self.binding["base_url"] + "/api/v1/accounts/" + str(self.binding["account_id"]) + suffix
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False, trust_env=False) as client:
                async with client.stream(method, url, json=data, params=params, headers={"api_access_token": token}) as response:
                    if response.status_code == 429:
                        raise RuntimeFailure("chatwoot_rate_limited", 429)
                    if not response.is_success:
                        raise RuntimeFailure("chatwoot_request_failed", 502)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2_097_152:
                            raise RuntimeFailure("chatwoot_invalid_response", 502)
            return json.loads(raw) if raw else {}
        except httpx.TimeoutException:
            raise RuntimeFailure("chatwoot_timeout", 504) from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise RuntimeFailure("chatwoot_invalid_response", 502) from None

    async def conversation(self, conversation_id):
        result = await self.call("GET", f"/conversations/{conversation_id}")
        if (not isinstance(result, dict) or result.get("id") != conversation_id
                or result.get("inbox_id") != self.binding["inbox_id"]):
            raise RuntimeFailure("chatwoot_conversation_mismatch", 409)
        return result

    async def messages(self, conversation_id, **params):
        result = await self.call("GET", f"/conversations/{conversation_id}/messages", params=params)
        if not isinstance(result, dict) or not isinstance(result.get("payload"), list):
            raise RuntimeFailure("chatwoot_invalid_response", 502)
        return result["payload"]


class ChatwootBridge:
    def __init__(self, engine, transport=None):
        self.engine, self.resolver, self.transport = engine, engine.resolver, transport
        self.store, self.vault = ManagementStore(self.resolver), CredentialVault(self.resolver)
        self.stopping = asyncio.Event()
        self.wakeup = asyncio.Event()
        self.setup_lock = asyncio.Lock()

    def _service(self, service_id):
        if not SERVICE_ID.fullmatch(service_id) or service_id not in self.store.read_services():
            raise RuntimeFailure("unknown_service_id", 404)

    @staticmethod
    def _ready(db):
        return db is not None and db.execute("PRAGMA user_version").fetchone()[0] >= 3

    def binding(self, service_id):
        self._service(service_id)
        with self.store._connection() as db:
            if not self._ready(db):
                return None
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM chatwoot_bindings WHERE service_id=?", (service_id,)).fetchone()
            return dict(row) if row else None

    def local_origins(self):
        # The platform's own Rails is reached over the private Docker network,
        # not through a possibly failed public primary DNS name. External
        # Chatwoot connections keep their explicitly configured URL.
        local = {self.resolver.env.get("CHATWOOT_LOCAL_ORIGIN", "")} - {""}
        with self.store._connection() as db:
            if self._ready(db):
                row = db.execute("SELECT config FROM runtime_profiles WHERE kind='website-origin' AND key='local'").fetchone()
                if row:
                    local = set(json.loads(row[0])) - {""}
        return local

    def require_unique_inbox(self, binding, inbox_id):
        local = {origin_identity(value) for value in self.local_origins()}
        current = origin_identity(binding["base_url"])
        with self.store._connection() as db:
            if not self._ready(db):
                return
            for sid, origin in db.execute("SELECT service_id,base_url FROM chatwoot_bindings WHERE account_id=? AND inbox_id=?",
                                          (binding["account_id"], inbox_id)):
                candidate = origin_identity(origin)
                if sid != binding["service_id"] and (candidate == current or {candidate, current} <= local):
                    raise RuntimeFailure("chatwoot_inbox_in_use", 409)

    def is_local_origin(self, value):
        return origin_identity(value) in {origin_identity(origin) for origin in self.local_origins()}

    def client(self, binding):
        credentials = self.vault.get("chatwoot", binding["service_id"])
        if not isinstance(credentials, dict):
            raise RuntimeFailure("chatwoot_not_configured", 409)
        origins = {origin_identity(binding["base_url"])}
        internal = self.is_local_origin(binding["base_url"]) and bool(self.resolver.env.get("CHATWOOT_INTERNAL_URL"))
        if internal:
            origins.update(origin_identity(value) for value in self.local_origins())
            binding = {**binding, "base_url": server_url(self.resolver.env["CHATWOOT_INTERNAL_URL"])}
            origins.add(origin_identity(binding["base_url"]))
        return ChatwootClient(binding, credentials, self.transport, attachment_origins=origins, attachment_private=internal)

    def state(self, service_id):
        binding = self.binding(service_id)
        if not binding:
            return {"configured": False, "enabled": False, "inboxes": [], "conversations": []}
        credentials = self.vault.get("chatwoot", service_id)
        with self.store._connection() as db:
            rows = db.execute("SELECT conversation_id,paused,reason FROM chatwoot_conversations WHERE service_id=? ORDER BY conversation_id DESC LIMIT 100", (service_id,)).fetchall()
            errors = db.execute("SELECT conversation_id,message_id,status,error FROM chatwoot_events WHERE service_id=? AND status IN ('failed','uncertain') ORDER BY created_at DESC LIMIT 20", (service_id,)).fetchall()
        return {**binding, "configured": True, "enabled": bool(binding["enabled"]),
                "token_masked": masked(credentials["api_token"]),
                "conversations": [{"id": row[0], "paused": bool(row[1]), "reason": row[2]} for row in rows],
                "errors": [{"conversation_id": r[0], "message_id": r[1], "status": r[2], "error": r[3]} for r in errors]}

    async def connect(self, service_id, base_url, account_id, api_token=""):
        self._service(service_id)
        base_url, account_id = server_url(base_url), identifier(account_id)
        async with self.setup_lock:
            previous = self.binding(service_id)
            if previous and previous["bot_id"] and (previous["base_url"], previous["account_id"]) != (base_url, account_id):
                raise RuntimeFailure("chatwoot_binding_immutable", 409)
            credentials = self.vault.get("chatwoot", service_id) or {}
            if api_token:
                if any(api_token == self.resolver.env.get(key) for key in RESERVED_CREDENTIALS):
                    raise RuntimeFailure("invalid_api_credential", 422)
                credentials["api_token"] = validate_secret(api_token)
            if not credentials.get("api_token"):
                raise RuntimeFailure("invalid_api_credential", 422)
            if any(value and value in base_url for value in credentials.values()):
                raise RuntimeFailure("invalid_chatwoot_url", 422)
            binding = {"service_id": service_id, "base_url": base_url, "account_id": account_id}
            client = ChatwootClient(binding, credentials, self.transport)
            inboxes = await self._inboxes(client)
            if previous and previous["bot_id"]:
                bot = await client.call("GET", f"/agent_bots/{previous['bot_id']}")
                credentials.update(self._bot_credentials(bot))
            with self.store._connection(write=True) as db:
                db.execute("INSERT INTO chatwoot_bindings(service_id,base_url,account_id) VALUES (?,?,?) ON CONFLICT(service_id) DO UPDATE SET base_url=excluded.base_url,account_id=excluded.account_id,last_error=''", (service_id, base_url, account_id))
                self.vault.put(db, "chatwoot", service_id, credentials)
            return {**self.state(service_id), "inboxes": inboxes}

    @staticmethod
    def _bot_credentials(bot):
        token = bot.get("access_token")
        token = token.get("token") if isinstance(token, dict) else token
        return {"bot_token": validate_secret(token), "webhook_secret": validate_secret(bot.get("secret"))}

    async def _inboxes(self, client):
        result = await client.call("GET", "/inboxes")
        if not isinstance(result, dict) or not isinstance(result.get("payload"), list):
            raise RuntimeFailure("chatwoot_invalid_response", 502)
        return [{"id": identifier(row["id"]), "name": str(row.get("name", row["id"]))[:128]}
                for row in result["payload"] if isinstance(row, dict) and row.get("channel_type") == "Channel::WebWidget"]

    async def inboxes(self, service_id):
        binding = self.binding(service_id)
        if not binding:
            return []
        return await self._inboxes(self.client(binding))

    async def bind(self, service_id, inbox_id, public_url):
        self._service(service_id)
        inbox_id = identifier(inbox_id)
        public_url = server_url(public_url)
        if not public_url.startswith("https://"):
            raise RuntimeFailure("chatwoot_https_required", 422)
        # Enabling a bot without a functioning instance/key would trap visitors
        # in pending conversations, so validate before changing Chatwoot.
        self.resolver.credential(self.resolver.resolve(service_id))
        async with self.setup_lock:
            binding = self.binding(service_id)
            if not binding:
                raise RuntimeFailure("chatwoot_not_configured", 409)
            if binding["inbox_id"] not in {None, inbox_id}:
                raise RuntimeFailure("chatwoot_binding_immutable", 409)
            client = self.client(binding)
            if inbox_id not in {row["id"] for row in await self._inboxes(client)}:
                raise RuntimeFailure("chatwoot_invalid_inbox", 422)
            self.require_unique_inbox(binding, inbox_id)
            existing = await client.call("GET", f"/inboxes/{inbox_id}/agent_bot")
            existing_id = (existing.get("agent_bot") or {}).get("id")
            if existing_id and existing_id != binding["bot_id"]:
                raise RuntimeFailure("chatwoot_existing_bot", 409)
            outgoing_url = public_url + "/integrations/chatwoot/" + service_id + "/webhook"
            bot_id = binding["bot_id"]
            if bot_id:
                bot = await client.call("PATCH", f"/agent_bots/{bot_id}", {"outgoing_url": outgoing_url})
            else:
                bot = await client.call("POST", "/agent_bots", {"name": self.store.read_services()[service_id]["name"],
                    "description": "Service Instance " + service_id, "outgoing_url": outgoing_url, "bot_type": "webhook"})
                bot_id = identifier(bot.get("id"))
            credentials = {**client.credentials, **self._bot_credentials(bot)}
            # Persist the bot's actual HMAC secret before activating its webhook.
            with self.store._connection(write=True) as db:
                db.execute("UPDATE chatwoot_bindings SET inbox_id=?,bot_id=?,enabled=0 WHERE service_id=?", (inbox_id, bot_id, service_id))
                self.vault.put(db, "chatwoot", service_id, credentials)
            await client.call("POST", f"/inboxes/{inbox_id}/set_agent_bot", {"agent_bot": bot_id})
            with self.store._connection(write=True) as db:
                db.execute("UPDATE chatwoot_bindings SET enabled=1,last_error='' WHERE service_id=?", (service_id,))
            return self.state(service_id)

    def toggle(self, service_id, enabled):
        binding = self.binding(service_id)
        if not binding or not binding["bot_id"]:
            raise RuntimeFailure("chatwoot_not_configured", 409)
        if enabled:
            self.resolver.credential(self.resolver.resolve(service_id))
        with self.store._connection(write=True) as db:
            db.execute("UPDATE chatwoot_bindings SET enabled=? WHERE service_id=?", (int(enabled), service_id))
        return self.state(service_id)

    def pause(self, service_id, conversation_id, reason="human_takeover", *, message_id=None):
        with self.store._connection(write=True) as db:
            db.execute("INSERT INTO chatwoot_conversations(service_id,conversation_id,paused,reason,generation) VALUES (?,?,1,?,1) ON CONFLICT(service_id,conversation_id) DO UPDATE SET paused=1,awaiting_reopen=0,reason=excluded.reason,generation=generation+1 WHERE ? IS NULL OR resume_after<?", (service_id, conversation_id, reason, message_id, message_id))

    def conversation_state(self, service_id, conversation_id):
        with self.store._connection() as db:
            row = db.execute("SELECT paused,resume_after FROM chatwoot_conversations WHERE service_id=? AND conversation_id=?", (service_id, conversation_id)).fetchone()
        return (bool(row[0]), row[1]) if row else (False, 0)

    def round_state(self, service_id, conversation_id):
        defaults = {"paused": 0, "resume_after": 0, "awaiting_reopen": 0, "generation": 0, "lifecycle_at": 0}
        with self.store._connection() as db:
            if not self._ready(db):
                return defaults
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM chatwoot_conversations WHERE service_id=? AND conversation_id=?", (service_id, conversation_id)).fetchone()
        return {**defaults, **dict(row)} if row else defaults

    def lifecycle(self, service_id, conversation):
        """Use signed, source-ordered round boundaries, never a recovery timeout.

        4.18's active inbox bot receives these even when a User owns the
        conversation. Its messages array contains the last public chat message.
        Reopening dispatches status_changed BEFORE message_created, with that
        new incoming message already committed. It must not become the fence.
        """
        cid = identifier(conversation["id"])
        at = conversation.get("updated_at")
        status = conversation.get("status")
        if (type(at) not in {float, int} or not math.isfinite(at) or at <= 0
                or status not in {"open", "pending", "resolved", "snoozed"}):
            raise ValueError
        changes = conversation.get("changed_attributes") or []
        if not isinstance(changes, list) or not all(isinstance(item, dict) for item in changes):
            raise ValueError
        reopened = any(item.get("status", {}).get("previous_value") == "resolved"
                       and item.get("status", {}).get("current_value") == "pending" for item in changes)
        if status == "pending" and not reopened:
            return {"status": "ignored"}
        messages = conversation.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError
        cursor = max((identifier(row["id"]) for row in messages), default=0)
        if reopened:
            # A pending transition alone does not authorize an old message.
            # Only the incoming message which reopened a resolved round is new.
            incoming = [identifier(row["id"]) for row in messages if is_incoming(row)]
            if not incoming:
                return {"status": "ignored"}
            cursor = min(incoming) - 1
        with self.store._connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO chatwoot_conversations(service_id,conversation_id) VALUES (?,?)", (service_id, cid))
            previous = db.execute("SELECT lifecycle_at,resume_after FROM chatwoot_conversations WHERE service_id=? AND conversation_id=?", (service_id, cid)).fetchone()
            if at <= previous[0]:
                return {"status": "ignored"}
            fence = max(previous[1], cursor)
            db.execute("UPDATE chatwoot_conversations SET paused=1,awaiting_reopen=?,resume_after=?,lifecycle_at=?,reason=?,generation=generation+1 WHERE service_id=? AND conversation_id=?",
                       (int(status == "resolved" or reopened), fence, at,
                        "conversation_resolved" if status == "resolved" or reopened else "human_takeover", service_id, cid))
            db.execute("UPDATE chatwoot_events SET status='ignored',reply=NULL WHERE service_id=? AND conversation_id=? AND message_id<=? AND status='waiting'", (service_id, cid, fence))
            if reopened and min(incoming) > fence:
                # Sidekiq jobs can be delivered out of order. A message worker
                # may already have deferred this new message while still
                # waiting for its status event. Requeue only that verified
                # reopening message, never sent/uncertain/failed/old work.
                mid = min(incoming)
                db.execute("INSERT OR IGNORE INTO chatwoot_events(service_id,conversation_id,message_id,created_at) VALUES (?,?,?,?)", (service_id, cid, mid, time.time()))
                db.execute("UPDATE chatwoot_events SET status='waiting',next_at=0 WHERE service_id=? AND conversation_id=? AND message_id=? AND status='handoff' AND attempts<?", (service_id, cid, mid, MAX_ATTEMPTS))
        return {"status": "recorded"}

    async def recover_round(self, client, event, conversation):
        """Reassign only a verified NEW incoming message after a resolved round.

        AssignmentService in 4.18 atomically clears the old human assignee and
        sets pending. Merely reopening the browser/open/snoozed is insufficient.
        """
        sid, cid, mid = event["service_id"], event["conversation_id"], event["message_id"]
        state = self.round_state(sid, cid)
        if not state["awaiting_reopen"] or mid <= state["resume_after"] or not client.binding["enabled"]:
            return conversation
        if conversation.get("status") != "pending":
            return conversation
        # Never override an explicitly assigned different bot.
        meta = conversation.get("meta") or {}
        if meta.get("assignee_type") == "AgentBot" and (meta.get("assignee") or {}).get("id") != client.binding["bot_id"]:
            return conversation
        self.resolver.credential(self.resolver.resolve(sid))
        if not self.bot_owns(conversation, client.binding):
            await client.call("POST", f"/conversations/{cid}/assignments",
                              {"assignee_id": client.binding["bot_id"], "assignee_type": "AgentBot"})
        conversation = await client.conversation(cid)
        if not self.bot_owns(conversation, client.binding):
            return conversation
        with self.store._connection(write=True) as db:
            # If a human/close webhook arrived during the API calls, it wins.
            db.execute("UPDATE chatwoot_conversations SET paused=0,awaiting_reopen=0,reason='',generation=generation+1,lifecycle_at=MAX(lifecycle_at,?) WHERE service_id=? AND conversation_id=? AND generation=? AND awaiting_reopen=1",
                       (conversation.get("updated_at", state["lifecycle_at"]), sid, cid, state["generation"]))
        return conversation

    async def resume(self, service_id, conversation_id):
        binding = self.binding(service_id)
        if not binding or not binding["enabled"] or not binding["bot_id"]:
            raise RuntimeFailure("chatwoot_not_enabled", 409)
        self.resolver.credential(self.resolver.resolve(service_id))
        client = self.client(binding)
        await client.conversation(conversation_id)
        rows = await client.messages(conversation_id)
        cursor = max((identifier(row["id"]) for row in rows), default=0)
        await client.call("POST", f"/conversations/{conversation_id}/assignments", {"assignee_id": binding["bot_id"], "assignee_type": "AgentBot"})
        await client.call("POST", f"/conversations/{conversation_id}/toggle_status", {"status": "pending"})
        with self.store._connection(write=True) as db:
            db.execute("INSERT INTO chatwoot_conversations(service_id,conversation_id,paused,resume_after,reason) VALUES (?,?,0,?,'') ON CONFLICT(service_id,conversation_id) DO UPDATE SET paused=0,awaiting_reopen=0,resume_after=MAX(resume_after,excluded.resume_after),reason='',generation=generation+1", (service_id, conversation_id, cursor))
            db.execute("UPDATE chatwoot_events SET status='ignored',reply=NULL WHERE service_id=? AND conversation_id=? AND message_id<=? AND status='waiting'", (service_id, conversation_id, cursor))
        return {"status": "resumed"}

    def accept(self, service_id, raw, timestamp, signature):
        binding = self.binding(service_id)
        if not binding or not binding["bot_id"]:
            raise RuntimeFailure("unauthorized", 401)
        secret = self.vault.get("chatwoot", service_id)["webhook_secret"]
        if (not re.fullmatch(r"[0-9]{10,12}", timestamp) or abs(time.time() - int(timestamp)) > 300
                or not re.fullmatch(r"sha256=[a-f0-9]{64}", signature)
                or not hmac.compare_digest(signature, "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest())):
            raise RuntimeFailure("unauthorized", 401)
        # Even previously saved connections using two aliases of the same local
        # Chatwoot must never ambiguously route one inbox to two knowledge sets.
        self.require_unique_inbox(binding, binding["inbox_id"])
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError
            if event.get("event") in {"conversation_resolved", "conversation_opened", "conversation_status_changed", "conversation_updated"}:
                if identifier(event["account"]["id"]) != binding["account_id"] or identifier(event["inbox_id"]) != binding["inbox_id"]:
                    raise ValueError
                if event["event"] == "conversation_updated" and not any(
                        set(item) & {"status", "assignee_id", "assignee_agent_bot_id", "ai_assignee_type"}
                        for item in (event.get("changed_attributes") or [])):
                    return {"status": "ignored"}
                return self.lifecycle(service_id, event)
            if event.get("event") != "message_created":
                return {"status": "ignored"}
            conversation = event["conversation"]
            if identifier(event["account"]["id"]) != binding["account_id"] or identifier(conversation["inbox_id"]) != binding["inbox_id"]:
                raise ValueError
            if "inbox" in event and identifier(event["inbox"]["id"]) != binding["inbox_id"]:
                raise ValueError
            conversation_id, message_id = identifier(conversation["id"]), identifier(event["id"])
            if is_human(event):
                if message_id <= self.conversation_state(service_id, conversation_id)[1]:
                    return {"status": "ignored"}
                self.pause(service_id, conversation_id, message_id=message_id)
                return {"status": "human_takeover"}
            if not is_incoming(event, webhook=True):
                return {"status": "ignored"}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RuntimeFailure("invalid_chatwoot_event", 422) from None
        with self.store._connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO chatwoot_conversations(service_id,conversation_id) VALUES (?,?)", (service_id, conversation_id))
            db.execute("INSERT OR IGNORE INTO chatwoot_events(service_id,conversation_id,message_id,created_at) VALUES (?,?,?,?)", (service_id, conversation_id, message_id, time.time()))
        return {"status": "accepted"}

    def claim(self):
        # Do not initialize/migrate storage merely by starting a worker.
        with self.store._connection() as db:
            if not self._ready(db):
                return None
            exists = db.execute("SELECT 1 FROM chatwoot_events WHERE status IN ('waiting','working','sending') LIMIT 1").fetchone()
            if not exists:
                return None
        with self.store._connection(write=True) as db:
            db.row_factory = sqlite3.Row
            now = time.time()
            row = db.execute("""SELECT * FROM chatwoot_events e WHERE
                ((status='waiting' AND next_at<=?) OR (status IN ('working','sending') AND lease_until<?))
                AND NOT EXISTS (SELECT 1 FROM chatwoot_events b WHERE b.service_id=e.service_id AND b.conversation_id=e.conversation_id
                    AND b.message_id<>e.message_id AND b.status IN ('working','sending') AND b.lease_until>=?)
                AND NOT EXISTS (SELECT 1 FROM chatwoot_events older WHERE older.service_id=e.service_id AND older.conversation_id=e.conversation_id
                    AND older.message_id<e.message_id AND older.status IN ('waiting','working','sending'))
                ORDER BY message_id LIMIT 1""", (now, now, now)).fetchone()
            if not row:
                return None
            event = dict(row)
            db.execute("UPDATE chatwoot_events SET status=?,attempts=attempts+1,lease_until=? WHERE service_id=? AND conversation_id=? AND message_id=?", ("sending" if row["status"] == "sending" else "working", now + 300, row["service_id"], row["conversation_id"], row["message_id"]))
            event["attempts"] += 1
            return event

    def finish(self, event, status, error="", *, reply=None):
        with self.store._connection(write=True) as db:
            sealed = self.vault.seal(db, reply) if reply is not None else None
            db.execute("UPDATE chatwoot_events SET status=?,error=?,lease_until=?,next_at=?,reply=? WHERE service_id=? AND conversation_id=? AND message_id=?",
                (status, error, time.time() + 300 if status == "sending" else 0, time.time() + min(60, 2 ** event["attempts"]), sealed,
                 event["service_id"], event["conversation_id"], event["message_id"]))
            db.execute("UPDATE chatwoot_bindings SET last_error=? WHERE service_id=?", (error, event["service_id"]))

    def prepare_send(self, event, generation, reply):
        # Final local fence is transactional: a lifecycle/human webhook which
        # committed while the last API GET was in flight must still win.
        with self.store._connection(write=True) as db:
            state = db.execute("SELECT c.paused,c.generation,c.resume_after,b.enabled FROM chatwoot_conversations c JOIN chatwoot_bindings b USING(service_id) WHERE c.service_id=? AND c.conversation_id=?", (event["service_id"], event["conversation_id"])).fetchone()
            if not state or state[0] or state[1] != generation or event["message_id"] <= state[2] or not state[3]:
                return False
            db.execute("UPDATE chatwoot_events SET status='sending',reply=?,lease_until=? WHERE service_id=? AND conversation_id=? AND message_id=?", (self.vault.seal(db, reply), time.time() + 300, event["service_id"], event["conversation_id"], event["message_id"]))
        return True

    @staticmethod
    def bot_owns(conversation, binding):
        meta = conversation.get("meta") or {}
        assignee = meta.get("assignee") or {}
        return (conversation.get("status") == "pending" and meta.get("assignee_type") == "AgentBot"
                and assignee.get("id") == binding["bot_id"])

    async def handoff(self, client, event, reason):
        state = self.round_state(event["service_id"], event["conversation_id"])
        if event["message_id"] <= state["resume_after"]:
            return
        self.pause(event["service_id"], event["conversation_id"], reason, message_id=event["message_id"])
        conversation = await client.conversation(event["conversation_id"])
        meta = conversation.get("meta") or {}
        reopened_for_human = (state["awaiting_reopen"] and conversation.get("status") == "pending"
                              and meta.get("assignee_type") in {None, "User"})
        if (event["message_id"] > self.conversation_state(event["service_id"], event["conversation_id"])[1]
                and (self.bot_owns(conversation, client.binding) or reopened_for_human)):
            await client.call("POST", f"/conversations/{event['conversation_id']}/toggle_status", {"status": "open"}, bot=True)

    async def process(self, event):
        sid, cid, mid = event["service_id"], event["conversation_id"], event["message_id"]
        binding = self.binding(sid)
        if not binding or not binding["bot_id"]:
            self.finish(event, "ignored")
            return
        client = self.client(binding)
        try:
            self.require_unique_inbox(binding, binding["inbox_id"])
            conversation = await client.conversation(cid)
            rows = await client.messages(cid, after=mid - 1)
            marker = f"{sid}:{cid}:{mid}"
            sent = next((row for row in rows if row.get("content_attributes", {}).get("platform_ai_reply_id") == marker
                         and row.get("sender", {}).get("type") == "agent_bot" and row.get("sender", {}).get("id") == binding["bot_id"]), None)
            if sent:
                self.finish(event, "sent")
                return
            if event["status"] == "sending":
                await self.handoff(client, event, "delivery_uncertain")
                self.finish(event, "uncertain", "chatwoot_delivery_uncertain")
                return
            if len(rows) >= 100:
                # Chatwoot `after` returns at most 100 rows. A reply marker may
                # lie outside this page after a restore or a long outage. Never
                # guess that it was not delivered; safely defer to an operator.
                await self.handoff(client, event, "delivery_uncertain")
                self.finish(event, "uncertain", "chatwoot_delivery_uncertain")
                return
            current = next((row for row in rows if row.get("id") == mid), None)
            if current is None or not is_incoming(current):
                self.finish(event, "ignored")
                return
            if mid <= self.conversation_state(sid, cid)[1]:
                self.finish(event, "ignored")
                return
            conversation = await self.recover_round(client, event, conversation)
            state = self.round_state(sid, cid)
            paused, resume_after = bool(state["paused"]), state["resume_after"]
            if mid <= resume_after:
                self.finish(event, "ignored")
                return
            if not binding["enabled"] or paused or not self.bot_owns(conversation, binding):
                # A resolved round must stay armed until the next NEW message;
                # a delayed old event must not clear its recovery boundary.
                if not state["awaiting_reopen"] or conversation.get("status") != "resolved" or not binding["enabled"]:
                    await self.handoff(client, event, "human_takeover")
                self.finish(event, "handoff")
                return
            if time.time() - event["created_at"] > 86400:
                await self.handoff(client, event, "event_expired")
                self.finish(event, "handoff")
                return
            if wants_human(current.get("content") or ""):
                await self.handoff(client, event, "visitor_requested_human")
                self.finish(event, "handoff")
                return
            # Read a bounded, API-verified history from exactly this conversation.
            before = await client.messages(cid, before=mid, filter_internal_messages="true")
            if any(is_human(row) and row["id"] > resume_after for row in before + rows):
                await self.handoff(client, event, "human_takeover")
                self.finish(event, "handoff")
                return
            history, remaining = [], MAX_CONTEXT_CHARS
            protected = [value for key, value in self.resolver.env.items() if ("TOKEN" in key or "SECRET" in key or "PASSWORD" in key or "KEY" in key) and value]
            protected.extend(client.credentials.values())
            protected.extend(item['data_url'] for item in (current.get('attachments') or [])
                             if isinstance(item, dict) and isinstance(item.get('data_url'), str) and item['data_url'])
            config = self.resolver.resolve(sid)
            protected.append(self.resolver.credential(config))
            for row in reversed(before[-MAX_HISTORY:]):
                if row.get("id", 0) <= resume_after:
                    continue
                sender = row.get("sender", {})
                allowed_bot = row.get("message_type") in {1, "outgoing"} and sender.get("type") == "agent_bot" and sender.get("id") == binding["bot_id"]
                if not (is_incoming(row) or allowed_bot) or row.get("private") is not False:
                    continue
                text = (row.get("content") or "")[:min(4000, remaining)]
                if row.get("attachments") and not allowed_bot:
                    text += "\n[该历史消息含附件；本轮未重新读取历史图片，不得推断其内容。]"
                    text = text[:min(4000, remaining)]
                for secret in protected:
                    text = text.replace(secret, "[已隐藏凭据]")
                history.insert(0, {"role": "assistant" if allowed_bot else "user", "content": text})
                remaining -= len(text)
                if remaining <= 0:
                    break
            text = (current.get("content") or "")[:32000]
            for secret in protected:
                text = text.replace(secret, "[已隐藏凭据]")
            if current.get("attachments"):
                from .images import ChatwootImages, IMAGE_FEEDBACK
                # Attachment and provider failures produce ONE honest feedback
                # reply via the same dedup/send/human fences, not a text-only
                # fallback which could pretend to have seen an unread image.
                try:
                    if not config.supports_vision:
                        raise RuntimeFailure("vision_not_supported", 422)
                    if current.get("sender", {}).get("id") != (conversation.get("meta", {}).get("sender") or {}).get("id"):
                        raise RuntimeFailure("image_invalid_attachment", 422)
                    images = await ChatwootImages(client).read(current, cid)
                    result = await self.engine.respond(text, sid, history=history, redact_values=protected, images=images)
                    reply = result["reply"][:32000]
                except RuntimeFailure as error:
                    code = error.code if error.code in IMAGE_FEEDBACK else "image_provider_failed"
                    reply = IMAGE_FEEDBACK[code]
                    # A stable code only; never log URLs, image bytes or exception text.
                    import logging
                    logging.getLogger("ai_runtime").warning("Image request unavailable code=%s", code)
            else:
                result = await self.engine.respond(text, sid, history=history, redact_values=protected)
                reply = result["reply"][:32000]
            for secret in protected:
                reply = reply.replace(secret, "[已隐藏凭据]")
            newest = await client.messages(cid)
            current_state = self.round_state(sid, cid)
            if (current_state["generation"] != state["generation"] or current_state["paused"] or not self.binding(sid)["enabled"]
                    or any(is_human(row) and row["id"] > resume_after for row in newest)
                    or not self.bot_owns(await client.conversation(cid), binding)):
                if current_state["generation"] == state["generation"]:
                    await self.handoff(client, event, "human_takeover")
                self.finish(event, "handoff")
                return
            # Persist 'sending' before any non-idempotent external POST.
            if not self.prepare_send(event, state["generation"], reply):
                self.finish(event, "handoff")
                return
            try:
                await client.call("POST", f"/conversations/{cid}/messages", {"content": reply, "message_type": "outgoing", "private": False,
                    "content_type": "text", "content_attributes": {"platform_ai_reply_id": marker, "in_reply_to": mid}}, bot=True)
            except RuntimeFailure as error:
                if error.code == "chatwoot_rate_limited" and event["attempts"] < MAX_ATTEMPTS:
                    self.finish(event, "waiting", error.code)
                    return
                # A timeout/5xx can mean the message was saved. Reconcile once;
                # an uncertain POST must never be sent a second time.
                check = await client.messages(cid, after=mid - 1)
                if any(row.get("content_attributes", {}).get("platform_ai_reply_id") == marker for row in check):
                    self.finish(event, "sent")
                    return
                await self.handoff(client, event, "delivery_uncertain")
                self.finish(event, "uncertain", "chatwoot_delivery_uncertain")
                return
            self.finish(event, "sent")
        except RuntimeFailure as error:
            # Keep an interrupted send in reconciliation-only mode, including
            # when its reconciliation GET or handoff fails.
            with self.store._connection() as db:
                row = db.execute("SELECT status FROM chatwoot_events WHERE service_id=? AND conversation_id=? AND message_id=?", (sid, cid, mid)).fetchone()
            sending = row and row[0] == "sending"
            if sending:
                self.finish(event, "sending", "chatwoot_delivery_uncertain")
                if event["attempts"] >= MAX_ATTEMPTS:
                    self.pause(sid, cid, "delivery_uncertain", message_id=mid)
                    self.finish(event, "uncertain", "chatwoot_delivery_uncertain")
            elif event["attempts"] < MAX_ATTEMPTS:
                self.finish(event, "waiting", error.code)
            else:
                self.pause(sid, cid, "ai_reply_failed", message_id=mid)
                try:
                    await self.handoff(client, event, "ai_reply_failed")
                except RuntimeFailure:
                    pass
                self.finish(event, "failed", error.code)

    async def tick(self):
        event = self.claim()
        if event:
            try:
                if event["attempts"] > MAX_ATTEMPTS and event["status"] != "sending":
                    self.pause(event["service_id"], event["conversation_id"], "ai_reply_failed", message_id=event["message_id"])
                    self.finish(event, "failed", "chatwoot_invalid_response")
                    return True
                async with asyncio.timeout(120):
                    await self.process(event)
            except Exception:
                with self.store._connection() as db:
                    status = db.execute("SELECT status FROM chatwoot_events WHERE service_id=? AND conversation_id=? AND message_id=?", (event["service_id"], event["conversation_id"], event["message_id"])).fetchone()[0]
                if status == "sending":
                    self.pause(event["service_id"], event["conversation_id"], "delivery_uncertain", message_id=event["message_id"])
                    self.finish(event, "uncertain", "chatwoot_delivery_uncertain")
                elif event["attempts"] >= MAX_ATTEMPTS:
                    self.pause(event["service_id"], event["conversation_id"], "ai_reply_failed", message_id=event["message_id"])
                    self.finish(event, "failed", "chatwoot_invalid_response")
                else:
                    self.finish(event, "waiting", "chatwoot_invalid_response")
        return event is not None

    async def run(self):
        while not self.stopping.is_set():
            try:
                if await self.tick():
                    continue
            except Exception:
                # Stable code only; neither exception text nor event body.
                import logging
                logging.getLogger("ai_runtime").warning("Chatwoot worker unavailable")
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except TimeoutError:
                pass
