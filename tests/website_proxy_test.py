"""Actual Nginx/SNI/TLS and Chromium on two independent same-origin sites.

The upstream Widget document is a protocol double, NOT a compiled Chatwoot
frontend or a claim of public certificate/live messaging acceptance.
"""
import base64
import hashlib
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import unittest
from unittest.mock import patch

import httpx

import admin_proxy_test as proxy
from platform_admin.app import create_app
from platform_admin.clients import InternalClient
from platform_admin.site_domains import WebsiteDomains


ROOT = Path(__file__).resolve().parents[1]


class MultiWebsiteProxyFixtures(unittest.TestCase):
    def setUp(self):
        self.driver = proxy.NginxHTTPSFixtures()
        self.addCleanup(lambda: self.assertTrue(self.driver.doCleanups()))
        self.alias = "b-" + secrets.token_hex(4) + ".example.test"
        self.jobs = []
        self.widget_requests = []
        factory = create_app

        def backend():
            env = dict(os.environ)

            def runtime(request):
                if request.headers.get("Authorization") != "Bearer " + env["SERVICE_MANAGEMENT_TOKEN"]:
                    return httpx.Response(401)
                if request.url.path == "/management/support-entry/public":
                    return httpx.Response(200, json={"website_token": "public-website-fixture"})
                if request.url.path == "/management/support-entry":
                    return httpx.Response(200, json={"selected": "instance", "options": [{"service_id": "instance", "name": "通用客服", "inbox_name": "网站客服"}]})
                return httpx.Response(200, json=[])

            def maintenance(request):
                if request.headers.get("Authorization") != "Bearer " + env["ADMIN_MAINTENANCE_TOKEN"]:
                    return httpx.Response(401)
                if request.url.path == "/website-domains":
                    return httpx.Response(200, json={"domains": [{"domain": self.alias, "primary": False, "ssl_status": "有效", "expires": "2099-01-01"}]})
                if request.url.path == "/jobs":
                    import json
                    self.jobs.append(json.loads(request.content))
                    return httpx.Response(200, json={"status": "accepted"})
                return httpx.Response(200, json={"jobs": [], "backups": [], "upgrades": []})

            def widget(request):
                self.widget_requests.append(request)
                return httpx.Response(200, text='<!doctype html><html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<script>window.chatwootWebChannel={websiteToken:"public-website-fixture"};window.authToken="visitor.fixture.jwt";</script>'
                    '</head><body><div id="app"></div></body></html>')

            return factory(env, InternalClient("http://runtime.test", env["SERVICE_MANAGEMENT_TOKEN"], httpx.MockTransport(runtime)),
                           InternalClient("http://worker.test", env["ADMIN_MAINTENANCE_TOKEN"], httpx.MockTransport(maintenance)), widget_transport=httpx.MockTransport(widget))

        def extra():
            self.alias_certificate = self.driver.directory / "backup.pem"
            alias_key = self.driver.directory / "backup.key"
            subprocess.run([self.driver.openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "1",
                "-keyout", str(alias_key), "-out", str(self.alias_certificate), "-subj", "/CN=" + self.alias,
                "-addext", "subjectAltName=DNS:" + self.alias], capture_output=True, check=True)
            values = {"DOMAIN": self.alias, "NGINX_LISTEN_PORT": str(self.driver.http_port),
                "DOMAIN_HTTPS_PORT": str(self.driver.https_port), "HTTPS_PORT_SUFFIX": ":" + str(self.driver.https_port),
                "CERTBOT_FULLCHAIN_PATH": self.alias_certificate.as_posix(), "CERTBOT_PRIVATE_KEY_PATH": alias_key.as_posix(),
                "WEBSITE_CERTBOT_WEBROOT": self.driver.directory.as_posix(), "NGINX_CLIENT_MAX_BODY_SIZE": "1m",
                "NGINX_UPSTREAM_HOST": "127.0.0.1", "CHATWOOT_HTTP_PORT": str(self.driver.backend_port),
                "NGINX_PROXY_READ_TIMEOUT_SECONDS": "5", "ADMIN_UPSTREAM_HOST": "127.0.0.1", "AI_ADMIN_HTTP_PORT": str(self.driver.backend_port),
                "INSTALLER_ROOT": str(ROOT), "ADMIN_MAINTENANCE_STATE_DIR": str(self.driver.directory / "state"),
                "NGINX_SITE_CONFIG_PATH": str(self.driver.directory / "site"), "ADMIN_ENABLED": "true",
                "DOMAIN_HTTPS_TEMPLATE_FILE": str(ROOT / "templates/domain/chatwoot-https.conf.template")}
            self.site_manager = WebsiteDomains(values)
            row = {"domain": self.alias, "fullchain": values["CERTBOT_FULLCHAIN_PATH"], "private_key": values["CERTBOT_PRIVATE_KEY_PATH"]}
            self.assertEqual(self.site_manager.certificate(row)["ssl_status"], "有效")
            template = self.site_manager.render({"version": 1, "primary": self.alias, "domains": [row]})
            template = re.sub(r"^\s*listen \[::\]:[^;]+;", "", template, flags=re.M)
            template = re.sub(r"listen (\d+)", r"listen 127.0.0.1:\1", template)
            return template.replace("\nunderscores_in_headers on;", "")

        self.driver.extra_nginx_servers = extra
        self.driver.leading_nginx_servers = lambda: "server_names_hash_bucket_size 128;\n"
        with patch("platform_admin.app.create_app", side_effect=backend):
            self.driver.setUp()
        self.driver.client.close()
        context = ssl.create_default_context(cafile=str(self.driver.certificate))
        context.load_verify_locations(cafile=str(self.alias_certificate))
        self.driver.client = httpx.Client(base_url=self.driver.origin, verify=context, trust_env=False, follow_redirects=False, timeout=5)
        self.driver.addCleanup(self.driver.client.close)
        self.alias_origin = "https://" + self.alias + ":" + str(self.driver.https_port)

    def request(self, method, url, **options):
        resolve = socket.getaddrinfo
        with patch("socket.getaddrinfo", side_effect=lambda host, *a, **kw: resolve("127.0.0.1" if host in {self.alias, self.driver.domain} else host, *a, **kw)):
            return self.driver.client.request(method, url, **options)

    def test_both_sni_names_admin_sessions_csrf_and_relative_support_assets(self):
        for origin in (self.driver.origin, self.alias_origin):
            csrf = proxy.csrf(self.request("GET", origin + "/admin/login"))
            response = self.request("POST", origin + "/admin/login", headers={"Origin": origin},
                data={"csrf": csrf, "username": "admin", "password": self.driver.env["ADMIN_PASSWORD"]})
            self.assertEqual(response.status_code, 303)
            page = self.request("GET", origin + "/admin/manage/domains")
            self.assertEqual(page.status_code, 200)
            self.assertIn("主域名", page.text)
            self.assertNotIn("service_id", page.text)
            support = self.request("GET", origin + "/support/")
            self.assertEqual(support.status_code, 200)
            self.assertIn('src="/support/session.js"', support.text)
            self.assertNotIn(self.driver.domain if origin == self.alias_origin else self.alias, support.text)
            self.assertNotIn("iframe", support.text)
            self.assertIn("Secure", response.headers["set-cookie"])
        self.assertTrue(all(r.url.host == "rails" for r in self.widget_requests))

    def test_alias_is_independent_when_primary_dns_cannot_resolve_and_native_form_ops(self):
        # Only resolve the backup in the client. No request is made to primary.
        csrf = proxy.csrf(self.request("GET", self.alias_origin + "/admin/login"))
        self.assertEqual(self.request("POST", self.alias_origin + "/admin/login", headers={"Origin": self.alias_origin},
            data={"csrf": csrf, "username": "admin", "password": self.driver.env["ADMIN_PASSWORD"]}).status_code, 303)
        page = self.request("GET", self.alias_origin + "/admin/websites")
        csrf = proxy.csrf(page)
        result = self.request("POST", self.alias_origin + "/admin/websites", headers={"Origin": self.alias_origin},
            data={"csrf": csrf, "action": "primary", "domain": self.alias})
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.jobs[-1]["operation"], "domain-primary")
        result = self.request("POST", self.alias_origin + "/admin/websites", headers={"Origin": self.driver.origin},
            data={"csrf": csrf, "action": "delete", "domain": self.alias})
        self.assertEqual(result.status_code, 403)

    def test_chromium_desktop_mobile_top_level_refresh_and_independent_alias_login(self):
        from playwright.sync_api import sync_playwright
        executable = os.environ.get("TEST_CHROMIUM_BINARY")
        pins = []
        for certificate in (self.driver.certificate, self.alias_certificate):
            pub = subprocess.run([self.driver.openssl, "x509", "-in", str(certificate), "-pubkey", "-noout"], capture_output=True, check=True).stdout
            der = subprocess.run([self.driver.openssl, "pkey", "-pubin", "-outform", "DER"], input=pub, capture_output=True, check=True).stdout
            pins.append(base64.b64encode(hashlib.sha256(der).digest()).decode())
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=executable, args=["--no-proxy-server",
                f"--host-resolver-rules=MAP {self.alias} 127.0.0.1,MAP {self.driver.domain} ~NOTFOUND", "--ignore-certificate-errors-spki-list=" + ",".join(pins)])
            try:
                context = browser.new_context()
                page = context.new_page()
                review = ROOT / "runtime/tests" / ("website-ui-" + secrets.token_hex(5))
                review.mkdir(parents=True)
                for width, height in ((1440, 900), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(self.alias_origin + "/admin/login")
                    page.locator('[name="username"]').fill("admin")
                    page.locator('[name="password"]').fill(self.driver.env["ADMIN_PASSWORD"])
                    page.get_by_role("button", name="登录", exact=True).click()
                    page.wait_for_url(self.alias_origin + "/admin")
                    page.goto(self.alias_origin + "/admin/websites")
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth<=innerWidth"))
                    page.screenshot(path=str(review / f"domains-{width}.png"), full_page=True)
                    page.goto(self.alias_origin + "/admin/support")
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth<=innerWidth"))
                    page.screenshot(path=str(review / f"support-settings-{width}.png"), full_page=True)
                    page.goto(self.alias_origin + "/support/")
                    page.wait_for_url(self.alias_origin + "/support/?website_token=public-website-fixture#/messages")
                    self.assertEqual(page.locator("iframe").count(), 0)
                    self.assertEqual(page.locator("button").count(), 0)
                    self.assertTrue(page.evaluate("window===window.top"))
                    box = page.locator("#app").bounding_box()
                    self.assertAlmostEqual(box["height"], height, delta=2)
                    page.reload()
                    self.assertEqual(self.widget_requests[-1].url.params.get("cw_conversation"), "visitor.fixture.jwt")
                context.close()
                print("Website management review screenshots: " + str(review))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
