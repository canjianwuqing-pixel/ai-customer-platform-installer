"""Signed v4.18 lifecycle events, persistent handoff rounds and old-message fences."""

import copy
import hashlib
import hmac
import json
import shutil
import sqlite3
from contextlib import closing
import time
import unittest
from unittest.mock import patch

from ai_runtime.app import create_app
from ai_runtime.chatwoot import ChatwootBridge
from ai_runtime.config import ServiceResolver
from ai_runtime.engine import RuntimeEngine
from ai_runtime.management_store import MIGRATIONS

import openai_chatwoot_test as integration


class LifecycleFixtures(unittest.TestCase):
    def setUp(self):
        self.f = integration.IntegrationFixtures()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.prepared()
        self.clock = time.time()

    def lifecycle(self, status, previous, *, deliver=True):
        self.clock += 1
        conversation = self.f.protocol.conversations[100]
        conversation.update(status=status, updated_at=self.clock)
        payload = {**copy.deepcopy(conversation), "account": {"id": 42},
                   "messages": copy.deepcopy(self.f.protocol.messages[100][-1:]),
                   "event": "conversation_status_changed",
                   "changed_attributes": [{"status": {"previous_value": previous, "current_value": status}}]}
        return self.send(payload) if deliver else payload

    def send(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        timestamp = str(int(time.time()))
        secret = self.f.bridge.client(self.f.bridge.binding("service_a")).credentials["webhook_secret"]
        signature = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return self.f.api.post("/integrations/chatwoot/service_a/webhook", content=raw,
            headers={"x-chatwoot-timestamp": timestamp, "x-chatwoot-signature": signature})

    def handoff(self):
        self.f.hook(self.f.protocol.visitor(content="请转人工客服"))
        self.f.tick()
        self.assertTrue(self.f.bridge.conversation_state("service_a", 100)[0])
        self.assertEqual(self.f.protocol.conversations[100]["status"], "open")

    def test_resolve_then_reuse_same_conversation_automatically_resumes(self):
        self.handoff()
        human = self.f.protocol.human()
        self.f.hook(human)
        waiting = self.f.protocol.visitor(content="人工处理期间的问题")
        self.f.hook(waiting)
        self.f.tick()
        self.assertEqual(self.f.protocol.sent, [])
        self.assertEqual(self.lifecycle("resolved", "open").status_code, 200)
        new = self.f.protocol.visitor(content="新一轮咨询")
        self.lifecycle("pending", "resolved")
        self.f.hook(new)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)
        self.assertEqual(self.f.protocol.sent[0]["content"], "模拟回复：新一轮咨询")
        self.assertFalse(self.f.bridge.conversation_state("service_a", 100)[0])
        self.assertEqual(self.f.hook(human).json()["status"], "ignored")
        self.f.hook(waiting)
        self.assertFalse(self.f.tick())

    def test_reopen_event_recovers_when_resolved_event_was_not_delivered(self):
        self.handoff()
        self.lifecycle("resolved", "open", deliver=False)
        new = self.f.protocol.visitor(content="无需人工操作恢复")
        self.lifecycle("pending", "resolved")
        self.f.hook(new)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)

    def test_message_worker_before_reopen_event_is_safely_requeued(self):
        self.handoff()
        self.lifecycle("resolved", "open", deliver=False)
        row = self.f.protocol.visitor(content="消息比状态事件先到达")
        pending = self.lifecycle("pending", "resolved", deliver=False)
        self.f.hook(row)
        self.f.tick()
        self.assertEqual(self.f.protocol.sent, [])
        self.send(pending)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)
        self.send(pending)
        self.f.hook(row)
        self.assertFalse(self.f.tick())

    def test_new_round_and_other_service_state_are_isolated(self):
        self.handoff()
        self.f.model("model_b", self.f.key_b, "independent-model")
        self.f.service("service_b", "model_b")
        self.f.connect("service_b", 8)
        self.f.hook(self.f.protocol.visitor(200, 8, "其他实例正常接待"), "service_b")
        self.lifecycle("resolved", "open")
        self.f.hook(self.f.protocol.visitor(300, 7, "同实例其他访客"))
        self.f.tick()
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 2)
        self.assertNotIn("其他实例", str(self.f.protocol.provider_calls[-1]))
        self.assertFalse(self.f.bridge.conversation_state("service_b", 200)[0])

    def test_old_sending_reconciliation_does_not_disarm_resolved_round(self):
        row = self.f.protocol.visitor(content="上次断电前待核对的消息")
        self.f.hook(row)
        event = self.f.bridge.claim()
        self.f.bridge.finish(event, "sending", reply="不能盲目重发")
        self.lifecycle("resolved", "pending")
        self.f.due()
        self.f.tick()
        self.assertEqual(self.f.status()[0][0], "uncertain")
        self.assertTrue(self.f.bridge.round_state("service_a", 100)["awaiting_reopen"])
        self.assertEqual(self.f.protocol.sent, [])
        new = self.f.protocol.visitor(content="之后的新咨询")
        self.lifecycle("pending", "resolved")
        self.f.hook(new)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)

    def test_closed_browser_snooze_open_and_elapsed_time_never_resume_ai(self):
        self.handoff()
        for status, previous in (("snoozed", "open"), ("open", "snoozed"), ("pending", "open")):
            self.lifecycle(status, previous)
            self.f.hook(self.f.protocol.visitor(content="仍在人工服务中"))
            self.f.tick()
        self.assertEqual(self.f.protocol.sent, [])
        self.assertTrue(self.f.bridge.conversation_state("service_a", 100)[0])

    def test_reordered_duplicate_lifecycle_and_old_human_do_not_repause_new_round(self):
        self.handoff()
        old_human = self.f.protocol.human()
        self.f.hook(old_human)
        resolved = self.lifecycle("resolved", "open", deliver=False)
        new = self.f.protocol.visitor(content="新问题")
        reopened = self.lifecycle("pending", "resolved", deliver=False)
        self.send(reopened)
        self.send(resolved)
        self.f.hook(new)
        self.f.tick()
        self.send(resolved)
        self.send(reopened)
        self.f.hook(old_human)
        self.f.hook(self.f.protocol.visitor(content="新一轮第二个问题"))
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 2)

    def test_queued_old_messages_are_discarded_and_generation_is_fenced(self):
        self.handoff()
        old = self.f.protocol.visitor(content="不能补发的旧消息")
        self.f.hook(old)
        self.lifecycle("resolved", "open")
        self.assertFalse(self.f.tick())
        new = self.f.protocol.visitor(content="新轮第一问")
        self.lifecycle("pending", "resolved")
        self.f.hook(new)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)
        self.assertNotIn("不能补发的旧消息", str(self.f.protocol.provider_calls[-1]))

    def test_close_and_reopen_during_generation_cannot_send_previous_round_reply(self):
        first = self.f.protocol.visitor(content="即将被关闭的问题")
        self.f.hook(first)
        original = self.f.engine.respond

        async def interrupt(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.lifecycle("resolved", "pending")
            self.new = self.f.protocol.visitor(content="下一轮问题")
            self.lifecycle("pending", "resolved")
            return result

        with patch.object(self.f.engine, "respond", side_effect=interrupt):
            self.f.tick()
        self.assertEqual(self.f.protocol.sent, [])
        self.f.hook(self.new)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)
        self.assertIn("下一轮问题", self.f.protocol.sent[0]["content"])

    def test_human_takes_over_during_automatic_assignment_and_wins(self):
        self.handoff()
        self.lifecycle("resolved", "open")
        new = self.f.protocol.visitor(content="重新打开")
        self.lifecycle("pending", "resolved")
        self.f.hook(new)
        original = self.f.protocol.__call__

        def request(req):
            result = original(req)
            if req.url.path.endswith("/assignments"):
                self.lifecycle("open", "pending")
                self.f.protocol.conversations[100]["meta"].update(assignee_type="User", assignee={"id": 99})
            return result

        import httpx
        self.f.bridge.transport = httpx.MockTransport(request)
        self.f.tick()
        self.assertEqual(self.f.protocol.sent, [])
        self.assertTrue(self.f.bridge.conversation_state("service_a", 100)[0])

    def test_human_webhook_during_final_api_get_wins_transactional_send_fence(self):
        self.f.hook(self.f.protocol.visitor(content="发送前人工接管"))
        original = self.f.protocol.__call__
        reads = 0

        def request(req):
            nonlocal reads
            result = original(req)
            if req.method == "GET" and req.url.path.endswith("/conversations/100"):
                reads += 1
                if reads == 2:
                    self.f.bridge.pause("service_a", 100)
            return result

        import httpx
        self.f.bridge.transport = httpx.MockTransport(request)
        self.f.tick()
        self.assertEqual(self.f.protocol.sent, [])
        self.assertEqual(self.f.status()[0][0], "handoff")

    def test_solving_or_manual_pending_without_new_message_cannot_send_old_reply(self):
        self.handoff()
        self.lifecycle("resolved", "open")
        self.assertFalse(self.f.tick())
        self.lifecycle("pending", "resolved")
        self.assertFalse(self.f.tick())
        self.assertEqual(self.f.protocol.sent, [])

    def test_live_status_open_or_other_bot_cannot_be_overridden(self):
        self.handoff()
        self.lifecycle("resolved", "open")
        row = self.f.protocol.visitor(content="事件和现场不一致")
        self.lifecycle("pending", "resolved")
        self.f.protocol.conversations[100]["status"] = "open"
        self.f.hook(row)
        self.f.tick()
        self.assertEqual(self.f.protocol.sent, [])
        self.assertFalse(self.f.bridge.round_state("service_a", 100)["awaiting_reopen"])
        self.assertFalse(any(path.endswith("/assignments") for _, path in self.f.protocol.calls))

    def test_pause_and_resolved_round_survive_runtime_restart_and_volume_restore(self):
        self.handoff()
        paused = ChatwootBridge(self.f.engine, self.f.bridge.transport)
        self.assertTrue(paused.conversation_state("service_a", 100)[0])
        self.lifecycle("resolved", "open")
        target = self.f.resolver.data_dir.parent / "restored-runtime-data"
        shutil.copytree(self.f.resolver.data_dir, target)
        resolver = ServiceResolver(self.f.resolver.config_dir, target, self.f.resolver.env)
        restored = ChatwootBridge(RuntimeEngine(resolver, self.f.engine.provider), self.f.bridge.transport)
        self.assertEqual(restored.round_state("service_a", 100), self.f.bridge.round_state("service_a", 100))
        self.f.bridge = restored
        # Recreate HTTP routes as well: their closures must use restored storage.
        self.f.app = create_app(restored.engine, chatwoot_transport=restored.transport)
        from fastapi.testclient import TestClient
        self.f.api = TestClient(self.f.app)
        self.addCleanup(self.f.api.close)
        self.f.bridge = self.f.app.state.chatwoot
        row = self.f.protocol.visitor(content="恢复卷后的新咨询")
        self.lifecycle("pending", "resolved")
        self.f.hook(row)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)

    def test_disabled_service_does_not_resume_and_other_visitors_are_isolated(self):
        self.handoff()
        self.lifecycle("resolved", "open")
        self.f.hook(self.f.protocol.visitor(200, content="另一个访客正常接待"))
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)
        self.f.bridge.toggle("service_a", False)
        row = self.f.protocol.visitor(content="AI停用时不恢复")
        self.lifecycle("pending", "resolved")
        self.f.hook(row)
        self.f.tick()
        self.assertEqual(len(self.f.protocol.sent), 1)
        self.assertEqual(self.f.protocol.conversations[100]["status"], "open")

    def test_lifecycle_auth_account_inbox_and_payload_validation(self):
        self.handoff()
        event = self.lifecycle("resolved", "open", deliver=False)
        for change in ({"account": {"id": 43}}, {"inbox_id": 8}, {"id": "100"},
                       {"updated_at": float("nan")}, {"messages": "not-a-list"},
                       {"changed_attributes": [{"status": "invalid"}]}):
            self.assertEqual(self.send({**event, **change}).status_code, 422)
        response = self.f.api.post("/integrations/chatwoot/service_a/webhook", json=event)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.f.bridge.round_state("service_a", 100)["awaiting_reopen"])

    def test_admin_no_conversation_identifiers_or_manual_resume_actions(self):
        self.handoff()
        page = self.f.client.get("/admin/chatwoot?service_id=service_a")
        for text in ("会话人工接管状态", "会话 ID", 'name="conversation_id"', 'value="resume"', "恢复 AI</button>"):
            self.assertNotIn(text, page.text)
        state = self.f.api.get("/management/chatwoot/service_a").json()
        self.assertIn("conversations", state, "Preserve the existing authenticated internal API contract")
        self.assertEqual(self.f.client.post("/admin/chatwoot", data={"csrf": self.f.fixture.csrf(),
            "action": "resume", "service_id": "service_a", "conversation_id": "100"}).status_code, 422)

    def test_v3_migration_preserves_bindings_credentials_pause_and_dedup(self):
        self.handoff()
        path = self.f.bridge.store.path
        # Reconstruct the exact old schema with its existing data.
        old = path.with_name("old.sqlite3")
        with closing(sqlite3.connect(old)) as db, db, closing(sqlite3.connect(path)) as current:
            for version in (1, 2, 3):
                for statement in MIGRATIONS[version]:
                    db.execute(statement)
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
                names = ','.join(columns)
                rows = current.execute(f"SELECT {names} FROM {table}").fetchall()
                db.executemany(f"INSERT INTO {table} ({names}) VALUES ({','.join('?' for _ in columns)})", rows)
            db.execute("PRAGMA user_version=3")
        old.chmod(0o600)
        old.replace(path)
        before = self.f.bridge.client(self.f.bridge.binding("service_a")).credentials
        self.lifecycle("resolved", "open")
        self.assertEqual(self.f.bridge.client(self.f.bridge.binding("service_a")).credentials, before)
        self.assertTrue(self.f.bridge.round_state("service_a", 100)["awaiting_reopen"])
        self.assertEqual(self.f.status()[0][0], "handoff")


if __name__ == "__main__":
    unittest.main()
