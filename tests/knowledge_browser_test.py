"""Native HTML knowledge forms through real Chromium, TLS Nginx and Admin."""

from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

import admin_forms_browser_test as browser_forms
from ai_runtime.engine import build_messages


class KnowledgeBrowserFixtures(unittest.TestCase):
    setUp = browser_forms.VisualBrowserFixtures.setUp

    def prepare(self):
        self.assertEqual(self.api.post("/management/profiles/knowledge", json={"id": "shared", "source": "knowledge.txt"}).status_code, 201)
        for key in ("service_a", "service_b"):
            self.assertEqual(self.api.post("/management/services", json={"id": key, "name": "客服实例 " + key,
                "knowledge_config": "shared"}).status_code, 201)
        self.page.goto(self.origin + "/admin/manage/knowledge")
        self.page.locator("#service-picker").select_option("service_a")
        self.page.wait_for_url(self.origin + "/admin/manage/knowledge?service_id=service_a")
        self.page.locator("#knowledge-content").wait_for()

    def save(self, name="保存正文"):
        with self.page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path == "/admin/knowledge") as posted:
            self.page.get_by_role("button", name=name, exact=True).click()
        self.assertEqual(posted.value.status, 303)
        self.page.wait_for_url(self.origin + "/admin/manage/knowledge?service_id=service_a&saved=1")
        self.assertIn("正文已保存", self.page.get_by_role("status").inner_text())

    def test_enabled_instance_associates_new_knowledge_in_native_form_and_saves_body(self):
        self.assertEqual(self.api.post("/management/profiles/ai", json={"id": "model", "model": "configured-model"}).status_code, 201)
        self.assertEqual(self.api.post("/management/profiles/knowledge", json={
            "id": "shared", "source": "new/knowledge.txt", "max_characters": 12}).status_code, 201)
        self.assertEqual(self.api.post("/management/services", json={"id": "service_a", "name": "客服实例",
            "enabled": True, "ai_config": "model"}).status_code, 201)
        browser_forms.VisualBrowserFixtures.open(self, "services", "service_a")
        self.assertTrue(self.page.locator('#config-form [name="enabled"]').is_checked())
        self.page.locator('#config-form [name="knowledge_config"]').select_option("shared")
        browser_forms.VisualBrowserFixtures.save(self, "services")
        record = self.api.get("/management/services/service_a").json()
        self.assertTrue(record["enabled"])
        self.assertEqual(record["knowledge_config"], "shared")
        self.assertNotIn("knowledge_context", str(build_messages(self.resolver.resolve("service_a"), "probe")))
        self.page.goto(self.origin + "/admin/manage/knowledge?service_id=service_a")
        self.page.get_by_text("尚无正文文件，当前按空知识处理。", exact=False).wait_for()
        self.assertEqual(self.page.locator("#knowledge-content").input_value(), "")
        text = "网页首次创建知识正文，保存后立即读取"
        self.page.locator("#knowledge-content").fill(text)
        self.save()
        self.page.reload()
        self.assertEqual(self.page.locator("#knowledge-content").input_value(), text)
        self.assertEqual(build_messages(self.resolver.resolve("service_a"), "probe")[0]["content"],
                         "<knowledge_context>\n" + text[:12] + "\n</knowledge_context>")
        self.assertTrue(self.api.get("/management/services/service_a").json()["enabled"])

    def test_native_create_edit_clear_isolation_and_responsive_layout(self):
        self.prepare()
        self.assertIn("尚无正文文件", self.page.content())
        content = "\n中文知识正文\n<script>window.executed = true</script>\n多行🙂"
        self.page.locator("#knowledge-content").fill(content)
        self.save()
        self.assertEqual(self.page.locator("#knowledge-content").input_value(), content)
        self.assertIsNone(self.page.evaluate("window.executed"))
        self.page.locator("#knowledge-content").fill("更新后的正文")
        self.save()
        self.page.reload()
        self.assertEqual(self.page.locator("#knowledge-content").input_value(), "更新后的正文")
        other = self.api.get("/management/services/service_b/knowledge").json()
        self.assertFalse(other["exists"])
        self.assertEqual(other["content"], "")
        directory = Path(__file__).resolve().parents[1] / "runtime" / "tests"
        directory.mkdir(parents=True, exist_ok=True)
        screenshots = Path(tempfile.mkdtemp(prefix="knowledge-ui-", dir=directory))
        for width in (1440, 390, 320):
            self.page.set_viewport_size({"width": width, "height": 1000 if width > 640 else 844})
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            for selector in ("#service-picker", "#knowledge-content", "#clear-knowledge"):
                box = self.page.locator(selector).bounding_box()
                self.assertTrue(box["x"] >= 0 and box["x"] + box["width"] <= width + 1)
            if width != 320:
                self.page.screenshot(path=str(screenshots / ("desktop.png" if width == 1440 else "mobile.png")), full_page=True)
        self.assertTrue(self.page.locator("#clear-knowledge").is_disabled())
        self.page.locator("#confirm-clear").check()
        self.save("清空正文")
        self.assertEqual(self.page.locator("#knowledge-content").input_value(), "")
        print("Knowledge visual review screenshots: " + str(screenshots))

    def test_unsaved_draft_protection_and_native_csrf_session_origin(self):
        self.prepare()
        self.page.locator("#knowledge-content").fill("不应丢失的草稿")
        dialogs = []

        def dismiss(dialog):
            dialogs.append(dialog.type)
            dialog.dismiss()

        self.page.on("dialog", dismiss)
        self.page.get_by_role("link", name="重新加载", exact=True).click()
        self.assertEqual(dialogs, ["beforeunload"])
        self.assertEqual(self.page.locator("#knowledge-content").input_value(), "不应丢失的草稿")
        self.page.locator("#service-picker").select_option("service_b")
        self.assertEqual(dialogs, ["beforeunload", "confirm"])
        self.assertEqual(self.page.locator("#service-picker").input_value(), "service_a")
        self.assertEqual(self.page.locator("#knowledge-content").input_value(), "不应丢失的草稿")
        self.page.remove_listener("dialog", dismiss)
        self.save()
        csrf = self.page.locator('#knowledge-form [name="csrf"]').input_value()
        revision = self.page.locator('#knowledge-form [name="revision"]').input_value()
        self.page.goto(self.driver.same_site_attacker)
        self.page.evaluate("""({target, csrf, revision}) => {
            const form = document.createElement('form'); form.method='post'; form.action=target;
            for (const [name,value] of Object.entries({csrf,revision,service_id:'service_a',content:'跨站覆盖',action:'save'})) {
                const input=document.createElement('input');input.name=name;input.value=value;form.append(input);
            }
            form.append(document.createElement('button'));document.body.append(form);
        }""", {"target": self.origin + "/admin/knowledge", "csrf": csrf, "revision": revision})
        self.page.locator("form button").click()
        self.assertIn("origin_rejected", self.page.content())
        self.assertEqual(self.api.get("/management/services/service_a/knowledge").json()["content"], "不应丢失的草稿")
        self.page.goto(self.origin + "/admin/manage/knowledge?service_id=service_a")
        self.page.locator('#knowledge-form [name="csrf"]').evaluate("node => node.value = 'invalid'")
        self.page.get_by_role("button", name="保存正文", exact=True).click()
        self.assertIn("csrf_rejected", self.page.content())
        self.page.goto(self.origin + "/admin/manage/knowledge?service_id=service_a")
        self.driver.context.clear_cookies()
        self.page.get_by_role("button", name="保存正文", exact=True).click()
        self.page.wait_for_url(self.origin + "/admin/login")


if __name__ == "__main__":
    unittest.main()
