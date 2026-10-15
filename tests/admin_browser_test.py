"""Real Chromium HTML form navigation over the production Nginx TLS templates.

No supplied Origin/Referer headers, disabled web security, personal browser
profile, system DNS/certificate changes, traces, HARs or credential logging.
"""

import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import urlsplit

import admin_proxy_test as proxy_fixtures

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class ChromiumHTTPSFixtures(unittest.TestCase):
    def setUp(self):
        required = os.environ.get("REQUIRE_ADMIN_BROWSER_TEST") == "true"
        if sync_playwright is None:
            if required:
                self.fail("Install tests/requirements-browser.txt and the Playwright Chromium browser.")
            self.skipTest("Optional Playwright/Chromium browser test dependencies are not installed.")
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        executable = os.environ.get("TEST_CHROMIUM_BINARY")
        if not executable:
            # Launch uses Playwright's matching headless shell by default.
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception:
                if required:
                    raise
                self.skipTest("Playwright Chromium is unavailable; install chromium --only-shell.")
            browser.close()
        elif not Path(executable).is_file():
            self.fail("TEST_CHROMIUM_BINARY is not a file.")

        self.proxy = proxy_fixtures.NginxHTTPSFixtures()
        self.addCleanup(lambda: self.assertTrue(self.proxy.doCleanups(), "Isolated proxy cleanup failed."))
        self.proxy.extra_nginx_servers = self.attacker_servers
        if required and not all(os.environ.get(variable) or shutil.which(command)
                                for variable, command in (("TEST_NGINX_BINARY", "nginx"), ("TEST_OPENSSL_BINARY", "openssl"))):
            self.fail("Real Nginx/OpenSSL are required for mandatory browser tests.")
        self.proxy.setUp()
        public_key = subprocess.run([self.proxy.openssl, "x509", "-in", str(self.proxy.certificate), "-pubkey", "-noout"],
                                    check=True, capture_output=True).stdout
        der = subprocess.run([self.proxy.openssl, "pkey", "-pubin", "-outform", "DER"], input=public_key,
                             check=True, capture_output=True).stdout
        pin = base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")
        browser = playwright.chromium.launch(headless=True, executable_path=executable, args=[
            "--no-proxy-server",
            "--host-resolver-rules=MAP " + self.proxy.domain + " 127.0.0.1,MAP " + self.proxy.foreign_domain + " 127.0.0.1",
            # Trust only this disposable certificate key, not arbitrary TLS
            # errors. Do not install a certificate or disable web security.
            "--ignore-certificate-errors-spki-list=" + pin])
        self.addCleanup(browser.close)
        self.browser_version = browser.version
        self.context = browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)

    def attacker_servers(self):
        port = self.proxy.available_port()
        self.same_site_attacker = "https://" + self.proxy.domain + ":" + str(port)
        self.cross_site_attacker = "https://" + self.proxy.foreign_domain + ":" + str(port)
        return ("\nserver { listen 127.0.0.1:" + str(port) + " ssl; server_name "
                + self.proxy.domain + " " + self.proxy.foreign_domain + ";\n"
                'ssl_certificate "' + self.proxy.certificate.as_posix() + '";\n'
                'ssl_certificate_key "' + (self.proxy.directory / "certificate.key").as_posix() + '";\n'
                "add_header Referrer-Policy strict-origin-when-cross-origin always;\n"
                "location / { default_type text/html; return 200 '<!doctype html><title>Isolated form fixture</title>'; }\n}\n")

    def login_page(self):
        response = self.page.goto(self.proxy.origin + "/admin/login")
        self.assertEqual(response.status, 200)
        # Check the actual production form, not a replacement fetch client.
        form = self.page.locator('form[action="/admin/login"]')
        self.assertEqual(form.get_attribute("method").lower(), "post")
        return response

    def credentials(self):
        self.page.locator('input[name="username"]').fill("admin")
        self.page.locator('input[name="password"]').fill(self.proxy.env["ADMIN_PASSWORD"])

    def submit(self, path="/admin/login"):
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and urlsplit(response.url).path == path) as posted:
            self.page.locator("form button").first.click()
        response = posted.value
        headers = response.request.all_headers()
        # Only public protocol metadata enters assertions/reports; no Cookie,
        # CSRF, Authorization, password, post_data or entire request dump.
        evidence = {"chromium": self.browser_version, "status": response.status,
                    "origin": headers.get("origin"), "fetch_site": headers.get("sec-fetch-site"),
                    "fetch_mode": headers.get("sec-fetch-mode"), "method": response.request.method,
                    "content_type": headers.get("content-type")}
        return response, evidence

    def test_browser_https_form_login_and_logout(self):
        document = self.login_page()
        self.credentials()
        response, evidence = self.submit()
        self.assertEqual(response.status, 303, str(evidence) + "; policy=" + document.headers.get("referrer-policy", ""))
        self.assertEqual(document.headers["referrer-policy"], "same-origin")
        self.assertEqual(evidence["origin"], self.proxy.origin)
        self.assertEqual(evidence["fetch_site"], "same-origin")
        self.assertEqual(evidence["fetch_mode"], "navigate")
        self.assertIn("application/x-www-form-urlencoded", evidence["content_type"])
        self.page.wait_for_url(self.proxy.origin + "/admin")
        cookie = next(item for item in self.context.cookies() if item["name"] == "platform_admin_session")
        self.assertTrue(cookie["secure"] and cookie["httpOnly"] and cookie["sameSite"] == "Strict")
        response, evidence = self.submit("/admin/logout")
        self.assertEqual(response.status, 303)
        self.assertEqual(evidence["origin"], self.proxy.origin)
        self.page.wait_for_url(self.proxy.origin + "/admin/login")

    def test_browser_null_origin_is_still_rejected(self):
        self.login_page()
        # Exercise browser policy processing, never set an Origin header.
        self.page.evaluate("""() => {
            const meta = document.createElement('meta');
            meta.name = 'referrer'; meta.content = 'no-referrer'; document.head.append(meta);
        }""")
        self.credentials()
        response, evidence = self.submit()
        self.assertEqual(evidence["origin"], "null")
        self.assertEqual(evidence["fetch_site"], "same-origin")
        self.assertEqual(response.status, 403)
        self.assertIn("origin_rejected", self.page.content())

    def attack_form(self, source, token):
        self.page.goto(source)
        self.page.evaluate("""({target, fields}) => {
            const form = document.createElement('form'); form.method = 'post'; form.action = target;
            for (const [name, value] of Object.entries(fields)) {
                const input = document.createElement('input'); input.name = name; input.value = value; form.append(input);
            }
            form.append(document.createElement('button')); document.body.append(form);
        }""", {"target": self.proxy.origin + "/admin/login", "fields": {
            "csrf": token, "username": "admin", "password": self.proxy.env["ADMIN_PASSWORD"]}})

    def test_browser_cross_origin_and_cross_site_forms_are_rejected(self):
        self.login_page()
        token = self.page.locator('input[name="csrf"]').input_value()
        # A different port is same-site: Strict cookies and a deliberately
        # known valid CSRF token are insufficient to bypass the Origin check.
        self.attack_form(self.same_site_attacker, token)
        response, evidence = self.submit()
        self.assertEqual(evidence["origin"], self.same_site_attacker)
        self.assertEqual(evidence["fetch_site"], "same-site")
        self.assertEqual(response.status, 403)
        self.assertIn("origin_rejected", self.page.content())

        self.login_page()
        token = self.page.locator('input[name="csrf"]').input_value()
        self.attack_form(self.cross_site_attacker, token)
        response, evidence = self.submit()
        self.assertEqual(evidence["origin"], self.cross_site_attacker)
        self.assertEqual(evidence["fetch_site"], "cross-site")
        # SameSite=Strict omits the session, so authentication rejects before
        # Origin processing and redirects to the login page.
        self.assertEqual(response.status, 303)
        self.page.wait_for_url(self.proxy.origin + "/admin/login")
        with proxy_fixtures.AuthStore(self.proxy.env).db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions WHERE authenticated=1").fetchone()[0], 0)

    def test_browser_csrf_and_session_checks_remain_required(self):
        self.login_page()
        self.page.locator('input[name="csrf"]').evaluate("node => node.value = 'invalid-csrf'")
        self.credentials()
        response, _ = self.submit()
        self.assertEqual(response.status, 403)
        self.assertIn("csrf_rejected", self.page.content())
        self.login_page()
        self.credentials()
        self.context.clear_cookies()
        response, _ = self.submit()
        self.assertEqual(response.status, 303)
        self.page.wait_for_url(self.proxy.origin + "/admin/login")

    def test_browser_same_origin_policy_does_not_leak_referrer_cross_origin(self):
        self.login_page()
        self.page.evaluate("""target => {
            const link = document.createElement('a'); link.href = target;
            link.id = 'external-fixture'; link.textContent = 'Fixture'; document.body.append(link);
        }""", self.same_site_attacker)
        with self.page.expect_request(self.same_site_attacker + "/") as navigated:
            self.page.locator("#external-fixture").click()
        self.assertNotIn("referer", navigated.value.all_headers())


if __name__ == "__main__":
    unittest.main()
