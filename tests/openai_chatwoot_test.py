"""Protocol-level Chatwoot 4.18 fixtures and mocked OpenAI, never paid requests."""

import asyncio
import copy
from contextlib import closing
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import admin_test
from admin_forms_test import Editor
from ai_runtime.app import create_app
from ai_runtime.config import ServiceResolver, RuntimeFailure
from ai_runtime.engine import ChatProvider, RuntimeEngine
from ai_runtime.management_store import ManagementStore, MIGRATIONS, SCHEMA_VERSION
from ai_runtime.vault import CredentialVault
from platform_admin.app import create_app as admin_app
from platform_admin.clients import InternalClient


class ChatwootProtocol:
    """Shapes/auth are from the tagged controllers, presenters and Jbuilders.

    This is NOT a live Ruby Chatwoot server and is not an E2E acceptance claim.
    """
    token = "fixture-chatwoot-admin-token-not-real-12345678"
    account = 42

    def __init__(self):
        self.bots, self.links, self.conversations, self.messages = {}, {}, {}, {}
        self.sent, self.calls, self.provider_calls = [], [], []
        self.fail_send = ""
        self.fail_reads = False
        self.provider_failure = False
        self.models_failure = False
        self.inbox_names = {7: "网站收件箱甲", 8: "网站收件箱乙"}

    def visitor(self, conversation_id=100, inbox_id=7, content="你好", sender_id=None):
        if conversation_id not in self.conversations:
            bot = self.links[inbox_id]
            self.conversations[conversation_id] = {"id": conversation_id, "inbox_id": inbox_id, "status": "pending",
                "meta": {"assignee_type": "AgentBot", "assignee": {"id": bot, "type": "agent_bot"},
                         "sender": {"id": sender_id or conversation_id + 1000, "type": "contact"}}}
            self.messages[conversation_id] = []
        row = {"id": 1 + max((r["id"] for rows in self.messages.values() for r in rows), default=0),
               "content": content, "message_type": 0, "private": False, "content_type": "text", "content_attributes": {},
               "sender": {"id": sender_id or conversation_id + 1000, "type": "contact"}, "conversation_id": conversation_id}
        self.messages[conversation_id].append(row)
        return row

    def webhook(self, row):
        conversation = copy.deepcopy(self.conversations[row["conversation_id"]])
        # Actual message webhook uses string message_type; list API uses ints.
        sender = dict(row["sender"])
        if sender.get("type") == "contact":
            del sender["type"]  # Contact.webhook_data differs from push_event_data.
        return {**row, "sender": sender, "message_type": {0: "incoming", 1: "outgoing"}[row["message_type"]],
                "account": {"id": self.account}, "conversation": conversation, "event": "message_created"}

    def human(self, conversation_id=100, content="人工回复"):
        row = self.visitor(conversation_id, content=content)
        row.update(message_type=1, sender={"id": 99, "type": "user"})
        return row

    def __call__(self, request):
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        if path.startswith("/public/api/v1/"):
            cid = int(path.split("/")[-2])
            return httpx.Response(200, json=[row for row in self.messages[cid] if not row["private"]])
        if not path.startswith("/api/v1/accounts/42/"):
            return httpx.Response(404)
        suffix = path[len("/api/v1/accounts/42"):]
        token = request.headers.get("api_access_token")
        bot = next((row for row in self.bots.values() if row["access_token"]["token"] == token), None)
        if token != self.token and not bot:
            return httpx.Response(401)
        if bot and method == "GET":
            return httpx.Response(401, json={"error": "bots cannot list messages/inboxes"})
        body = json.loads(request.content) if request.content else {}
        if suffix == "/inboxes":
            return httpx.Response(200, json={"payload": [
                {"id": key, "name": name, "channel_type": "Channel::WebWidget"} for key, name in self.inbox_names.items()
            ] + [{"id": 9, "name": "其他渠道", "channel_type": "Channel::Api"}]})
        if suffix.startswith("/inboxes/"):
            key = int(suffix.split("/")[2])
            if method == "GET":
                if len(suffix.split("/")) == 3:
                    if key not in self.inbox_names:
                        return httpx.Response(404)
                    return httpx.Response(200, json={"id": key, "name": self.inbox_names[key],
                        "channel_type": "Channel::WebWidget", "website_token": "public-website-token-" + str(key)})
                return httpx.Response(200, json={"agent_bot": self.bots.get(self.links.get(key), {})})
            self.links[key] = body["agent_bot"]
            return httpx.Response(200)
        if suffix.startswith("/agent_bots"):
            if method == "POST":
                key = 501 + len(self.bots)
                self.bots[key] = {"id": key, "secret": "fixture-signing-secret-" + str(key) + "a" * 32,
                    "access_token": {"token": "fixture-bot-token-not-real-" + str(key) + "a" * 20}, **body}
            else:
                key = int(suffix.split("/")[2])
                if method == "PATCH":
                    self.bots[key].update(body)
            return httpx.Response(200, json=self.bots[key])
        if suffix.startswith("/conversations/"):
            if self.fail_reads and method == "GET":
                return httpx.Response(503)
            parts = suffix.split("/")
            cid = int(parts[2])
            if cid not in self.conversations:
                return httpx.Response(404)
            conversation = self.conversations[cid]
            if len(parts) == 3:
                return httpx.Response(200, json=conversation)
            if parts[3] == "toggle_status":
                conversation["status"] = body["status"]
                if body["status"] == "open" and bot:
                    conversation["meta"].update(assignee=None, assignee_type=None)
                return httpx.Response(200, json={"payload": {"status": body["status"]}})
            if parts[3] == "assignments":
                conversation["meta"].update(assignee={"id": body["assignee_id"], "type": "agent_bot"}, assignee_type=body["assignee_type"])
                return httpx.Response(200, json={"id": body["assignee_id"], "type": "agent_bot"})
            if parts[3] == "messages" and method == "GET":
                rows = self.messages[cid]
                if "after" in request.url.params:
                    rows = [row for row in rows if row["id"] > int(request.url.params["after"])][:100]
                elif "before" in request.url.params:
                    rows = [row for row in rows if row["id"] < int(request.url.params["before"])][-20:]
                else:
                    rows = rows[-20:]
                if "filter_internal_messages" in request.url.params:
                    rows = [row for row in rows if not row["private"] and row["message_type"] != 2]
                return httpx.Response(200, json={"meta": {}, "payload": rows})
            if parts[3] == "messages" and method == "POST":
                if not bot:
                    raise AssertionError("AI replies must use Agent Bot token, not the human account token")
                if self.fail_send == "429":
                    return httpx.Response(429)
                if self.fail_send == "timeout-before-save":
                    raise httpx.ReadTimeout("fixture", request=request)
                row = {"id": 1 + max(r["id"] for rows in self.messages.values() for r in rows), **body,
                    "sender": {"id": bot["id"], "type": "agent_bot"}, "conversation_id": cid, "message_type": 1}
                self.messages[cid].append(row)
                self.sent.append(row)
                if self.fail_send == "timeout-after-save":
                    raise httpx.ReadTimeout("fixture", request=request)
                return httpx.Response(200, json=row)
        return httpx.Response(404)

    def provider(self, request):
        if request.url.path.endswith("/models"):
            if self.models_failure:
                return httpx.Response(404)
            return httpx.Response(200, json={"data": [{"id": "vendor/arbitrary-model-2077"}, {"id": "another-unlisted-model"}]})
        body = json.loads(request.content)
        self.provider_calls.append((request.headers["authorization"], body))
        if self.provider_failure:
            return httpx.Response(401, json={"error": {"message": request.headers["authorization"]}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "模拟回复：" + body["messages"][-1]["content"]}}]})


