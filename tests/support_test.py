"""Real bearer/session/CSRF/storage with Chatwoot v4.18-shaped HTTP responses."""
import asyncio
from contextlib import closing
import json
import re
import sqlite3
from unittest.mock import patch
import unittest

from fastapi.testclient import TestClient
import httpx

import openai_chatwoot_test as integration
from ai_runtime.chatwoot import ChatwootBridge
from ai_runtime.support_api import SupportEntry
from platform_admin.app import create_app
from platform_admin.clients import InternalClient


class SupportFixtures(unittest.TestCase):
    def setUp(self):
        self.f = integration.IntegrationFixtures()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.prepared()
        self.requests = []
        original = self.f.protocol

        def protocol(request):
            self.requests.append(request)
            if re.fullmatch(r"/api/v1/accounts/42/inboxes/[78]", request.url.path):
                if request.headers.get("api_access_token") != original.token:
                    return httpx.Response(401)
                iid = int(request.url.path.rsplit("/", 1)[1])
                return httpx.Response(200, json={"id": iid, "channel_type": "Channel::WebWidget", "name": "通用网站",
                    "website_token": "public-website-token-" + str(iid), "hmac_token": "MUST-NOT-LEAK"})
            return original(request)

        self.f.bridge.transport = httpx.MockTransport(protocol)
        self.documents = []

        def widget(request):
            self.documents.append(request)
            self.assertEqual(request.url.path, "/widget")
            self.assertNotIn("api_access_token", request.headers)
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            self.assertEqual(request.headers["host"], getattr(self, "expected_host", "backup.example.test"))
            self.assertEqual(request.headers["x-forwarded-proto"], "https")
            return httpx.Response(200, text='<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<script>window.chatwootWebChannel={websiteToken:"' + request.url.params["website_token"] + '"};'
                'window.authToken="' + ('native-visitor.jwt' if request.url.params["website_token"].endswith('-7') else 'other-visitor.jwt') + '";</script><script type="module" src="/vite/assets/widget.js"></script>'
                '</head><body><div id="app" class="h-full"></div></body></html>')

        management = InternalClient("http://runtime.test", self.f.fixture.env["SERVICE_MANAGEMENT_TOKEN"], httpx.ASGITransport(app=self.f.app))
        self.app = create_app(self.f.fixture.env, management, self.f.fixture.maintenance, widget_transport=httpx.MockTransport(widget))
        self.browser = TestClient(self.app, base_url="https://backup.example.test", follow_redirects=False)
        self.addCleanup(self.browser.close)

    def login(self):
        csrf = re.search(r'name="csrf" value="([^"]+)"', self.browser.get("/admin/login").text)[1]
        self.assertEqual(self.browser.post("/admin/login", data={"csrf": csrf, "username": "admin", "password": self.f.fixture.password},
                                          headers={"origin": "https://backup.example.test"}).status_code, 303)

    def test_single_candidate_public_native_document_only_and_refresh_cookie(self):
        response = self.f.api.get("/management/support-entry/public")
        self.assertEqual(response.json(), {"website_token": "public-website-token-7"})
        page = self.browser.get("/support/")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("/support/session.js", page.text)
        self.assertNotIn("iframe", page.text)
        self.assertNotIn("chatwootSDK", page.text)
        for secret in (self.f.protocol.token, "MUST-NOT-LEAK", self.f.fixture.env["SERVICE_MANAGEMENT_TOKEN"]):
            self.assertNotIn(secret, page.text)
        self.browser.cookies.set("platform_support_public-website-token-7", "native-visitor.jwt", domain="backup.example.test", path="/support/")
        self.browser.get("/support/")
        self.assertEqual(self.documents[-1].url.params["cw_conversation"], "native-visitor.jwt")

    def test_widget_bootstrap_runs_before_chatwoot_module_initialization(self):
        # v4.18 endPoints.js builds message/history URLs from location.search,
        # NOT from window.chatwootWebChannel.websiteToken. A document 200 alone
        # therefore says nothing about whether this native Widget can send.
        document = self.browser.get("/support/").text
        self.assertIn('<script src="/support/session.js"></script>', document)
        script = (__import__('pathlib').Path(__file__).resolve().parents[1] /
                  'platform_admin/static/support.js').read_text(encoding='utf-8')
        self.assertIn('website_token', script)
        self.assertIn('history.replaceState', script)

    def test_multiple_instances_require_explicit_default_and_preserve_isolation(self):
        self.f.model("model_b")
        self.f.service("service_b", "model_b")
        self.f.connect("service_b", 8)
        self.assertEqual(self.f.api.get("/management/support-entry/public").status_code, 409)
        self.assertEqual(self.f.api.put("/management/support-entry", json={"service_id": "service_b"}).status_code, 200)
        self.assertEqual(self.f.api.get("/management/support-entry/public").json()["website_token"], "public-website-token-8")
        self.assertEqual(self.f.bridge.binding("service_a")["inbox_id"], 7)
        self.assertEqual(self.f.bridge.binding("service_b")["inbox_id"], 8)
        restart = SupportEntry(ChatwootBridge(self.f.engine, self.f.bridge.transport))
        self.assertEqual(restart.selected(), "service_b")
        self.assertEqual(self.f.api.put("/management/support-entry", json={"service_id": "../credentials"}).status_code, 422)

    def test_bearer_session_csrf_and_origin_are_not_bypassed(self):
        for path in ("/management/support-entry", "/management/support-entry/public"):
            self.assertEqual(self.f.api.get(path, headers={"authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.browser.get("/admin/support").status_code, 303)
        self.assertEqual(self.browser.post("/admin/websites", data={"action": "add", "domain": "attacker.example.test"}).status_code, 303)
        self.login()
        page = self.browser.get("/admin/support")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        body = {"service_id": "service_a", "csrf": csrf}
        self.assertEqual(self.browser.post("/admin/support", data=body, headers={"origin": "https://evil.example.test"}).status_code, 403)
        self.assertEqual(self.browser.post("/admin/support", data={"service_id": "service_a"}).status_code, 403)
        self.assertEqual(self.browser.post("/admin/support", data=body, headers={"origin": "https://backup.example.test"}).status_code, 303)

    def test_default_entry_survives_actual_sqlite_backup_and_restore(self):
        self.assertEqual(self.f.api.put("/management/support-entry", json={"service_id": "service_a"}).status_code, 200)
        backup = self.f.fixture.root / "backup.sqlite3"
        with closing(sqlite3.connect(self.f.bridge.store.path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        with self.f.bridge.store._connection(write=True) as db:
            db.execute("DELETE FROM runtime_profiles WHERE kind='support-entry'")
        with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(self.f.bridge.store.path)) as target:
            source.backup(target)
        self.assertEqual(SupportEntry(self.f.bridge).selected(), "service_a")

    def test_primary_migration_preserves_bot_credentials_binding_and_ai_reply(self):
        before = self.f.bridge.binding("service_a")
        credentials = dict(self.f.bridge.client(before).credentials)
        new = "https://backup.example.test"
        self.assertEqual(self.f.api.post("/management/website-origin", json={"old_url": "https://platform.example.test", "new_url": new}).status_code, 200)
        after = self.f.bridge.binding("service_a")
        self.assertEqual(after, before)
        self.assertEqual(self.f.bridge.client(after).credentials, credentials)
        self.assertEqual(self.f.protocol.bots[before["bot_id"]]["outgoing_url"], new + "/integrations/chatwoot/service_a/webhook")
        row = self.f.protocol.visitor()
        self.assertEqual(self.f.hook(row).status_code, 200)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)

    def test_local_api_remains_independent_from_failed_primary_hostname(self):
        self.f.resolver.env.update(CHATWOOT_LOCAL_ORIGIN="https://chatwoot.example.test", CHATWOOT_INTERNAL_URL="http://rails:3000")
        before = self.f.bridge.binding("service_a")
        self.assertEqual(self.f.bridge.client(before).binding["base_url"], "http://rails:3000")
        self.assertEqual(self.f.api.post("/management/website-origin", json={"old_url": before["base_url"], "new_url": "https://backup.example.test"}).status_code, 200)
        after = self.f.bridge.binding("service_a")
        self.assertEqual(after["base_url"], "https://backup.example.test")
        self.assertEqual(self.f.bridge.client(after).binding["base_url"], "http://rails:3000")
        self.assertEqual(self.f.api.get("/management/support-entry/public").status_code, 200)

    def test_primary_patch_timeout_after_remote_save_is_safely_reverted(self):
        binding = self.f.bridge.binding("service_a")
        old = self.f.protocol.bots[binding["bot_id"]]["outgoing_url"]
        original = self.f.bridge.transport
        failed = []
        def ambiguous(request):
            response = self.f.protocol(request)
            if request.method == "PATCH" and not failed:
                failed.append(True)
                raise httpx.ReadTimeout("fixture", request=request)
            return response
        self.f.bridge.transport = httpx.MockTransport(ambiguous)
        result = self.f.api.post("/management/website-origin", json={"old_url": "https://platform.example.test", "new_url": "https://backup.example.test"})
        self.assertEqual(result.status_code, 504)
        self.assertEqual(self.f.protocol.bots[binding["bot_id"]]["outgoing_url"], old)
        self.assertEqual(self.f.bridge.binding("service_a"), binding)

    def test_retiring_backup_migrates_existing_callbacks_and_connections_to_primary(self):
        primary, backup = "https://chatwoot.example.test", "https://backup.example.test"
        self.f.resolver.env.update(CHATWOOT_LOCAL_ORIGIN=primary, CHATWOOT_INTERNAL_URL="http://rails:3000")
        self.assertEqual(self.f.api.post("/management/website-alias", json={"old_url": primary, "new_url": backup}).status_code, 200)
        binding = self.f.bridge.binding("service_a")
        credentials = dict(self.f.bridge.client(binding).credentials)
        self.f.protocol.bots[binding["bot_id"]]["outgoing_url"] = backup + "/integrations/chatwoot/service_a/webhook"
        with self.f.bridge.store._connection(write=True) as db:
            db.execute("UPDATE chatwoot_bindings SET base_url=? WHERE service_id='service_a'", (backup,))
        self.assertEqual(self.f.api.post("/management/website-retire", json={"old_url": backup, "new_url": primary}).status_code, 200)
        after = self.f.bridge.binding("service_a")
        self.assertEqual(after["base_url"], primary)
        self.assertEqual(after["bot_id"], binding["bot_id"])
        self.assertEqual(self.f.bridge.client(after).credentials, credentials)
        self.assertEqual(self.f.protocol.bots[after["bot_id"]]["outgoing_url"], primary + "/integrations/chatwoot/service_a/webhook")
        self.assertEqual(self.f.bridge.client(after).binding["base_url"], "http://rails:3000")
        self.assertEqual(self.f.bridge.client({**after, "base_url": backup}).binding["base_url"], backup)


if __name__ == "__main__":
    unittest.main()
