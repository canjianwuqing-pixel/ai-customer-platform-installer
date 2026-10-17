"""Chromium + real Nginx/TLS, pinned v4.18 Widget endpoint builders.

The UI/HTTP upstream is a protocol harness, not the compiled Chatwoot app.
The real, unchanged endpoint builders require location.search at module load.
"""
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.responses import HTMLResponse, Response, JSONResponse

import admin_proxy_test as proxy
import support_test


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "tests/fixtures/chatwoot-v4.18.0"


class SupportBrowserFixtures(unittest.TestCase):
    def setUp(self):
        self.support = support_test.SupportFixtures()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.f = self.support.f
        self.driver = proxy.NginxHTTPSFixtures()
        self.addCleanup(self.driver.doCleanups)
        self.failures, self.messages = [], []
        app = self.support.app

        @app.get("/vite/assets/widget.js")
        def module():
            if getattr(self, "widget_module", None):
                return Response(self.widget_module, media_type="text/javascript")
            # Run the unmodified upstream builders. They read location.search
            # and therefore reproduce the previous HTML-200/messages-404 bug.
            return Response('''import endpoints from '/contract/api/endPoints';
                window.WOOT_WIDGET={$root:{$i18n:{locale:'zh_CN'}}};
                const root=document.querySelector('#app');
                root.innerHTML='<h1>网站客服协议测试</h1><div id="history"></div><form><label>消息<textarea required></textarea></label><button>发送</button></form>';
                const history=document.querySelector('#history');
                async function call(endpoint,method='GET') {
                  const response=await fetch(endpoint.url,{method,headers:{'Content-Type':'application/json','X-Auth-Token':window.authToken},
                    body:method==='POST'?JSON.stringify(endpoint.params):undefined});
                  if(!response.ok) throw Error('widget_http_'+response.status);
                  return response.json();
                }
                const loaded=await call(endpoints.getConversation({}));
                history.textContent=loaded.payload.map(m=>m.content).join('\\n');
                root.dataset.ready='true';
                root.querySelector('form').onsubmit=async event=>{
                  event.preventDefault();
                  const input=root.querySelector('textarea');
                  const result=await call(endpoints.createConversation({message:input.value}),'POST');
                  history.textContent=result.messages.map(m=>m.content).join('\\n');
                  input.value='';
                };''', media_type="text/javascript")

        @app.get("/contract/api/endPoints")
        def endpoints():
            return Response((SOURCES / "widget_endPoints.js").read_text(encoding="utf-8"), media_type="text/javascript")

        @app.get("/contract/helpers/urlParamsHelper")
        def helpers():
            return Response((SOURCES / "widget_urlParamsHelper.js").read_text(encoding="utf-8"), media_type="text/javascript")

        @app.get("/contract/api/events")
        def events():
            return Response("export const generateEventParams=()=>({});", media_type="text/javascript")

        @app.api_route("/api/v1/widget/{resource}", methods=["GET", "POST"])
        async def widget_api(resource: str, request: Request):
            # v4.18 WebsiteTokenHelper checks website_token before any message
            # read/write. Visitor auth is distinct from management credentials.
            token = request.query_params.get("website_token")
            if token not in {"public-website-token-7", "public-website-token-8"}:
                self.failures.append((resource, token))
                return JSONResponse({"error": "web widget does not exist"}, 404)
            if request.headers.get("x-auth-token") != ("native-visitor.jwt" if token.endswith('-7') else "other-visitor.jwt"):
                return JSONResponse({}, 401)
            if request.method == "POST":
                content = (await request.json())["message"]["content"]
                self.messages.append((token, content))
                iid = 7 if token.endswith('-7') else 8
                sid, cid = ('service_a', 100) if iid == 7 else ('service_b', 200)
                incoming = self.f.protocol.visitor(cid, iid, content)
                self.assertEqual(self.f.hook(incoming, sid).status_code, 200)
                # Exercise the real durable intake, instance resolver, knowledge,
                # mock OpenAI and authenticated Chatwoot delivery, not a UI echo.
                await self.f.bridge.tick()
            cid = 100 if token.endswith('-7') else 200
            rows = [{"content": row["content"]} for row in self.f.protocol.messages.get(cid, [])]
            return {"payload": rows, "messages": rows}

        customize_app = getattr(self, "customize_app", None)
        if customize_app:
            customize_app(app)
        with patch("platform_admin.app.create_app", return_value=app):
            self.driver.setUp()
        self.support.expected_host = self.driver.domain + ":" + str(self.driver.https_port)

    def chromium(self, playwright):
        # SPKI pin only the disposable test certificate, not global TLS bypass.
        import base64
        import hashlib
        import subprocess
        pub = subprocess.run([self.driver.openssl, "x509", "-in", str(self.driver.certificate), "-pubkey", "-noout"], capture_output=True, check=True).stdout
        der = subprocess.run([self.driver.openssl, "pkey", "-pubin", "-outform", "DER"], input=pub, capture_output=True, check=True).stdout
        pin = base64.b64encode(hashlib.sha256(der).digest()).decode()
        return playwright.chromium.launch(headless=True, executable_path=os.environ.get("TEST_CHROMIUM_BINARY"), args=[
            "--no-proxy-server", f"--host-resolver-rules=MAP {self.driver.domain} 127.0.0.1",
            "--ignore-certificate-errors-spki-list=" + pin])

    def test_native_widget_query_and_send_before_any_manual_url_edit(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright, self.chromium(playwright) as browser:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.driver.origin + "/support/")
            try:
                page.wait_for_selector('#app[data-ready="true"]', timeout=5000)
            except Exception:
                self.fail(f"Widget initialization failed: rejected API requests={self.failures}; browser errors={errors}")
            page.get_by_role("textbox", name="消息").fill("合法访客消息")
            page.get_by_role("button", name="发送", exact=True).click()
            page.get_by_text("模拟回复：合法访客消息", exact=False).wait_for()
            self.assertEqual(self.messages, [("public-website-token-7", "合法访客消息")])
            self.assertEqual(self.failures, [])
            self.assertEqual(errors, [])
            self.assertEqual(len(self.f.protocol.sent), 1)

    def test_admin_two_entry_crud_mobile_desktop_and_distinct_visitor_sessions(self):
        from playwright.sync_api import sync_playwright
        self.f.model("model_b")
        self.f.service("service_b", "model_b")
        self.f.connect("service_b", 8)
        with sync_playwright() as playwright, self.chromium(playwright) as browser:
            context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.driver.origin + "/admin/login")
            page.locator('[name="username"]').fill("admin")
            page.locator('[name="password"]').fill(self.f.fixture.password)
            page.get_by_role("button", name="登录", exact=True).click()
            page.wait_for_url(self.driver.origin + "/admin")
            for slug, sid, label in (("platform-a", "service_a", "平台甲"), ("platform-b", "service_b", "平台乙")):
                page.goto(self.driver.origin + "/admin/support")
                page.locator('#entry-name').fill(label)
                page.locator('#entry-slug').fill(slug)
                page.locator('#entry-service').select_option(sid)
                page.get_by_role("button", name="保存客服入口", exact=True).click()
                page.get_by_role("status").filter(has_text="已保存").wait_for()
            section = page.locator('section').filter(has=page.get_by_role('heading', name='平台乙', exact=True))
            section.get_by_role("link", name="编辑", exact=True).click()
            page.locator('#entry-name').fill("平台乙已编辑")
            page.get_by_role("button", name="保存客服入口", exact=True).click()
            section = page.locator('section').filter(has=page.get_by_role('heading', name='平台乙已编辑', exact=True))
            section.get_by_role("button", name="停用", exact=True).click()
            section.get_by_text("状态：已停用", exact=True).wait_for()
            section.get_by_role("button", name="启用", exact=True).click()
            section.get_by_role("button", name="设为默认客服", exact=True).click()
            page.get_by_role("button", name="复制客服链接", exact=True).last.click()
            page.get_by_text("客服链接已复制。", exact=True).wait_for()
            self.assertEqual(page.evaluate("navigator.clipboard.readText()"), self.driver.origin + "/support/platform-b/")
            review = ROOT / "runtime/tests" / ("support-entries-ui-" + os.urandom(4).hex())
            review.mkdir(parents=True)
            for width, height in ((1440, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                page.goto(self.driver.origin + "/admin/support")
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth<=innerWidth"))
                page.screenshot(path=str(review / f"entries-{width}.png"), full_page=True)
                for slug, iid in (("platform-a", 7), ("platform-b", 8)):
                    page.goto(self.driver.origin + "/support/" + slug + "/")
                    page.wait_for_selector('#app[data-ready="true"]')
                    self.assertIn("website_token=public-website-token-" + str(iid), page.url)
                    message = slug + "-" + str(width)
                    page.get_by_role("textbox", name="消息").fill(message)
                    page.get_by_role("button", name="发送", exact=True).click()
                    page.get_by_text(message, exact=False).wait_for()
                    page.reload()
                    page.wait_for_selector('#app[data-ready="true"]')
                    self.assertEqual(self.support.documents[-1].url.params.get("cw_conversation"), "native-visitor.jwt" if iid == 7 else "other-visitor.jwt")
                    self.assertNotIn("platform-b" if iid == 7 else "platform-a", page.locator('#history').inner_text())
                    page.screenshot(path=str(review / f"{slug}-{width}.png"), full_page=True)
                page.goto(self.driver.origin + "/support/")
                page.wait_for_selector('#app[data-ready="true"]')
                self.assertIn("website_token=public-website-token-8", page.url)
                result = page.goto(self.driver.origin + "/support/never-existed/")
                self.assertEqual(result.status, 404)
                self.assertEqual(page.locator("#app").count(), 0)
            page.goto(self.driver.origin + "/admin/support")
            page.locator('section').filter(has=page.get_by_role('heading', name='平台甲', exact=True)).get_by_role("button", name="删除入口", exact=True).click()
            self.assertEqual(page.locator('a[href="/support/platform-a/"]').count(), 0)
            self.assertEqual(self.failures, [])
            self.assertEqual(errors, [])
            print("Support entry review screenshots: " + str(review))


if __name__ == "__main__":
    unittest.main()