class IntegrationFixtures(unittest.TestCase):
    key_a = "sk-fixture-alpha-not-real-1234567890123456789"
    key_b = "sk-fixture-beta-not-real-12345678901234567890"

    def setUp(self):
        self.fixture = admin_test.AdminFixtures()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.protocol = ChatwootProtocol()
        self.resolver = self.fixture.resolver
        self.resolver.env["INTERNAL_API_TOKEN"] = "fixture-runtime-internal-not-real-123456789"
        self.engine = RuntimeEngine(self.resolver, ChatProvider(httpx.MockTransport(self.protocol.provider)))
        self.app = create_app(self.engine, chatwoot_transport=httpx.MockTransport(self.protocol))
        self.bridge = self.app.state.chatwoot
        self.api = TestClient(self.app, headers={"authorization": "Bearer " + self.fixture.env["SERVICE_MANAGEMENT_TOKEN"]})
        self.addCleanup(self.api.close)
        management = InternalClient("http://runtime.test", self.fixture.env["SERVICE_MANAGEMENT_TOKEN"], httpx.ASGITransport(app=self.app))
        self.admin = admin_app(self.fixture.env, management, self.fixture.maintenance)
        self.client = TestClient(self.admin, base_url="https://platform.example.test", follow_redirects=False)
        self.addCleanup(self.client.close)
        self.fixture.client = self.client
        self.assertEqual(self.fixture.login().status_code, 303)

    def model(self, key="model_a", secret=None, model="vendor/arbitrary-model-2077"):
        result = self.api.post("/management/models/save?create=true", json={"id": key, "profile": {"model": model}, "api_key": secret or self.key_a})
        self.assertEqual(result.status_code, 200, result.text)

    def service(self, key="service_a", model="model_a"):
        result = self.api.post("/management/services", json={"id": key, "name": "通用客服", "enabled": True, "ai_config": model})
        self.assertEqual(result.status_code, 201, result.text)

    def connect(self, key="service_a", inbox=7):
        result = self.api.put("/management/chatwoot/" + key + "/connection", json={"base_url": "https://chatwoot.example.test", "account_id": self.protocol.account, "api_token": self.protocol.token})
        self.assertEqual(result.status_code, 200, result.text)
        result = self.api.post("/management/chatwoot/" + key + "/bind", json={"inbox_id": inbox, "public_url": "https://platform.example.test"})
        self.assertEqual(result.status_code, 200, result.text)

    def prepared(self):
        self.model()
        self.service()
        self.connect()

    def hook(self, row, service="service_a", *, signature=None, timestamp=None, changes=None):
        event = {**self.protocol.webhook(row), **(changes or {})}
        raw = json.dumps(event, ensure_ascii=False).encode()
        stamp = timestamp or str(int(time.time()))
        bot = self.protocol.bots[self.bridge.binding(service)["bot_id"]]
        signed = "sha256=" + hmac.new(bot["secret"].encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return self.api.post("/integrations/chatwoot/" + service + "/webhook", content=raw,
            headers={"x-chatwoot-timestamp": stamp, "x-chatwoot-signature": signature or signed})

    def tick(self):
        return asyncio.run(self.bridge.tick())

    def due(self):
        with self.bridge.store._connection(write=True) as db:
            db.execute("UPDATE chatwoot_events SET next_at=0,lease_until=0")

    def status(self):
        with self.bridge.store._connection() as db:
            return db.execute("SELECT status,attempts FROM chatwoot_events ORDER BY message_id").fetchall()

    def test_keys_prefix_replacement_restart_encryption_and_live_selected_model(self):
        self.model()
        meta = self.api.get("/management/models/model_a/credential").json()
        self.assertEqual(meta["masked"], self.key_a[:8] + "********")
        self.assertNotIn(self.key_a, self.api.get("/management/profiles/ai").text)
        raw = self.bridge.store.path.read_bytes()
        self.assertNotIn(self.key_a.encode(), raw)
        self.assertEqual(self.api.post("/management/models/save", json={"id": "model_a", "profile": {"model": "manual-id-not-in-list"}, "api_key": self.key_b}).status_code, 200)
        resolver = ServiceResolver(self.resolver.config_dir, self.resolver.data_dir, self.resolver.env)
        self.assertEqual(CredentialVault(resolver).get("model", "model_a"), self.key_b)
        result = self.api.post("/management/models/test", json={"profile_id": "model_a", "model": "manual-id-not-in-list"})
        self.assertEqual(result.status_code, 200)
        auth, request = self.protocol.provider_calls[-1]
        self.assertEqual(auth, "Bearer " + self.key_b)
        self.assertEqual(request["model"], "manual-id-not-in-list")
        self.protocol.provider_failure = True
        with self.assertLogs("ai_runtime", level="WARNING") as logged:
            result = self.api.post("/management/models/test", json={"profile_id": "model_a", "model": "arbitrary"})
        self.assertEqual(result.status_code, 502)
        self.assertNotIn(self.key_b, result.text + "".join(logged.output))

    def test_dynamic_models_no_whitelist_manual_fallback_and_env_compatibility(self):
        self.model()
        result = self.api.post("/management/models/discover", json={"profile_id": "model_a"})
        self.assertEqual(result.json()["models"], ["another-unlisted-model", "vendor/arbitrary-model-2077"])
        self.protocol.models_failure = True
        self.assertEqual(self.api.post("/management/models/discover", json={"profile_id": "model_a"}).status_code, 422)
        self.assertEqual(self.api.post("/management/models/test", json={"profile_id": "model_a", "model": "still-manually-supported"}).status_code, 200)
        self.resolver.env["EXISTING_PROVIDER_KEY"] = self.key_b
        self.assertEqual(self.api.post("/management/profiles/ai", json={"id": "env_model", "model": "existing-model", "api_key_env": "EXISTING_PROVIDER_KEY"}).status_code, 201)
        status = self.api.get("/management/models/env_model/credential").json()
        self.assertEqual(status["source"], "environment")
        self.assertEqual(status["masked"], self.key_b[:8] + "********")

    def test_admin_model_form_save_mask_reopen_replace_and_no_secret_echo(self):
        form = Editor(self.client.get("/admin/edit/ai/_new").text).data
        form.update(id="model_a", model="arbitrary-admin-selected", api_key=self.key_a)
        result = self.client.post("/admin/edit/ai/_new", data=form)
        self.assertEqual(result.status_code, 303, result.text)
        page = self.client.get("/admin/edit/ai/model_a")
        self.assertIn(self.key_a[:8] + "********", page.text)
        self.assertNotIn(self.key_a, page.text)
        form = Editor(page.text).data
        form.update(model="model-edited-without-key")
        self.assertEqual(self.client.post("/admin/edit/ai/model_a", data=form).status_code, 303)
        self.assertEqual(CredentialVault(self.resolver).get("model", "model_a"), self.key_a)
        form.update(api_key=self.key_b)
        self.assertEqual(self.client.post("/admin/edit/ai/model_a", data=form).status_code, 303)
        self.assertEqual(CredentialVault(self.resolver).get("model", "model_a"), self.key_b)
        form.update(model="", api_key=self.key_a)
        invalid = self.client.post("/admin/edit/ai/model_a", data=form)
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(self.key_a, invalid.text)

    def test_visitor_reply_widget_visibility_multi_turn_and_dedup(self):
        self.prepared()
        first = self.protocol.visitor(content="第一问")
        self.assertEqual(self.hook(first).status_code, 200)
        self.assertTrue(self.tick())
        self.assertEqual(len(self.protocol.sent), 1)
        self.assertEqual(self.protocol.sent[0]["content"], "模拟回复：第一问")
        async def widget():
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.protocol)) as client:
                return await client.get("https://chatwoot.example.test/public/api/v1/inboxes/widget/contacts/visitor/conversations/100/messages")
        self.assertIn("模拟回复：第一问", asyncio.run(widget()).text)
        self.assertEqual(self.hook(first).status_code, 200)
        self.assertFalse(self.tick())
        self.assertEqual(self.hook(self.protocol.sent[0]).json()["status"], "ignored")
        second = self.protocol.visitor(content="接着上一个问题")
        self.hook(second)
        self.tick()
        messages = self.protocol.provider_calls[-1][1]["messages"]
        self.assertEqual([(r["role"], r["content"]) for r in messages], [
            ("user", "第一问"), ("assistant", "模拟回复：第一问"), ("user", "接着上一个问题")])
        self.assertEqual(len(self.protocol.sent), 2)

    def test_human_request_pause_resume_only_new_messages_and_human_reply(self):
        self.prepared()
        row = self.protocol.visitor(content="请转人工客服")
        self.hook(row)
        self.tick()
        self.assertEqual(self.protocol.conversations[100]["status"], "open")
        self.assertEqual(len(self.protocol.sent), 0)
        self.assertTrue(self.bridge.conversation_state("service_a", 100)[0])
        human = self.protocol.human()
        self.assertEqual(self.hook(human).json()["status"], "human_takeover")
        next_row = self.protocol.visitor(content="人工期间的追问")
        self.hook(next_row)
        self.tick()
        self.assertEqual(len(self.protocol.sent), 0)
        result = self.api.post("/management/chatwoot/service_a/conversations/100/resume", json={})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.protocol.conversations[100]["status"], "pending")
        self.assertEqual(self.hook(human).json()["status"], "ignored")
        self.assertFalse(self.bridge.conversation_state("service_a", 100)[0])
        self.hook(row)
        self.assertFalse(self.tick())
        self.hook(self.protocol.visitor(content="恢复之后的新问题"))
        self.tick()
        self.assertEqual(len(self.protocol.sent), 1)
        self.assertIn("恢复之后的新问题", self.protocol.sent[-1]["content"])
        self.assertEqual(self.hook(self.protocol.human()).json()["status"], "human_takeover")
        self.hook(self.protocol.visitor(content="等待人工回答"))
        self.tick()
        self.assertEqual(len(self.protocol.sent), 1)

    def test_separate_services_visitors_models_credentials_and_disabled_ai(self):
        self.prepared()
        self.model("model_b", self.key_b, "other-provider-model")
        self.service("service_b", "model_b")
        self.connect("service_b", 8)
        a = self.protocol.visitor(100, 7, "甲访客独立内容")
        b = self.protocol.visitor(200, 8, "乙访客独立内容")
        self.hook(a)
        self.hook(b, "service_b")
        self.tick()
        self.tick()
        calls = self.protocol.provider_calls
        self.assertEqual(calls[0][0], "Bearer " + self.key_a)
        self.assertEqual(calls[1][0], "Bearer " + self.key_b)
        self.assertNotIn("甲访客", str(calls[1][1]))
        self.assertEqual(calls[1][1]["model"], "other-provider-model")
        third = self.protocol.visitor(300, 7, "另一访客")
        self.hook(third)
        self.tick()
        self.assertNotIn("甲访客", str(self.protocol.provider_calls[-1][1]))
        self.assertEqual(self.api.put("/management/chatwoot/service_a/enabled", json={"enabled": False}).status_code, 200)
        self.hook(self.protocol.visitor(400, 7, "AI关闭"))
        self.tick()
        self.assertEqual(len(self.protocol.sent), 3)
        self.assertEqual(self.protocol.conversations[400]["status"], "open")

    def test_webhook_hmac_timestamp_replay_unauthorized_and_binding_validation(self):
        self.prepared()
        row = self.protocol.visitor()
        self.assertEqual(self.hook(row, signature="sha256=" + "0" * 64).status_code, 401)
        self.assertEqual(self.hook(row, timestamp=str(int(time.time()) - 600)).status_code, 401)
        self.assertEqual(self.hook(row, changes={"account": {"id": 999}}).status_code, 422)
        private = {**row, "private": True}
        self.assertEqual(self.hook(private).json()["status"], "ignored")
        self.assertEqual(self.api.post("/integrations/chatwoot/service_a/webhook", json={}).status_code, 401)
        with TestClient(self.app) as anonymous:
            self.assertEqual(anonymous.get("/management/chatwoot/service_a").status_code, 401)
            self.assertEqual(anonymous.post("/management/models/discover", json={}).status_code, 401)
        self.assertEqual(self.api.post("/management/chatwoot/service_a/bind", json={"inbox_id": 9, "public_url": "https://platform.example.test"}).status_code, 409)
        self.model("model_b", self.key_b)
        self.service("service_b", "model_b")
        self.assertEqual(self.api.put("/management/chatwoot/service_b/connection", json={"base_url": "https://chatwoot.example.test", "account_id": 42, "api_token": self.protocol.token}).status_code, 200)
        self.assertEqual(self.api.post("/management/chatwoot/service_b/bind", json={"inbox_id": 7, "public_url": "https://platform.example.test"}).status_code, 409)
        self.assertEqual(self.api.post("/management/chatwoot/service_a/conversations/999/resume", json={}).status_code, 502)

    def test_bounded_retry_provider_failure_handoff_and_ambiguous_send_no_duplicates(self):
        self.prepared()
        self.protocol.provider_failure = True
        self.hook(self.protocol.visitor())
        for _ in range(3):
            self.due()
            self.tick()
        self.assertEqual(self.status(), [("failed", 3)])
        self.assertFalse(self.tick())
        self.assertEqual(self.protocol.conversations[100]["status"], "open")
        self.protocol.provider_failure = False
        self.protocol.fail_send = "timeout-after-save"
        self.hook(self.protocol.visitor(200, content="发送响应丢失"))
        self.tick()
        self.assertEqual(len(self.protocol.sent), 1)
        self.assertEqual(self.status()[-1][0], "sent")
        self.protocol.fail_send = "timeout-before-save"
        self.hook(self.protocol.visitor(300, content="发送结果不确定"))
        self.tick()
        self.due()
        self.assertFalse(self.tick())
        self.assertEqual(self.status()[-1][0], "uncertain")
        self.assertEqual(len(self.protocol.sent), 1)

    def test_crash_sending_reconciles_without_post_and_human_during_generation_wins(self):
        self.prepared()
        row = self.protocol.visitor()
        self.hook(row)
        event = self.bridge.claim()
        self.bridge.finish(event, "sending", reply="尚未确认的回复")
        self.due()
        self.tick()
        self.assertEqual(len(self.protocol.sent), 0)
        self.assertEqual(self.status()[0][0], "uncertain")
        self.hook(self.protocol.visitor(200, content="生成时人工接管"))
        original = self.protocol.provider
        def human_while_generating(request):
            result = original(request)
            self.bridge.pause("service_a", 200)
            return result
        self.engine.provider = ChatProvider(httpx.MockTransport(human_while_generating))
        self.tick()
        self.assertEqual(len(self.protocol.sent), 0)
        self.assertTrue(self.bridge.conversation_state("service_a", 200)[0])

    def test_admin_chatwoot_forms_auth_csrf_origin_and_redacted_tokens(self):
        self.model()
        self.service()
        page = self.client.get("/admin/chatwoot?service_id=service_a")
        payload = {"csrf": self.fixture.csrf(), "service_id": "service_a", "action": "connect", "base_url": "https://chatwoot.example.test", "account_id": "42", "api_token": self.protocol.token}
        self.assertEqual(self.client.post("/admin/chatwoot", data={**payload, "csrf": "bad"}).status_code, 403)
        self.assertEqual(self.client.post("/admin/chatwoot", data=payload, headers={"origin": "https://foreign.example.test"}).status_code, 403)
        result = self.client.post("/admin/chatwoot", data=payload)
        self.assertEqual(result.status_code, 303, result.text)
        page = self.client.get(result.headers["location"])
        self.assertIn(self.protocol.token[:8] + "********", page.text)
        self.assertNotIn(self.protocol.token, page.text)
        self.assertIn("网站收件箱甲", page.text)
        binding = {"csrf": self.fixture.csrf(), "service_id": "service_a", "action": "bind", "inbox_id": "7"}
        self.assertEqual(self.client.post("/admin/chatwoot", data=binding).status_code, 303)
        self.assertIn("已启用", self.client.get("/admin/chatwoot?service_id=service_a").text)

    def test_additive_v2_migration_retains_profiles_and_environment_credentials(self):
        path = ManagementStore(self.resolver).path
        self.assertFalse(path.exists())
        path.parent.mkdir(parents=True, mode=0o700)
        with closing(sqlite3.connect(path)) as db, db:
            for version in (1, 2):
                for sql in MIGRATIONS[version]:
                    db.execute(sql)
            db.execute("PRAGMA user_version=2")
            db.execute("INSERT INTO runtime_profiles VALUES ('ai','legacy',?)", (json.dumps({"model": "legacy-arbitrary-id", "api_base_url": "https://provider.example.test/v1", "api_key_env": "LEGACY_PROVIDER_KEY"}),))
        path.chmod(0o600)
        self.resolver.env["LEGACY_PROVIDER_KEY"] = self.key_b
        result = self.api.get("/management/models/legacy/credential")
        self.assertEqual(result.json()["source"], "environment")
        self.assertEqual(result.json()["masked"], self.key_b[:8] + "********")
        self.model()
        self.assertEqual(self.api.get("/management/profiles/ai/legacy").json()["model"], "legacy-arbitrary-id")
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_atomic_credential_update_missing_vault_key_and_restored_volume(self):
        self.prepared()
        body = {"id": "model_a", "profile": {"model": "changed-model"}, "api_key": self.key_b}
        with patch.object(CredentialVault, "put", side_effect=RuntimeFailure("credential_storage_unavailable")):
            self.assertEqual(self.api.post("/management/models/save", json=body).status_code, 503)
        self.assertEqual(CredentialVault(self.resolver).get("model", "model_a"), self.key_a)
        self.assertEqual(self.api.get("/management/profiles/ai/model_a").json()["model"], "vendor/arbitrary-model-2077")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "restored"
            shutil.copytree(self.resolver.data_dir, target)
            restored = ServiceResolver(self.resolver.config_dir, target, self.resolver.env)
            self.assertEqual(restored.credential(restored.resolve("service_a")), self.key_a)
            self.assertEqual(CredentialVault(restored).get("chatwoot", "service_a")["api_token"], self.protocol.token)
        vault = CredentialVault(self.resolver)
        vault.key_path.unlink()
        response = self.api.post("/management/models/save", json=body)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(vault.key_path.exists(), "Do not regenerate a key over encrypted existing data")

    def test_model_edits_apply_immediately_and_credentials_never_enter_public_profiles(self):
        self.prepared()
        for body in ({"model": self.key_a}, {"model": "manual", "api_base_url": "https://provider.example.test/" + self.key_a}):
            response = self.api.put("/management/profiles/ai/model_a", json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(self.key_a, response.text)
        self.assertEqual(self.api.post("/management/models/save", json={"id": "model_a", "profile": {"model": "updated-model-without-redeploy"}, "api_key": ""}).status_code, 200)
        self.hook(self.protocol.visitor())
        self.tick()
        self.assertEqual(self.protocol.provider_calls[-1][1]["model"], "updated-model-without-redeploy")
        self.assertEqual(self.protocol.provider_calls[-1][0], "Bearer " + self.key_a)
        self.assertNotIn(self.key_a, self.api.get("/management/profiles/ai").text)

    def test_history_limits_private_messages_and_control_secrets_redacted(self):
        self.prepared()
        for index in range(30):
            row = self.protocol.visitor(content=str(index) + "x" * 5000)
            if index % 4 == 0:
                row.update(private=True, content="private-message-must-not-enter-context")
        secrets = [self.key_a, self.protocol.token, self.resolver.env["INTERNAL_API_TOKEN"],
                   self.protocol.bots[self.protocol.links[7]]["secret"]]
        row = self.protocol.visitor(content="用户问题 " + " ".join(secrets))
        self.hook(row)
        self.tick()
        messages = self.protocol.provider_calls[-1][1]["messages"]
        self.assertLessEqual(len(messages), 21)
        self.assertLessEqual(sum(len(message["content"]) for message in messages[:-1]), 24000)
        self.assertNotIn("private-message-must-not-enter-context", str(messages))
        for secret in secrets:
            self.assertNotIn(secret, str(messages))
            self.assertNotIn(secret, self.protocol.sent[-1]["content"])

    def test_rate_limit_retries_and_recovery_from_full_history_are_bounded(self):
        self.prepared()
        self.protocol.fail_send = "429"
        self.hook(self.protocol.visitor())
        for _ in range(3):
            self.due()
            self.tick()
        self.assertFalse(self.tick())
        self.assertEqual(self.status()[0][1], 3)
        self.assertEqual(len(self.protocol.sent), 0)
        self.protocol.fail_send = ""
        old = self.protocol.visitor(200, content="恢复后的旧 Webhook")
        for _ in range(105):
            self.protocol.visitor(200, content="新消息")
        self.hook(old)
        self.tick()
        self.assertEqual(len(self.protocol.sent), 0)
        self.assertEqual(self.status()[-1][0], "uncertain")

    def test_auto_reply_preserves_fresh_prompt_rules_and_instance_knowledge_limits(self):
        self.model()
        for kind, record in (("prompts", {"id": "prompt", "system_prompt": "通用系统提示", "reply_rules": ["通用回复规则"]}),
                             ("knowledge", {"id": "knowledge", "source": "manual.txt", "max_characters": 4})):
            self.assertEqual(self.api.post("/management/profiles/" + kind, json=record).status_code, 201)
        self.assertEqual(self.api.post("/management/services", json={"id": "service_a", "name": "通用实例", "ai_config": "model_a", "prompt_config": "prompt", "knowledge_config": "knowledge"}).status_code, 201)
        path = "/management/services/service_a/knowledge"
        for text in ("甲乙丙丁其余不传", "新鲜正文立即生效"):
            revision = self.api.get(path).json()["revision"]
            self.assertEqual(self.api.put(path, json={"content": text, "revision": revision}).status_code, 200)
            if not self.protocol.links:
                self.assertEqual(self.api.post("/management/services/service_a/enable").status_code, 200)
                self.connect()
            self.hook(self.protocol.visitor(content="知识问题"))
            self.tick()
            messages = self.protocol.provider_calls[-1][1]["messages"]
            self.assertEqual(messages[0]["content"], "通用系统提示\n通用回复规则")
            self.assertEqual(messages[1]["content"], "<knowledge_context>\n" + text[:4] + "\n</knowledge_context>")

    def test_untrusted_provider_cannot_echo_keys_or_control_tokens_to_visitors(self):
        self.prepared()
        values = [self.key_a, self.protocol.token, self.resolver.env["INTERNAL_API_TOKEN"]]
        def provider(_request):
            return httpx.Response(200, json={"choices": [{"message": {"content": " ".join(values)}}]})
        self.engine.provider = ChatProvider(httpx.MockTransport(provider))
        self.hook(self.protocol.visitor())
        self.tick()
        for value in values:
            self.assertNotIn(value, self.protocol.sent[0]["content"])

    def test_failed_durable_webhook_intake_requests_actual_chatwoot_retry_status(self):
        self.prepared()
        row = self.protocol.visitor()
        with patch.object(self.bridge, "accept", side_effect=RuntimeFailure("management_storage_unavailable")):
            self.assertEqual(self.hook(row).status_code, 500)
        self.assertEqual(self.hook(row).status_code, 200)
        self.assertEqual(self.hook(row).status_code, 200)
        self.tick()
        self.assertEqual(len(self.protocol.sent), 1)

    def test_environment_alias_cannot_expose_or_use_internal_control_credentials(self):
        self.resolver.env["PROVIDER_KEY_ALIAS"] = self.resolver.env["SERVICE_MANAGEMENT_TOKEN"]
        self.assertEqual(self.api.post("/management/profiles/ai", json={"id": "alias", "model": "manual-id", "api_key_env": "PROVIDER_KEY_ALIAS"}).status_code, 201)
        self.assertEqual(self.api.get("/management/models/alias/credential").json()["masked"], "")
        self.service(model="alias")
        with self.assertRaises(RuntimeFailure):
            self.resolver.credential(self.resolver.resolve("service_a"))
        result = self.api.post("/management/models/test", json={"profile_id": "alias", "model": "manual-id", "api_key_env": "PROVIDER_KEY_ALIAS"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(len(self.protocol.provider_calls), 0)


if __name__ == "__main__":
    unittest.main()
