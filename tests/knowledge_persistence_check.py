"""Actual knowledge bytes survive installer/upgrade/tar restore fixtures.

Content is created through the authenticated API, never through argv or env.
"""

from pathlib import Path
import sys
import httpx

from fastapi.testclient import TestClient
from ai_runtime.app import create_app
from ai_runtime.config import ServiceResolver
from ai_runtime.engine import RuntimeEngine, build_messages
from ai_runtime.vault import CredentialVault
import time
from openai_chatwoot_test import ChatwootProtocol


mode, project, data_path = sys.argv[1:]
root, data = Path(project), Path(data_path)
env = {"SERVICE_MANAGEMENT_TOKEN": "fixture-knowledge-management-not-real-12345678",
       "AI_API_BASE_URL": "https://provider.example.test/v1"}
resolver = ServiceResolver(root / "config/services", data, env)
expected = {"service_a": "甲实例持久化知识正文\n升级、重复安装和备份恢复保持不变。",
            "service_b": "乙实例独立正文\n共享配置不共享正文。"}
provider_key = "sk-persistence-fixture-not-real-1234567890123456"
protocol = ChatwootProtocol()
application = create_app(RuntimeEngine(resolver), chatwoot_transport=httpx.MockTransport(protocol))
with TestClient(application, headers={"authorization": "Bearer " + env["SERVICE_MANAGEMENT_TOKEN"]}) as api:
    if mode == "prepare":
        for kind, record in (("ai", {"id": "model", "model": "fixture-model"}),
                             ("knowledge", {"id": "shared", "source": "legacy/manual.txt", "max_characters": 12000})):
            assert api.post("/management/profiles/" + kind, json=record).status_code == 201
        for key, content in expected.items():
            assert api.post("/management/services", json={"id": key, "name": "通用客服", "enabled": True, "ai_config": "model"}).status_code == 201
            base = "/management/services/" + key
            assert api.patch(base, json={"knowledge_config": "shared"}).status_code == 200
            assert not api.get(base + "/knowledge").json()["exists"]
            assert build_messages(resolver.resolve(key), "fixture message") == [{"role": "user", "content": "fixture message"}]
            revision = api.get(base + "/knowledge").json()["revision"]
            assert api.put(base + "/knowledge", json={"content": content, "revision": revision}).status_code == 200
        assert api.post("/management/services", json={"id": "service_empty", "name": "尚无正文的客服",
            "enabled": True, "ai_config": "model", "knowledge_config": "shared"}).status_code == 201
        assert api.post("/management/models/save", json={"id": "model", "profile": {"model": "fixture-model"}, "api_key": provider_key}).status_code == 200
        for service_id, inbox in (("service_a", 7), ("service_b", 8)):
            assert api.put("/management/chatwoot/" + service_id + "/connection", json={"base_url": "https://chatwoot.example.test", "account_id": 42, "api_token": protocol.token}).status_code == 200
            assert api.post("/management/chatwoot/" + service_id + "/bind", json={"inbox_id": inbox, "public_url": "https://platform.example.test"}).status_code == 200
        bridge = application.state.chatwoot
        assert api.put("/management/support-entry", json={"service_id": "service_a"}).status_code == 200
        for sid, iid, slug in (("service_a", 7, "platform-a"), ("service_b", 8, "platform-b")):
            assert api.post("/management/support-entries", json={"slug": slug, "name": "独立平台",
                "service_id": sid, "account_id": 42, "inbox_id": iid, "enabled": True}).status_code == 201
        assert api.post("/management/support-entries/platform-a/default").status_code == 200
        bridge.pause("service_a", 100, "visitor_requested_human")
        bridge.lifecycle("service_a", {"id": 100, "status": "resolved", "updated_at": time.time(), "messages": [{"id": 20}]})
        bridge.pause("service_b", 200)
        with bridge.store._connection(write=True) as db:
            db.execute("INSERT INTO chatwoot_events(service_id,conversation_id,message_id,status,created_at) VALUES ('service_a',100,19,'sent',?)", (time.time(),))
    elif mode == "mutate":
        assert api.put("/management/support-entry", json={"service_id": "service_b"}).status_code == 200
        assert api.delete("/management/support-entries/platform-b").status_code == 200
        for key in expected:
            base = "/management/services/" + key + "/knowledge"
            revision = api.get(base).json()["revision"]
            assert api.put(base, json={"content": "待恢复的修改", "revision": revision}).status_code == 200
        assert api.post("/management/models/save", json={"id": "model", "profile": {"model": "fixture-model"}, "api_key": provider_key + "changed"}).status_code == 200
        for service_id in expected:
            assert api.put("/management/chatwoot/" + service_id + "/enabled", json={"enabled": False}).status_code == 200
        application.state.chatwoot.pause("service_a", 100)
        base = "/management/services/service_empty/knowledge"
        assert api.put(base, json={"content": "备份后才创建的正文", "revision": api.get(base).json()["revision"]}).status_code == 200
    elif mode == "verify":
        # Read the saved choice without calling remote Chatwoot in a fresh
        # process; the choice is in the existing backed-up Runtime volume.
        from ai_runtime.support_api import SupportEntry
        assert SupportEntry(application.state.chatwoot).selected() == "service_a"
        from ai_runtime.support_entries import SupportEntries
        entries = SupportEntries(application.state.chatwoot)
        assert [(row["slug"], row["service_id"], row["inbox_id"]) for row in entries.read()] == [
            ("platform-a", "service_a", 7), ("platform-b", "service_b", 8)]
        with entries.store._connection() as db:
            assert entries.default(db)["slug"] == "platform-a"
        for key, content in expected.items():
            result = api.get("/management/services/" + key + "/knowledge")
            assert result.status_code == 200
            assert result.json()["content"] == content
            assert result.json()["source"] == "legacy/manual.txt"
            # A newly created resolver/engine reads the restored bytes directly.
            messages = build_messages(resolver.resolve(key), "fixture message")
            assert messages[0]["content"] == "<knowledge_context>\n" + content + "\n</knowledge_context>"
            assert resolver.credential(resolver.resolve(key)) == provider_key
            assert api.get("/management/services/" + key).json()["enabled"]
        empty = api.get("/management/services/service_empty/knowledge").json()
        assert not empty["exists"] and empty["content"] == ""
        assert not (data / "services/service_empty/legacy/manual.txt").exists()
        assert api.get("/management/services/service_empty").json()["enabled"]
        assert build_messages(resolver.resolve("service_empty"), "fixture message") == [{"role": "user", "content": "fixture message"}]
        vault = CredentialVault(resolver)
        for service_id, inbox in (("service_a", 7), ("service_b", 8)):
            state = api.get("/management/chatwoot/" + service_id).json()
            assert state["enabled"] and state["inbox_id"] == inbox
            saved = vault.get("chatwoot", service_id)
            assert saved["api_token"] == protocol.token and saved["webhook_secret"] and saved["bot_token"]
        bridge = application.state.chatwoot
        closed = bridge.round_state("service_a", 100)
        handling = bridge.round_state("service_b", 200)
        assert closed["paused"] and closed["awaiting_reopen"] and closed["resume_after"] == 20
        assert handling["paused"] and not handling["awaiting_reopen"]
        with bridge.store._connection() as db:
            assert db.execute("SELECT status FROM chatwoot_events WHERE service_id='service_a' AND conversation_id=100 AND message_id=19").fetchone()[0] == 'sent'
    else:
        raise AssertionError("unknown fixture mode")
print("Knowledge, encrypted keys, Chatwoot bindings, lifecycle fences and dedup persistence " + mode + " passed.")
