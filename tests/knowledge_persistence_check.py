"""Actual knowledge bytes survive installer/upgrade/tar restore fixtures.

Content is created through the authenticated API, never through argv or env.
"""

from pathlib import Path
import sys

from fastapi.testclient import TestClient
from ai_runtime.app import create_app
from ai_runtime.config import ServiceResolver
from ai_runtime.engine import RuntimeEngine, build_messages


mode, project, data_path = sys.argv[1:]
root, data = Path(project), Path(data_path)
env = {"SERVICE_MANAGEMENT_TOKEN": "fixture-knowledge-management-not-real-12345678",
       "AI_API_BASE_URL": "https://provider.example.test/v1"}
resolver = ServiceResolver(root / "config/services", data, env)
expected = {"service_a": "甲实例持久化知识正文\n升级、重复安装和备份恢复保持不变。",
            "service_b": "乙实例独立正文\n共享配置不共享正文。"}
with TestClient(create_app(RuntimeEngine(resolver)), headers={"authorization": "Bearer " + env["SERVICE_MANAGEMENT_TOKEN"]}) as api:
    if mode == "prepare":
        for kind, record in (("ai", {"id": "model", "model": "fixture-model"}),
                             ("knowledge", {"id": "shared", "source": "legacy/manual.txt", "max_characters": 12000})):
            assert api.post("/management/profiles/" + kind, json=record).status_code == 201
        for key, content in expected.items():
            assert api.post("/management/services", json={"id": key, "name": "通用客服", "ai_config": "model", "knowledge_config": "shared"}).status_code == 201
            base = "/management/services/" + key
            revision = api.get(base + "/knowledge").json()["revision"]
            assert api.put(base + "/knowledge", json={"content": content, "revision": revision}).status_code == 200
            assert api.post(base + "/enable").status_code == 200
    elif mode == "mutate":
        for key in expected:
            base = "/management/services/" + key + "/knowledge"
            revision = api.get(base).json()["revision"]
            assert api.put(base, json={"content": "待恢复的修改", "revision": revision}).status_code == 200
    elif mode == "verify":
        for key, content in expected.items():
            result = api.get("/management/services/" + key + "/knowledge")
            assert result.status_code == 200
            assert result.json()["content"] == content
            assert result.json()["source"] == "legacy/manual.txt"
            # A newly created resolver/engine reads the restored bytes directly.
            messages = build_messages(resolver.resolve(key), "fixture message")
            assert messages[0]["content"] == "<knowledge_context>\n" + content + "\n</knowledge_context>"
    else:
        raise AssertionError("unknown fixture mode")
print("Knowledge persistence " + mode + " passed.")
