"""Real management storage/auth/AI worker; Chatwoot/OpenAI HTTP doubles."""
import asyncio
from contextlib import closing
import copy
import json
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch

from ai_runtime.chatwoot import ChatwootBridge
from ai_runtime.config import RuntimeFailure
from ai_runtime.management_store import ManagementStore, SCHEMA_VERSION
from ai_runtime.support_api import SupportEntry
from ai_runtime.support_entries import SupportEntries
import support_test


class EntryFixtures(unittest.TestCase):
    def setUp(self):
        self.support = support_test.SupportFixtures()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.f = self.support.f
        self.api, self.browser = self.f.api, self.support.browser

    def pair(self):
        self.f.model("model_b")
        self.f.service("service_b", "model_b")
        self.f.connect("service_b", 8)

    @staticmethod
    def record(slug="platform-a", sid="service_a", iid=7, **fields):
        return {"slug": slug, "name": "通用客服平台", "service_id": sid, "account_id": 42,
                "inbox_id": iid, "enabled": True, **fields}

    def create(self, **kwargs):
        result = self.api.post("/management/support-entries", json=self.record(**kwargs))
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def test_two_entries_use_distinct_inboxes_tokens_and_exact_paths(self):
        self.pair()
        self.create()
        self.create(slug="platform-b", sid="service_b", iid=8)
        for slug, iid in (("platform-a", 7), ("platform-b", 8)):
            result = self.api.get("/management/support-entry/public/" + slug)
            self.assertEqual(result.json(), {"website_token": "public-website-token-" + str(iid)})
            page = self.browser.get("/support/" + slug + "/?website_token=ATTACKER&cw_conversation=ATTACKER")
            self.assertEqual(page.status_code, 200)
            self.assertIn("public-website-token-" + str(iid), page.text)
            self.assertNotIn("ATTACKER", page.text)
            self.assertEqual(self.support.documents[-1].url.params["website_token"], "public-website-token-" + str(iid))
        self.assertEqual(self.browser.get("/support/unknown/").status_code, 404)
        self.assertIn("不存在", self.browser.get("/support/unknown/").text)
        self.assertEqual(self.browser.get("/support/platform-a").status_code, 307)

    def test_default_compatibility_rename_disable_and_delete_do_not_touch_chatwoot(self):
        self.pair()
        self.api.put("/management/support-entry", json={"service_id": "service_a"})
        self.create()
        self.create(slug="platform-b", sid="service_b", iid=8)
        before = copy.deepcopy(self.f.protocol.bots), self.f.bridge.vault.get("chatwoot", "service_a"), self.f.bridge.store.read_services()
        self.assertEqual(self.api.get("/management/support-entry/public").json()["website_token"], "public-website-token-7")
        self.assertEqual(self.api.post("/management/support-entries/platform-b/default").status_code, 200)
        self.assertEqual(self.api.get("/management/support-entry/public").json()["website_token"], "public-website-token-8")
        self.assertEqual(self.api.delete("/management/support-entries/platform-b").status_code, 409)
        update = self.record("new-platform", "service_b", 8, name="新名称")
        self.assertEqual(self.api.put("/management/support-entries/platform-b", json=update).status_code, 200)
        self.assertEqual(self.api.get("/management/support-entry/public/platform-b").status_code, 404)
        self.assertEqual(self.api.get("/management/support-entry/public").json()["website_token"], "public-website-token-8")
        update["enabled"] = False
        self.assertEqual(self.api.put("/management/support-entries/new-platform", json=update).status_code, 409)
        self.api.post("/management/support-entries/platform-a/default")
        self.assertEqual(self.api.put("/management/support-entries/new-platform", json=update).status_code, 200)
        count = len(self.support.documents)
        self.assertEqual(self.browser.get("/support/new-platform/").status_code, 410)
        self.assertEqual(len(self.support.documents), count)
        self.assertEqual(self.api.delete("/management/support-entries/new-platform").status_code, 200)
        self.assertEqual(before, (self.f.protocol.bots, self.f.bridge.vault.get("chatwoot", "service_a"), self.f.bridge.store.read_services()))
        self.assertFalse(any(method == "DELETE" for method, _ in self.f.protocol.calls))

    def test_validation_duplicate_slug_and_inbox_are_rejected(self):
        self.pair()
        self.create()
        self.assertEqual(self.api.post("/management/support-entries", json=self.record()).status_code, 409)
        self.assertEqual(self.api.post("/management/support-entries", json=self.record("different")).status_code, 409)
        for slug in ("../secrets", "bad_name", "UPPER", "a--b", "-a", "a-", "a" * 65, "错误"):
            result = self.api.post("/management/support-entries", json=self.record(slug))
            self.assertEqual(result.status_code, 422, (slug, result.text))
        for field, value in (("inbox_id", 8), ("account_id", 43), ("service_id", "unknown"), ("enabled", "true"), ("name", " "), ("api_token", "must-not-save")):
            body = self.record("other", **{field: value})
            self.assertGreaterEqual(self.api.post("/management/support-entries", json=body).status_code, 400)
        self.assertEqual(len(self.api.get("/management/support-entries").json()), 1)

    def test_admin_forms_csrf_origin_no_secret_and_reopen(self):
        self.support.login()
        csrf = re.search(r'name="csrf" value="([^"]+)"', self.browser.get("/admin/support").text)[1]
        body = {"csrf": csrf, "action": "save", "slug": "platform-a", "name": "平台甲", "service_id": "service_a", "enabled": "true", "original_slug": ""}
        self.assertEqual(self.browser.post("/admin/support", data=body, headers={"origin": "https://evil.example.test"}).status_code, 403)
        self.assertEqual(self.browser.post("/admin/support", data={k: v for k, v in body.items() if k != "csrf"}).status_code, 403)
        self.assertEqual(self.browser.post("/admin/support", data=body).status_code, 303)
        page = self.browser.get("/admin/support?edit=platform-a")
        self.assertEqual(page.status_code, 200)
        self.assertIn('value="平台甲"', page.text)
        for secret in (self.f.protocol.token, self.f.key_a, self.f.fixture.env["SERVICE_MANAGEMENT_TOKEN"], "MUST-NOT-LEAK"):
            self.assertNotIn(secret, page.text)
        self.assertEqual(self.browser.get("/admin/assets/support-entries.js").status_code, 200)
        self.assertEqual(self.api.get("/management/support-entries", headers={"authorization": "Bearer invalid"}).status_code, 401)
        self.assertEqual(self.api.get("/management/support-entry/public/platform-a", headers={"authorization": "Bearer invalid"}).status_code, 401)

    def test_api_options_are_live_and_bindings_cannot_point_at_external_widget(self):
        options = self.api.get("/management/support-entries/options").json()
        self.assertEqual(options[0]["inbox_id"], 7)
        self.assertEqual(options[0]["inbox_name"], self.f.protocol.inbox_names[7])
        self.f.resolver.env["CHATWOOT_LOCAL_ORIGIN"] = "https://another.example.test"
        self.assertEqual(self.api.post("/management/support-entries", json=self.record()).status_code, 409)
        self.assertEqual(self.api.get("/management/support-entries/options").json(), [])

    def test_aliases_cannot_bind_one_account_inbox_to_two_instances(self):
        self.f.model("model_b")
        self.f.service("service_b", "model_b")
        self.f.resolver.env["CHATWOOT_LOCAL_ORIGIN"] = "https://chatwoot.example.test"
        self.api.post("/management/website-alias", json={"old_url": "https://chatwoot.example.test", "new_url": "https://backup.example.test"})
        self.assertEqual(self.api.put("/management/chatwoot/service_b/connection", json={"base_url": "https://backup.example.test", "account_id": 42, "api_token": self.f.protocol.token}).status_code, 200)
        result = self.api.post("/management/chatwoot/service_b/bind", json={"inbox_id": 7, "public_url": "https://backup.example.test"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"], "chatwoot_inbox_in_use")

    def test_canonical_authority_and_preexisting_ambiguous_queue_fail_closed(self):
        self.pair()
        self.f.resolver.env["CHATWOOT_LOCAL_ORIGIN"] = "https://chatwoot.example.test"
        self.api.post("/management/website-alias", json={"old_url": "https://chatwoot.example.test", "new_url": "https://backup.example.test"})
        row = self.f.protocol.visitor()
        self.assertEqual(self.f.hook(row).status_code, 200)
        # Old releases allowed this alias/default-port duplicate. Keep existing
        # rows/data, but reject BOTH newly arriving and already queued replies.
        with self.f.bridge.store._connection(write=True) as db:
            db.execute("UPDATE chatwoot_bindings SET base_url='https://BACKUP.example.test:443',inbox_id=7 WHERE service_id='service_b'")
        result = self.f.hook(row)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"], "chatwoot_inbox_in_use")
        self.f.tick()
        self.assertEqual(self.f.protocol.provider_calls, [])
        self.assertEqual(self.f.protocol.sent, [])
        self.assertEqual(self.api.get("/management/support-entry/public").status_code, 409)

    def test_offline_connection_can_still_disable_nondefault_entry(self):
        self.create()
        with patch.object(self.f.bridge, "inboxes", side_effect=RuntimeFailure("chatwoot_request_failed", 502)):
            result = self.api.put("/management/support-entries/platform-a", json=self.record(enabled=False))
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["enabled"])
        self.assertEqual(self.browser.get("/support/platform-a/").status_code, 410)

    def test_shared_profile_different_instance_knowledge_prompts_and_signed_routing(self):
        self.pair()
        for kind, body in (("knowledge", {"id": "shared", "source": "content.txt"}),
                           ("prompts", {"id": "prompt_a", "system_prompt": "只接待平台甲"}),
                           ("prompts", {"id": "prompt_b", "system_prompt": "只接待平台乙"})):
            self.assertEqual(self.api.post("/management/profiles/" + kind, json=body).status_code, 201)
        for sid, suffix in (("service_a", "甲"), ("service_b", "乙")):
            self.assertEqual(self.api.patch("/management/services/" + sid, json={"knowledge_config": "shared", "prompt_config": "prompt_a" if suffix == "甲" else "prompt_b"}).status_code, 200)
            endpoint = "/management/services/" + sid + "/knowledge"
            revision = self.api.get(endpoint).json()["revision"]
            self.assertEqual(self.api.put(endpoint, json={"content": "独立知识" + suffix, "revision": revision}).status_code, 200)
        for sid, iid, cid in (("service_a", 7, 100), ("service_b", 8, 200)):
            row = self.f.protocol.visitor(cid, iid)
            self.assertEqual(self.f.hook(row, sid).status_code, 200)
            self.f.tick()
        bodies = [json.dumps(body, ensure_ascii=False) for _, body in self.f.protocol.provider_calls]
        self.assertIn("独立知识甲", bodies[0]); self.assertNotIn("独立知识乙", bodies[0])
        self.assertIn("独立知识乙", bodies[1]); self.assertNotIn("独立知识甲", bodies[1])
        self.assertIn("只接待平台甲", bodies[0]); self.assertIn("只接待平台乙", bodies[1])
        # Authentic signature alone does not authorize a different inbox/account.
        wrong = self.f.protocol.visitor(201, 8)
        self.assertEqual(self.f.hook(wrong, "service_a").status_code, 422)
        self.assertEqual(self.f.hook(wrong, "service_b", changes={"account": {"id": 43}}).status_code, 422)
        self.assertEqual(self.f.hook(wrong, "service_b", changes={"inbox": {"id": 7}}).status_code, 422)
        self.assertEqual(self.f.hook(wrong, "service_b", signature="sha256=" + "0" * 64).status_code, 401)
        self.assertEqual(len(self.f.protocol.sent), 2)

    def test_failed_incremental_migration_leaves_old_records_and_passwords_intact(self):
        from ai_runtime.management_store import MIGRATIONS
        before = self.f.bridge.vault.get("chatwoot", "service_a")
        with self.f.bridge.store._connection(write=True) as db:
            db.execute("DROP TABLE support_entries")
            db.execute("PRAGMA user_version=4")
        migrations = {**MIGRATIONS, 5: (*MIGRATIONS[5], "INVALID SQL MUST FAIL")}
        with patch('ai_runtime.management_store.MIGRATIONS', migrations):
            self.assertEqual(self.api.post("/management/support-entries", json=self.record()).status_code, 503)
        with self.f.bridge.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='support_entries'").fetchone())
        self.assertEqual(self.f.bridge.vault.get("chatwoot", "service_a"), before)
        self.create()

    def test_incremental_migration_restart_reinstall_and_sqlite_backup_restore(self):
        self.api.put("/management/support-entry", json={"service_id": "service_a"})
        before = self.f.bridge.vault.get("chatwoot", "service_a")
        # Recreate the actual previous schema version without changing old rows.
        with self.f.bridge.store._connection(write=True) as db:
            db.execute("DROP TABLE support_entries")
            db.execute("PRAGMA user_version=4")
        self.assertEqual(self.api.get("/management/support-entries").json(), [])
        self.assertEqual(self.api.get("/management/support-entry/public").status_code, 200)
        self.create()
        self.api.post("/management/support-entries/platform-a/default")
        restart = SupportEntries(ChatwootBridge(self.f.engine, self.f.bridge.transport))
        self.assertEqual(restart.read()[0]["slug"], "platform-a")
        self.f.bridge.store.migrate_yaml(); self.f.bridge.store.migrate_yaml()
        self.assertEqual(restart.read()[0]["slug"], "platform-a")
        backup = self.f.fixture.root / "entries-backup.sqlite3"
        with closing(sqlite3.connect(self.f.bridge.store.path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        with self.f.bridge.store._connection(write=True) as db:
            db.execute("DELETE FROM support_entries")
        with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(self.f.bridge.store.path)) as target:
            source.backup(target)
        self.assertEqual(restart.read()[0]["slug"], "platform-a")
        self.assertEqual(asyncio.run(SupportEntry(restart.bridge).public())["website_token"], "public-website-token-7")
        self.assertEqual(self.f.bridge.vault.get("chatwoot", "service_a"), before)
        with self.f.bridge.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
