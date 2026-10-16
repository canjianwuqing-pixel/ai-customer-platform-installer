"""Real API/files/Admin flows, with provider transport only stubbed."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import admin_test
from admin_forms_test import Editor
from ai_runtime.app import create_app
from ai_runtime.config import RuntimeFailure, ServiceResolver
from ai_runtime.engine import ChatProvider, RuntimeEngine
from ai_runtime.knowledge import KnowledgeStore, MAX_CONTENT_BYTES, read_runtime_knowledge


class KnowledgeFixtures(unittest.TestCase):
    def setUp(self):
        self.fixture = admin_test.AdminFixtures()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.resolver = self.fixture.resolver
        self.resolver.env.update(INTERNAL_API_TOKEN="fixture-internal", OPENAI_API_KEY="fixture-provider")
        self.provider_requests = []

        def provider(request):
            self.provider_requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "fixture reply"}}]})

        self.app = create_app(RuntimeEngine(self.resolver, ChatProvider(httpx.MockTransport(provider))))
        self.api = TestClient(self.app, headers={"authorization": "Bearer " + self.fixture.env["SERVICE_MANAGEMENT_TOKEN"]})
        self.addCleanup(self.api.close)
        self.assertEqual(self.fixture.login().status_code, 303)
        self.client = self.fixture.client
        for kind, body in (("ai", {"id": "model", "model": "configured-model"}),
                           ("prompts", {"id": "prompt", "system_prompt": "unchanged policy", "reply_rules": ["unchanged rule"]}),
                           ("knowledge", {"id": "shared", "source": "nested/context.md", "max_characters": 12})):
            self.assertEqual(self.api.post("/management/profiles/" + kind, json=body).status_code, 201)
        for key in ("service_a", "service_b"):
            self.assertEqual(self.api.post("/management/services", json={"id": key, "name": "客服 " + key,
                "ai_config": "model", "prompt_config": "prompt", "knowledge_config": "shared"}).status_code, 201)

    def path(self, service="service_a"):
        return self.resolver.data_dir / "services" / service / "nested" / "context.md"

    def get(self, service="service_a"):
        response = self.api.get("/management/services/" + service + "/knowledge")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def write(self, text, service="service_a", *, revision=None, expected=200):
        revision = self.get(service)["revision"] if revision is None else revision
        response = self.api.put("/management/services/" + service + "/knowledge", json={"content": text, "revision": revision})
        self.assertEqual(response.status_code, expected, response.text)
        return response

    def form(self, service="service_a"):
        response = self.client.get("/admin/manage/knowledge", params={"service_id": service})
        self.assertEqual(response.status_code, 200, response.text)
        return Editor(response.text, "knowledge-form").data

    def enable_without_knowledge(self, service="service_a"):
        self.assertEqual(self.api.patch("/management/services/" + service, json={"knowledge_config": ""}).status_code, 200)
        self.assertEqual(self.api.post("/management/services/" + service + "/enable").status_code, 200)

    def chat(self, service="service_a"):
        response = self.api.post("/chat", json={"service_id": service, "message": "知识正文测试"},
                                 headers={"authorization": "Bearer fixture-internal"})
        self.assertEqual(response.status_code, 200, response.text)
        return self.provider_requests[-1]["messages"]

    def test_enabled_instance_first_association_is_empty_until_first_safe_save(self):
        self.enable_without_knowledge()
        with patch("ai_runtime.knowledge.read_runtime_knowledge", wraps=read_runtime_knowledge) as reader:
            result = self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"})
            self.assertEqual(reader.call_count, 1, "Association must still perform real runtime validation")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["enabled"])
        self.assertFalse(self.path().parent.exists(), "Validation must not create directories or files")
        self.assertEqual(self.chat(), [
            {"role": "system", "content": "unchanged policy\nunchanged rule"},
            {"role": "user", "content": "知识正文测试"},
        ])
        first = self.get()
        self.assertFalse(first["exists"])
        self.write("首次保存后立即加入知识上下文")
        self.assertEqual(self.chat()[1]["content"], "<knowledge_context>\n" + "首次保存后立即加入知识上下文"[:12] + "\n</knowledge_context>")
        self.write("修改后下次请求读取最新正文")
        self.assertEqual(self.chat()[1]["content"], "<knowledge_context>\n" + "修改后下次请求读取最新正文"[:12] + "\n</knowledge_context>")
        self.assertTrue(self.api.get("/management/services/service_a").json()["enabled"])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path().parent.stat().st_mode), 0o700)

    def test_runtime_reader_returns_empty_for_missing_leaf_or_parent_without_writes(self):
        record = self.api.get("/management/services/service_a").json()
        config = self.resolver.runtime_for_instance(record)
        self.assertEqual(read_runtime_knowledge(config), "")
        self.assertFalse(self.path().parent.exists())
        self.path().parent.mkdir(parents=True)
        self.assertEqual(read_runtime_knowledge(config), "")
        self.assertFalse(self.path().exists())

    def test_enabled_instance_switch_and_profile_update_preserve_all_old_files(self):
        original = "\ufeff原有知识正文\r\n全部保留".encode("utf-8")
        self.path().parent.mkdir(parents=True)
        self.path().write_bytes(original)
        self.assertEqual(self.api.post("/management/services/service_a/enable").status_code, 200)
        self.assertEqual(self.api.post("/management/profiles/knowledge", json={
            "id": "new_knowledge", "source": "new/knowledge.txt", "max_characters": 100}).status_code, 201)
        result = self.api.patch("/management/services/service_a", json={"knowledge_config": "new_knowledge"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["enabled"])
        self.assertEqual(self.path().read_bytes(), original)
        self.assertFalse(self.get()["exists"])
        self.assertNotIn("knowledge_context", str(self.chat()))
        self.write("新知识正文")
        new_file = self.resolver.data_dir / "services/service_a/new/knowledge.txt"
        result = self.api.put("/management/profiles/knowledge/new_knowledge", json={"source": "next/body.txt", "max_characters": 100})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(self.get()["exists"])
        self.assertEqual(new_file.read_text(encoding="utf-8"), "新知识正文")
        self.assertEqual(self.path().read_bytes(), original)
        self.assertEqual(self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"}).status_code, 200)
        self.assertEqual(self.get()["content"].encode("utf-8"), original)
        self.assertIn(original.decode("utf-8").replace("\r\n", "\n")[:12], self.chat()[1]["content"])

    def test_enabled_instances_share_missing_profile_but_not_content_after_restart(self):
        for service in ("service_a", "service_b"):
            self.enable_without_knowledge(service)
            result = self.api.patch("/management/services/" + service, json={"knowledge_config": "shared"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertNotIn("knowledge_context", str(self.chat(service)))
        self.write("甲实例独立知识", "service_a")
        self.assertEqual(self.get("service_b")["content"], "")
        self.assertFalse(self.get("service_b")["exists"])
        self.write("乙实例独立知识", "service_b")
        resolver = ServiceResolver(self.resolver.config_dir, self.resolver.data_dir, self.resolver.env)
        for service, expected in (("service_a", "甲实例独立知识"), ("service_b", "乙实例独立知识")):
            self.assertEqual(KnowledgeStore(resolver).content(service)["content"], expected)
            self.assertEqual(read_runtime_knowledge(resolver.resolve(service)), expected)
            self.assertEqual(self.chat(service)[1]["content"], "<knowledge_context>\n" + expected + "\n</knowledge_context>")

    def test_permission_io_and_invalid_utf8_are_not_silently_empty(self):
        self.enable_without_knowledge()
        self.path().parent.mkdir(parents=True)
        self.path().write_bytes(b"\xff\xfe")
        result = self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.path().read_bytes(), b"\xff\xfe")
        self.assertEqual(self.api.get("/management/services/service_a").json()["knowledge_config"], "")
        self.path().write_text("现有有效正文", encoding="utf-8")
        for error in (PermissionError("denied"), OSError("read failed")):
            with patch("ai_runtime.knowledge._open", side_effect=error):
                result = self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"})
            self.assertEqual(result.status_code, 422, result.text)
            self.assertEqual(self.api.get("/management/services/service_a").json()["knowledge_config"], "")
        self.assertEqual(self.path().read_text(encoding="utf-8"), "现有有效正文")
        self.assertEqual(self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"}).status_code, 200)
        for error in (PermissionError("denied"), OSError("read failed")):
            with patch("ai_runtime.knowledge._open", side_effect=error):
                response = self.api.post("/chat", json={"service_id": "service_a", "message": "不能忽略读错误"},
                    headers={"authorization": "Bearer fixture-internal"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"], "knowledge_source_unavailable")
        self.assertEqual(self.provider_requests, [])

    def test_missing_file_exception_does_not_bypass_link_or_file_type_validation(self):
        self.enable_without_knowledge()
        self.path().parent.mkdir(parents=True)
        missing = self.fixture.root / "must-not-create.txt"
        self.path().symlink_to(missing)
        self.assertEqual(self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"}).status_code, 422)
        self.assertFalse(missing.exists())
        self.path().unlink()
        self.path().mkdir()
        self.assertEqual(self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"}).status_code, 422)
        self.path().rmdir()
        self.path().parent.rmdir()
        self.path().parent.write_text("父路径不是目录", encoding="utf-8")
        self.assertEqual(self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"}).status_code, 422)
        self.path().parent.unlink()
        self.path().parent.symlink_to(self.fixture.root / "missing-directory", target_is_directory=True)
        self.assertEqual(self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"}).status_code, 422)
        self.path().parent.unlink()
        self.assertEqual(self.api.get("/management/services/service_a").json()["knowledge_config"], "")

    def test_link_added_after_configuration_validation_is_still_rejected(self):
        record = self.api.get("/management/services/service_a").json()
        config = self.resolver.runtime_for_instance(record)
        self.path().parent.mkdir(parents=True)
        self.path().symlink_to(self.fixture.root / "missing-file.txt")
        with self.assertRaises(RuntimeFailure) as failure:
            read_runtime_knowledge(config)
        self.assertEqual(failure.exception.code, "knowledge_source_unavailable")
        self.path().unlink()
        self.path().parent.rmdir()
        self.path().parent.symlink_to(self.fixture.root / "missing-directory", target_is_directory=True)
        with self.assertRaises(RuntimeFailure) as failure:
            read_runtime_knowledge(config)
        self.assertEqual(failure.exception.code, "knowledge_source_unavailable")
        self.path().parent.unlink()

    def test_first_save_failure_and_empty_save_keep_enabled_association_safe(self):
        self.enable_without_knowledge()
        self.assertEqual(self.api.patch("/management/services/service_a", json={"knowledge_config": "shared"}).status_code, 200)
        with patch("ai_runtime.knowledge.os.replace", side_effect=OSError("disk error")):
            self.write("未提交的首次正文", expected=503)
        self.assertFalse(self.path().exists())
        self.assertEqual(list(self.path().parent.glob(".knowledge-*")), [])
        self.assertFalse(self.get()["exists"])
        self.write("")
        self.assertTrue(self.get()["exists"])
        self.assertEqual(self.path().read_bytes(), b"")
        self.assertNotIn("knowledge_context", str(self.chat()))
        self.assertTrue(self.api.get("/management/services/service_a").json()["enabled"])

    def test_missing_profile_is_not_confused_with_missing_body(self):
        self.enable_without_knowledge()
        result = self.api.patch("/management/services/service_a", json={"knowledge_config": "nonexistent_profile"})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json()["error"], "invalid_runtime_configuration")
        self.assertEqual(self.api.get("/management/services/service_a").json()["knowledge_config"], "")

    def test_first_create_update_clear_and_preserve_legacy_source(self):
        first = self.get()
        self.assertFalse(first["exists"])
        self.assertFalse(self.path().exists())
        self.write("\n中文知识\r\n第二行🙂")
        self.assertEqual(self.get()["content"], "\n中文知识\r\n第二行🙂")
        self.write("更新后的正文")
        current = self.get()
        result = self.api.request("DELETE", "/management/services/service_a/knowledge", json={"revision": current["revision"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.get()["content"], "")
        self.assertTrue(self.path().is_file())
        profile = self.api.get("/management/profiles/knowledge/shared").json()
        self.assertEqual(profile, {"id": "shared", "source": "nested/context.md", "max_characters": 12})
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)

    def test_shared_profile_isolation_and_provider_reads_fresh_content_with_limit(self):
        for key, text in (("service_a", "甲实例的独立知识正文内容超过限制"), ("service_b", "乙实例独立知识")):
            self.write(text, key)
            self.assertEqual(self.api.post("/management/services/" + key + "/enable").status_code, 200)
        for text in ("这是更新后第一版的正文超过读取上限", "这是更新后第二版的正文超过读取上限"):
            self.write(text)
            result = self.api.post("/chat", json={"service_id": "service_a", "message": "用户问题"},
                                   headers={"authorization": "Bearer fixture-internal"})
            self.assertEqual(result.status_code, 200)
            sent = self.provider_requests[-1]
            self.assertEqual(sent["model"], "configured-model")
            self.assertEqual(sent["messages"][0]["content"], "unchanged policy\nunchanged rule")
            self.assertEqual(sent["messages"][1]["content"], "<knowledge_context>\n" + text[:12] + "\n</knowledge_context>")
            self.assertNotIn("乙实例", str(sent))
        self.assertEqual(self.get("service_b")["content"], "乙实例独立知识")

    def test_restart_and_new_resolver_reuse_same_files_and_management_database(self):
        self.write("持久化的知识正文")
        resolver = ServiceResolver(self.resolver.config_dir, self.resolver.data_dir, self.resolver.env)
        self.assertEqual(KnowledgeStore(resolver).content("service_a")["content"], "持久化的知识正文")
        self.assertEqual(self.api.get("/management/services/service_a").json()["knowledge_config"], "shared")

    def test_existing_utf8_file_preserved_on_read_and_clear_takes_effect_in_runtime(self):
        path = self.path()
        path.parent.mkdir(parents=True)
        original = "\ufeff原有知识文件\r\n多行内容".encode("utf-8")
        path.write_bytes(original)
        self.assertEqual(self.get()["content"].encode("utf-8"), original)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.api.post("/management/services/service_a/enable").status_code, 200)
        self.write("")
        result = self.api.post("/chat", json={"service_id": "service_a", "message": "问题"},
                               headers={"authorization": "Bearer fixture-internal"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.provider_requests[-1]["messages"]), 2)
        self.assertNotIn("knowledge_context", str(self.provider_requests[-1]))

    def test_large_unicode_html_body_and_oversized_raw_requests(self):
        content = "中文🙂\n" * 12000
        form = self.form()
        result = self.client.post("/admin/knowledge", data={**form, "content": content, "action": "save"})
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.get()["content"], content)
        result = self.api.put("/management/services/service_a/knowledge", content=b" " * (MAX_CONTENT_BYTES * 6 + 1025))
        self.assertEqual(result.status_code, 413)
        self.assertEqual(self.get()["content"], content)

    def test_invalid_percent_encoded_utf8_is_rejected_without_replacing_text(self):
        self.write("原有正文")
        form = self.form()
        raw = ("csrf=" + form["csrf"] + "&service_id=service_a&action=save&revision=" + form["revision"] + "&content=%FF")
        response = self.client.post("/admin/knowledge", content=raw,
                                    headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.get()["content"], "原有正文")
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/admin/static/knowledge.js").status_code, 303)
        self.assertEqual(self.client.get("/admin/manage/knowledge?service_id=service_a").status_code, 303)

    def test_different_profiles_use_same_instance_scoping_and_missing_source_is_explicit(self):
        self.write("实例 A 的原文件")
        self.assertEqual(self.api.post("/management/profiles/knowledge", json={"id": "separate", "source": "knowledge.txt"}).status_code, 201)
        self.assertEqual(self.api.patch("/management/services/service_b", json={"knowledge_config": "separate"}).status_code, 200)
        self.write("实例 B 的独立文件", "service_b")
        self.assertEqual(self.get()["content"], "实例 A 的原文件")
        self.assertEqual(self.get("service_b")["content"], "实例 B 的独立文件")
        self.assertEqual(self.api.put("/management/profiles/knowledge/separate", json={"source": ""}).status_code, 200)
        response = self.client.get("/admin/manage/knowledge?service_id=service_b")
        self.assertEqual(response.status_code, 409)
        self.assertIn("文件来源为空", response.text)
        self.assertTrue((self.resolver.data_dir / "services/service_b/knowledge.txt").exists())

    def test_stale_content_or_binding_and_parallel_writers_do_not_overwrite(self):
        stale = self.get()["revision"]
        self.write("当前版本")
        self.write("过期覆盖", revision=stale, expected=409)
        revision = self.get()["revision"]
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda text: self.api.put("/management/services/service_a/knowledge",
                json={"revision": revision, "content": text}).status_code, ("竞争一", "竞争二")))
        self.assertEqual(sorted(results), [200, 409])
        previous = self.get()["revision"]
        self.assertEqual(self.api.put("/management/profiles/knowledge/shared", json={"source": "other.txt"}).status_code, 200)
        self.write("错误新路径", revision=previous, expected=409)
        self.assertFalse((self.path().parents[1] / "other.txt").exists())

    def test_unauthorized_invalid_service_missing_association_and_extra_fields(self):
        for method in ("GET", "PUT", "DELETE"):
            with TestClient(self.app) as anonymous:
                result = anonymous.request(method, "/management/services/service_a/knowledge", json={})
                self.assertEqual(result.status_code, 401)
        self.assertEqual(self.api.get("/management/services/missing/knowledge").status_code, 404)
        self.assertEqual(self.api.get("/management/services/Bad_ID/knowledge").status_code, 422)
        self.assertEqual(self.api.patch("/management/services/service_b", json={"knowledge_config": ""}).status_code, 200)
        self.assertEqual(self.api.get("/management/services/service_b/knowledge").status_code, 409)
        self.assertEqual(self.api.put("/management/services/service_a/knowledge",
            json={"content": "bad", "revision": self.get()["revision"], "source": "../.env"}).status_code, 422)

    def test_paths_symlinks_hardlinks_and_non_text_files_are_rejected(self):
        for source in ("../secret.txt", "/etc/passwd", "other/../context.txt", "x\\context.txt", "C:secret.txt",
                       ".env", "config.yaml", "token/.env", "auth.sqlite3", "private.key", "./data.txt",
                       "token.txt", "database.ini", "nginx.conf", "settings.toml"):
            result = self.api.post("/management/profiles/knowledge", json={"id": "unsafe", "source": source})
            self.assertEqual(result.status_code, 422, source)
        self.write("原文件")
        original = self.path().read_bytes()
        outside = self.fixture.root / "outside.txt"
        outside.write_text("不可访问的文件", encoding="utf-8")
        self.path().unlink()
        self.path().symlink_to(outside)
        self.assertNotEqual(self.api.get("/management/services/service_a/knowledge").status_code, 200)
        self.path().unlink()
        os.link(outside, self.path())
        self.assertNotEqual(self.api.get("/management/services/service_a/knowledge").status_code, 200)
        self.path().unlink()
        self.path().write_bytes(b"\xff\xfe")
        self.assertEqual(self.api.get("/management/services/service_a/knowledge").status_code, 422)
        self.path().write_bytes(original)
        nested = self.path().parent
        moved = nested.with_name("old")
        nested.rename(moved)
        nested.symlink_to(moved, target_is_directory=True)
        try:
            self.assertNotEqual(self.api.get("/management/services/service_a/knowledge").status_code, 200)
        finally:
            nested.unlink()
            moved.rename(nested)
        self.assertEqual(outside.read_text(encoding="utf-8"), "不可访问的文件")

    def test_size_utf8_atomic_failure_no_content_in_logs_or_errors(self):
        self.write("原有正确知识")
        for text, status in (("中" * (MAX_CONTENT_BYTES // 3 + 1), 413), ("text\x00bad", 422)):
            self.write(text, expected=status)
        self.assertEqual(self.api.put("/management/services/service_a/knowledge", content=json.dumps(
            {"content": "\ud800", "revision": self.get()["revision"]}), headers={"content-type": "application/json"}).status_code, 422)
        original = self.path().read_bytes()
        for target in ("ai_runtime.knowledge.os.replace", "ai_runtime.knowledge.os.fsync"):
            with patch(target, side_effect=OSError("simulated disk error")):
                with self.assertLogs("ai_runtime", level="WARNING") as logged:
                    result = self.write("绝不能泄露的完整知识正文", expected=503)
                self.assertNotIn("绝不能泄露", "".join(logged.output) + result.text)
            self.assertEqual(self.path().read_bytes(), original)
            self.assertEqual(list(self.path().parent.glob(".knowledge-*")), [])
        self.path().write_bytes(b"x" * (MAX_CONTENT_BYTES + 1))
        self.assertEqual(self.api.get("/management/services/service_a/knowledge").status_code, 413)

    def test_admin_form_roundtrip_clear_feedback_and_security(self):
        form = self.form()
        form.update(content="\n网页中文知识\n<script>not executable</script>", action="save")
        result = self.client.post("/admin/knowledge", data=form, headers={"origin": "https://platform.example.test"})
        self.assertEqual(result.status_code, 303)
        reopened = self.form()
        self.assertEqual(reopened["content"], form["content"])
        self.assertEqual(self.api.post("/management/services/service_a/enable").status_code, 200)
        reply = self.api.post("/chat", json={"service_id": "service_a", "message": "网页保存后读取"},
                              headers={"authorization": "Bearer fixture-internal"})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(self.provider_requests[-1]["messages"][1]["content"],
                         "<knowledge_context>\n" + form["content"][:12] + "\n</knowledge_context>")
        self.assertIn("&lt;script&gt;", self.client.get(result.headers["location"]).text)
        self.assertIn("正文已保存", self.client.get(result.headers["location"]).text)
        for headers, expected in (({}, 403), ({"origin": "null"}, 403), ({"origin": "https://attacker.test"}, 403)):
            payload = {**reopened, "action": "save"}
            if not headers:
                payload["csrf"] = "invalid"
            self.assertEqual(self.client.post("/admin/knowledge", data=payload, headers=headers).status_code, expected)
        stale = {**reopened, "action": "save", "content": "未保存草稿"}
        self.write("其他窗口保存")
        failed = self.client.post("/admin/knowledge", data=stale)
        self.assertEqual(failed.status_code, 409)
        self.assertIn("未保存草稿", failed.text)
        self.assertIn('data-dirty="true"', failed.text)
        cleared = self.client.post("/admin/knowledge", data={**self.form(), "action": "clear"})
        self.assertEqual(cleared.status_code, 303)
        self.assertEqual(self.form()["content"], "")
        self.assertEqual(Editor(self.client.get("/admin/edit/knowledge/_new").text).data["source"], "knowledge.txt")
        for value in (self.fixture.env["SERVICE_MANAGEMENT_TOKEN"], self.fixture.env["ADMIN_SESSION_SECRET"]):
            self.assertNotIn(value, failed.text)


if __name__ == "__main__":
    unittest.main()
