"""Actual Chromium HTTPS forms, dynamic model discovery and inbox binding."""

from pathlib import Path
import json
import tempfile
import unittest

import httpx

import admin_forms_browser_test as visual
from openai_chatwoot_test import ChatwootProtocol


class IntegrationBrowserFixtures(unittest.TestCase):
    open = visual.VisualBrowserFixtures.open
    fill = visual.VisualBrowserFixtures.fill
    save = visual.VisualBrowserFixtures.save

    def setUp(self):
        self.protocol = ChatwootProtocol()
        visual.VisualBrowserFixtures.setUp(self)

    def test_visual_vision_switch_and_multimodal_connection_probe(self):
        requests = []
        def provider(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'blue'}}]})
        self.runtime.state.chatwoot.engine.provider.transport = httpx.MockTransport(provider)
        self.open('ai')
        self.fill('id', 'vision-profile')
        self.fill('model', 'freely-selected/vision')
        self.fill('api_key', 'sk-vision-browser-fixture-not-real-1234567890123')
        switch = self.page.get_by_role('switch', name='此模型支持图片识别')
        self.assertFalse(switch.is_checked())
        self.assertIn('费用', self.page.locator('#hint-supports_vision').inner_text())
        switch.check()
        self.page.get_by_role('button', name='测试模型连接').click()
        self.page.locator('#model-feedback').filter(has_text='图片连接测试成功').wait_for()
        self.assertEqual(requests[-1]['model'], 'freely-selected/vision')
        self.assertEqual(requests[-1]['messages'][-1]['content'][1]['type'], 'image_url')
        self.save('ai')
        self.open('ai', 'vision-profile')
        self.assertTrue(switch.is_checked())
        switch.uncheck()
        self.save('ai')
        self.open('ai', 'vision-profile')
        self.assertFalse(switch.is_checked())

    def test_real_browser_models_credentials_manual_fallback_and_chatwoot(self):
        key = "sk-browser-fixture-not-real-1234567890123456"
        replacement = "sk-browser-replacement-not-real-09876543210"
        self.open("ai")
        self.fill("id", "browser/model")
        self.fill("api_base_url", "https://provider.example.test/v1")
        self.fill("api_key", key)
        self.page.get_by_role("button", name="获取模型列表").click()
        self.page.locator("#model-feedback").filter(has_text="模型列表已加载").wait_for()
        self.page.locator("#model-options").select_option("vendor/arbitrary-model-2077")
        self.assertEqual(self.page.locator('[name="model"]').input_value(), "vendor/arbitrary-model-2077")
        self.page.get_by_role("button", name="测试模型连接").click()
        self.page.locator("#model-feedback").filter(has_text="模型连接成功").wait_for()
        self.assertEqual(self.protocol.provider_calls[-1][1]["model"], "vendor/arbitrary-model-2077")
        self.save("ai")
        self.open("ai", "browser/model")
        self.assertIn(key[:8] + "********", self.page.locator("#key-prefix").inner_text())
        self.assertNotIn(key, self.page.content())
        self.assertEqual(self.page.locator('[name="api_key"]').input_value(), "")
        self.fill("api_key", replacement)
        self.protocol.models_failure = True
        self.page.get_by_role("button", name="获取模型列表").click()
        self.page.locator("#model-feedback").filter(has_text="手动").wait_for()
        self.fill("model", "manual-vendor/model-without-listing")
        self.page.get_by_role("button", name="测试模型连接").click()
        self.page.locator("#model-feedback").filter(has_text="模型连接成功").wait_for()
        self.assertEqual(self.protocol.provider_calls[-1][1]["model"], "manual-vendor/model-without-listing")
        self.save("ai")
        self.open("ai", "browser/model")
        self.assertIn(replacement[:8], self.page.locator("#key-prefix").inner_text())
        self.protocol.provider_failure = True
        self.page.get_by_role("button", name="测试模型连接").click()
        self.page.locator("#model-feedback").filter(has_text="模型请求失败").wait_for()
        self.assertNotIn(replacement, self.page.content())
        self.protocol.provider_failure = False
        self.assertEqual(self.api.post("/management/services", json={"id": "browser_service", "name": "通用客服实例", "enabled": True, "ai_config": "browser/model"}).status_code, 201)
        self.page.goto(self.origin + "/admin/chatwoot?service_id=browser_service")
        self.page.locator('[name="base_url"]').fill("https://chatwoot.example.test")
        self.page.locator('[name="account_id"]').fill("42")
        self.page.locator('[name="api_token"]').fill(self.protocol.token)
        self.page.get_by_role("button", name="保存连接并获取网站收件箱").click()
        self.page.locator("#chatwoot-inbox").select_option("7")
        self.page.get_by_role("button", name="绑定收件箱并启用 AI").click()
        self.page.get_by_text("当前实例 AI：已启用", exact=False).wait_for()
        bot = self.protocol.bots[self.protocol.links[7]]
        self.assertEqual(bot["outgoing_url"], self.origin + "/integrations/chatwoot/browser_service/webhook")
        self.runtime.state.chatwoot.pause("browser_service", self.protocol.visitor()["conversation_id"])
        self.page.reload()
        self.assertEqual(self.page.get_by_role("button", name="恢复 AI", exact=True).count(), 0)
        self.assertNotIn("会话 ID", self.page.content())
        self.assertNotIn('name="conversation_id"', self.page.content())
        self.page.get_by_text("访客聊天、人工接管和解决会话均在 Chatwoot 后台操作。", exact=False).wait_for()
        self.page.get_by_role("button", name="停用 AI", exact=True).click()
        self.page.get_by_text("当前实例 AI：已停用", exact=False).wait_for()
        self.page.get_by_role("button", name="启用 AI", exact=True).click()
        screenshots = Path(tempfile.mkdtemp(prefix="openai-chatwoot-ui-", dir=visual.ROOT / "runtime/tests"))
        for width in (1440, 390, 320):
            self.page.set_viewport_size({"width": width, "height": 1000 if width > 640 else 844})
            for path in ("/admin/chatwoot?service_id=browser_service", "/admin/edit/ai/browser/model"):
                self.page.goto(self.origin + path)
                self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                for secret in (key, replacement, self.protocol.token, bot["secret"], bot["access_token"]["token"]):
                    self.assertNotIn(secret, self.page.content())
                if width != 320:
                    name = ("chatwoot" if "chatwoot" in path else "model") + ("-desktop.png" if width > 640 else "-mobile.png")
                    self.page.screenshot(path=str(screenshots / name), full_page=True)
        print("OpenAI/Chatwoot browser screenshots: " + str(screenshots))


if __name__ == "__main__":
    unittest.main()
