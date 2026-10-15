"""Real Chromium + TLS Nginx + Admin + real management API visual workflows."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

import admin_browser_test as browser_fixtures
import platform_admin.app as admin_app
from platform_admin.clients import InternalClient
from ai_runtime.app import create_app as runtime_app
from ai_runtime.config import ServiceResolver
from ai_runtime.engine import RuntimeEngine


ROOT = Path(__file__).resolve().parents[1]


class VisualBrowserFixtures(unittest.TestCase):
    def setUp(self):
        self.driver = browser_fixtures.ChromiumHTTPSFixtures()
        self.addCleanup(lambda: self.assertTrue(self.driver.doCleanups()))
        factory = admin_app.create_app

        def backend():
            env = dict(os.environ)
            root = Path(env["ADMIN_DATA_PATH"]).parent
            config = root / "config" / "services"
            config.mkdir(parents=True)
            (config / "services.yaml").write_text("version: 1\nservices: []\n", encoding="utf-8")
            (config.parent / "domains").mkdir()
            (config.parent / "domains" / "domains.yaml").write_text("version: 1\ndomains: []\n", encoding="utf-8")
            self.resolver = ServiceResolver(config, root / "runtime-data", {**env, "AI_API_BASE_URL": "https://provider.example.test/v1"})
            self.runtime = runtime_app(RuntimeEngine(self.resolver))
            self.api = TestClient(self.runtime, headers={"authorization": "Bearer " + env["SERVICE_MANAGEMENT_TOKEN"]})
            self.addCleanup(self.api.close)
            management = InternalClient("http://runtime.test", env["SERVICE_MANAGEMENT_TOKEN"], httpx.ASGITransport(app=self.runtime))
            maintenance = InternalClient("http://maintenance.test", env["ADMIN_MAINTENANCE_TOKEN"],
                                         httpx.MockTransport(lambda _: httpx.Response(200, json={"backups": [], "upgrades": [], "jobs": []})))
            return factory(env, management, maintenance)

        with patch.object(admin_app, "create_app", side_effect=backend):
            self.driver.setUp()
        self.page = self.driver.page
        self.origin = self.driver.proxy.origin
        self.driver.login_page()
        self.driver.credentials()
        response, evidence = self.driver.submit()
        self.assertEqual(response.status, 303, str(evidence))
        self.page.wait_for_url(self.origin + "/admin")

    def open(self, kind, key="_new"):
        response = self.page.goto(self.origin + "/admin/edit/" + kind + "/" + key)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.page.locator('textarea[name="record"]').count(), 0)

    def fill(self, name, value):
        self.page.locator('#config-form [name="' + name + '"]').fill(value)

    def save(self, kind):
        path = urlsplit(self.page.url).path
        with self.page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path == path) as saved:
            self.page.get_by_role("button", name="保存配置", exact=True).click()
        self.assertEqual(saved.value.status, 303)
        self.page.wait_for_url(self.origin + "/admin/manage/" + kind + "?saved=1")
        self.assertIn("配置已保存", self.page.get_by_role("status").inner_text())

    def record(self, kind, key):
        path = "/management/" + (kind if kind in {"services", "domains"} else "profiles/" + kind)
        result = self.api.get(path + "/" + key)
        self.assertEqual(result.status_code, 200)
        return result.json()

    def test_all_seven_pages_create_edit_reopen_and_associate_without_json(self):
        self.open("ai")
        self.fill("id", "nested/model_a")
        self.fill("model", "generic-model")
        self.fill("api_key_env", "PROVIDER_CREDENTIAL")
        self.save("ai")
        self.open("ai", "nested/model_a")
        self.assertTrue(self.page.locator("#identity").get_attribute("readonly") is not None)
        self.fill("model", "edited-model")
        self.save("ai")
        self.assertEqual(self.record("ai", "nested/model_a")["api_key_env"], "PROVIDER_CREDENTIAL")

        self.open("prompts")
        self.fill("id", "prompt_a")
        self.fill("system_prompt", "\n通用提示词\n允许多行")
        self.fill("reply_rule_0", "第一条规则\n补充说明")
        self.page.get_by_role("button", name="添加一条规则").click()
        self.fill("reply_rule_1", "第二条规则")
        self.save("prompts")
        self.open("prompts", "prompt_a")
        self.assertEqual(self.page.locator('[name="reply_rule_0"]').input_value(), "第一条规则\n补充说明")
        self.fill("system_prompt", "修改后的提示词")
        self.save("prompts")
        self.assertEqual(self.record("prompts", "prompt_a")["reply_rules"], ["第一条规则\r\n补充说明", "第二条规则"])

        for kind, values, updated in (
            ("knowledge", {"id": "knowledge_a", "source": "manual.txt", "max_characters": "5000"}, {"max_characters": "6000"}),
            ("channels", {"id": "channel_a", "provider": "custom-provider", "config_ref": "group/connection"}, {"provider": "updated-provider"})):
            self.open(kind)
            for name, value in values.items():
                self.fill(name, value)
            self.save(kind)
            self.open(kind, values["id"])
            for name, value in updated.items():
                self.fill(name, value)
            self.save(kind)
            record = self.record(kind, values["id"])
            for name, value in updated.items():
                self.assertEqual(str(record[name]), value)

        service_data = self.resolver.data_dir / "services" / "service_a"
        service_data.mkdir(parents=True)
        (service_data / "manual.txt").write_text("隔离测试知识库", encoding="utf-8")
        self.open("services")
        self.fill("id", "service_a")
        self.fill("name", "通用客服实例")
        for field, value in (("ai_config", "nested/model_a"), ("prompt_config", "prompt_a"),
                             ("knowledge_config", "knowledge_a"), ("channel_config", "channel_a")):
            self.page.locator('[name="' + field + '"]').select_option(value)
        self.page.get_by_role("switch", name="启用实例").check()
        self.save("services")
        self.open("services", "service_a")
        self.assertEqual(self.page.locator('[name="ai_config"]').input_value(), "nested/model_a")
        self.fill("name", "已编辑客服实例")
        self.save("services")
        self.page.get_by_role("button", name="停用", exact=True).click()
        self.page.wait_for_url(self.origin + "/admin/manage/services?saved=1")
        self.assertFalse(self.record("services", "service_a")["enabled"])
        self.page.get_by_role("button", name="启用", exact=True).click()
        self.page.wait_for_url(self.origin + "/admin/manage/services?saved=1")
        self.assertTrue(self.record("services", "service_a")["enabled"])

        self.open("domains")
        self.fill("domain", self.driver.proxy.domain)
        self.page.locator('[name="service_id"]').select_option("service_a")
        self.page.get_by_role("switch", name="启用域名").check()
        self.page.get_by_role("switch", name="设为默认入口").check()
        self.save("domains")
        self.open("domains", self.driver.proxy.domain)
        self.page.get_by_role("switch", name="设为默认入口").uncheck()
        self.save("domains")
        self.assertFalse(self.record("domains", self.driver.proxy.domain)["default_entry"])

        self.open("runtime", "default")
        self.page.locator('[name="default_service_id"]').select_option("service_a")
        self.save("runtime")
        self.open("runtime", "default")
        self.assertEqual(self.page.locator('[name="default_service_id"]').input_value(), "service_a")
        self.assertEqual(self.resolver.resolve().model, "edited-model")

        self.open("services")
        self.fill("id", "service_a")
        self.fill("name", "未保存的重复实例")
        self.page.get_by_role("button", name="保存配置", exact=True).click()
        self.assertIn("该标识已存在", self.page.get_by_role("alert").inner_text())
        self.assertEqual(self.page.locator('[name="name"]').input_value(), "未保存的重复实例")
        self.fill("id", "Bad ID")
        self.assertFalse(self.page.locator("#identity").evaluate("node => node.checkValidity()"))

    def test_desktop_mobile_layout_and_no_credentials_in_any_page(self):
        for kind, record in (("ai", {"id": "model_a", "model": "generic-model"}),
                             ("prompts", {"id": "prompt_a", "system_prompt": "通用提示词"}),
                             ("knowledge", {"id": "knowledge_a", "source": ""}),
                             ("channels", {"id": "channel_a", "provider": "custom-provider"})):
            self.assertEqual(self.api.post("/management/profiles/" + kind, json=record).status_code, 201)
        self.assertEqual(self.api.post("/management/services", json={"id": "service_a", "name": "通用客服实例", "ai_config": "model_a"}).status_code, 201)
        (ROOT / "runtime" / "tests").mkdir(parents=True, exist_ok=True)
        screenshots = Path(tempfile.mkdtemp(prefix="admin-forms-ui-", dir=ROOT / "runtime" / "tests"))
        for width in (1440, 390, 320):
            self.page.set_viewport_size({"width": width, "height": 1000 if width > 640 else 844})
            for kind in ("services", "domains", "ai", "prompts", "knowledge", "channels", "runtime"):
                self.open(kind, "default" if kind == "runtime" else "_new")
                self.assertEqual(self.page.locator("html").get_attribute("lang"), "zh-CN")
                self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), kind)
                for locator in self.page.locator("#config-form input:not([type=hidden]), #config-form textarea, #config-form select").all():
                    box = locator.bounding_box()
                    self.assertTrue(box["x"] >= 0 and box["x"] + box["width"] <= width + 1, kind)
                content = self.page.content()
                for name in ("ADMIN_PASSWORD", "ADMIN_SESSION_SECRET", "SERVICE_MANAGEMENT_TOKEN", "ADMIN_MAINTENANCE_TOKEN"):
                    self.assertTrue(self.driver.proxy.env[name] not in content)
            self.open("services", "service_a")
            if width != 320:
                self.page.screenshot(path=str(screenshots / ("desktop.png" if width == 1440 else "mobile.png")), full_page=True)
        print("Visual review screenshots: " + str(screenshots))

    def test_native_form_origin_csrf_and_session_protections(self):
        self.open("services")
        self.fill("id", "safe")
        self.fill("name", "未保存实例")
        self.page.locator('#config-form [name="csrf"]').evaluate("node => node.value = 'invalid'")
        self.page.get_by_role("button", name="保存配置", exact=True).click()
        self.assertIn("csrf_rejected", self.page.content())

        self.open("services")
        csrf = self.page.locator('#config-form [name="csrf"]').input_value()
        self.page.goto(self.driver.same_site_attacker)
        self.page.evaluate("""({target, csrf}) => {
            const form = document.createElement('form'); form.method='post'; form.action=target;
            for (const [name,value] of Object.entries({csrf,id:'blocked',name:'跨站配置'})) {
                const input=document.createElement('input');input.name=name;input.value=value;form.append(input);
            }
            form.append(document.createElement('button'));document.body.append(form);
        }""", {"target": self.origin + "/admin/edit/services/_new", "csrf": csrf})
        self.page.locator("form button").click()
        self.assertIn("origin_rejected", self.page.content())
        self.assertEqual(self.api.get("/management/services").json(), [])

        self.open("services")
        self.fill("id", "safe")
        self.fill("name", "未保存实例")
        self.driver.context.clear_cookies()
        self.page.get_by_role("button", name="保存配置", exact=True).click()
        self.page.wait_for_url(self.origin + "/admin/login")
        self.assertEqual(self.api.get("/management/services").json(), [])


if __name__ == "__main__":
    unittest.main()
