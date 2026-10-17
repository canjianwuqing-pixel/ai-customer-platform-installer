"""CSP regression with actual pinned Vue/i18n/utils, real Chromium/Nginx/TLS.

Build the test-only bundle as documented in fixtures/widget-csp/README.md.
This is NOT the entire compiled Chatwoot frontend or live production acceptance.
"""
import os
from pathlib import Path
import unittest

from fastapi.responses import Response

import support_entries_browser_test as support_browser


ROOT = Path(__file__).resolve().parents[1]


class WidgetCSPFixtures(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ.get('TEST_WIDGET_CSP_BUNDLE', ROOT / 'runtime/tests/widget-csp.bundle.js'))
        if not path.is_file():
            message = 'Build the pinned Widget CSP dependency bundle; see tests/fixtures/widget-csp/README.md.'
            if os.environ.get('REQUIRE_WIDGET_CSP_TEST') == 'true':
                self.fail(message)
            self.skipTest(message)
        self.harness = support_browser.SupportBrowserFixtures()
        self.harness.widget_module = path.read_text(encoding='utf-8')
        self.harness.customize_app = self.configure_app
        self.legacy_bootstrap = False
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def configure_app(self, app):
        @app.middleware('http')
        async def legacy(request, call_next):
            if self.legacy_bootstrap and request.url.path == '/support/session.js':
                source = (ROOT / 'platform_admin/static/support.js').read_text(encoding='utf-8')
                # The previous bootstrap already initialized token/history/cookie
                # correctly. Omit ONLY the new dependency initialization.
                source = '(() => {\n' + source[source.index('  const token ='):]
                return Response(source, media_type='text/javascript')
            return await call_next(request)

        @app.get('/contract/csp-probe.js')
        def probe():
            # Executed as a real same-origin script, not DevTools evaluate()
            # (CDP itself can bypass CSP). The attempts MUST remain blocked.
            return Response('''window.cspProbe = {};
                try { Function('return 42')(); } catch (error) { window.cspProbe.functionBlocked = error instanceof EvalError; }
                try { eval('42'); } catch (error) { window.cspProbe.evalBlocked = error instanceof EvalError; }''', media_type='text/javascript')

    def playwright(self):
        from playwright.sync_api import sync_playwright
        return sync_playwright()

    def assert_support_policy(self, response):
        self.assertEqual(response.headers['content-security-policy'],
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; media-src 'self' blob:; "
            "connect-src 'self' wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

    def test_real_dependencies_mount_on_default_and_two_isolated_support_entries(self):
        f = self.harness.f
        f.model('model_b'); f.service('service_b', 'model_b'); f.connect('service_b', 8)
        for slug, sid, iid in (('platform-a', 'service_a', 7), ('platform-b', 'service_b', 8)):
            result = f.api.post('/management/support-entries', json={'slug': slug, 'name': '通用平台',
                'service_id': sid, 'account_id': 42, 'inbox_id': iid, 'enabled': True})
            self.assertEqual(result.status_code, 201)
        self.assertEqual(f.api.post('/management/support-entries/platform-a/default').status_code, 200)
        with self.playwright() as playwright, self.harness.chromium(playwright) as browser:
            page = browser.new_page()
            errors, violations = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: violations.append(message.text) if 'Content Security Policy' in message.text else None)
            for path, iid in (('/support/', 7), ('/support/platform-a/', 7), ('/support/platform-b/', 8)):
                response = page.goto(self.harness.driver.origin + path)
                self.assertEqual(response.status, 200)
                self.assert_support_policy(response)
                try:
                    page.wait_for_selector('#app[data-v-app] section[data-ready="true"]', timeout=5000)
                except Exception:
                    self.fail(f'Actual Vue/utils dependencies could not mount: errors={errors}; CSP={violations}')
                self.assertTrue(page.get_by_role('heading', name='在线客服').is_visible())
                self.assertEqual(page.evaluate('typeof window.WOOT_WIDGET.$el'), 'object')
                self.assertEqual(page.evaluate('typeof window.regeneratorRuntime.wrap'), 'function')
                self.assertTrue(page.evaluate('window.__INTLIFY_JIT_COMPILATION__'))
                self.assertIn('website_token=public-website-token-' + str(iid), page.url)
                message = 'CSP测试-' + str(iid) + '-' + path
                page.get_by_role('textbox', name='消息').fill(message)
                page.get_by_role('button', name='发送', exact=True).click()
                page.get_by_text('模拟回复：' + message, exact=True).wait_for()
            self.assertEqual(errors, [])
            self.assertEqual(violations, [])

    def test_previous_bootstrap_reproduces_eval_error_and_corrected_bootstrap_mounts(self):
        with self.playwright() as playwright, self.harness.chromium(playwright) as browser:
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            self.legacy_bootstrap = True
            response = page.goto(self.harness.driver.origin + '/support/')
            self.assert_support_policy(response)
            page.wait_for_function("document.readyState === 'complete'")
            self.assertTrue(any('unsafe-eval' in error for error in errors), errors)
            self.assertEqual(page.locator('#app[data-v-app]').count(), 0)
            self.legacy_bootstrap = False
            errors.clear()
            page.reload()
            page.wait_for_selector('#app[data-v-app] section[data-ready="true"]')
            self.assertEqual(errors, [])

    def test_eval_remains_blocked_in_support_and_admin_policies_are_unchanged(self):
        default_admin = ("default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        with self.playwright() as playwright, self.harness.chromium(playwright) as browser:
            page = browser.new_page()
            response = page.goto(self.harness.driver.origin + '/support/')
            self.assert_support_policy(response)
            page.wait_for_selector('#app[data-v-app] section[data-ready="true"]')
            page.add_script_tag(url=self.harness.driver.origin + '/contract/csp-probe.js')
            self.assertEqual(page.evaluate('window.cspProbe'), {'functionBlocked': True, 'evalBlocked': True})
            self.assertEqual(page.goto(self.harness.driver.origin + '/admin/login').headers['content-security-policy'], default_admin)
            page.locator('[name="username"]').fill('admin')
            page.locator('[name="password"]').fill(self.harness.f.fixture.password)
            page.get_by_role('button', name='登录', exact=True).click()
            page.wait_for_url(self.harness.driver.origin + '/admin')
            for path, suffix in (('/admin', ''), ('/admin/password', ''), ('/admin/support', "; script-src 'self'")):
                response = page.goto(self.harness.driver.origin + path)
                self.assertEqual(response.headers['content-security-policy'], default_admin + suffix)
                self.assertNotIn('unsafe-eval', response.headers['content-security-policy'])
            page.add_script_tag(url=self.harness.driver.origin + '/contract/csp-probe.js')
            self.assertEqual(page.evaluate('window.cspProbe'), {'functionBlocked': True, 'evalBlocked': True})


if __name__ == '__main__':
    unittest.main()
