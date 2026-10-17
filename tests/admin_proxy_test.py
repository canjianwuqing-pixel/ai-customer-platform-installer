"""Origin regressions and real TLS Nginx -> HTTP Uvicorn authentication tests.

Set TEST_NGINX_BINARY/TEST_OPENSSL_BINARY when the tools are not on PATH.
REQUIRE_ADMIN_PROXY_TEST=true turns missing integration tools into a failure.
All domains, credentials, certificates, ports and storage are disposable.
"""

import os
from pathlib import Path
import re
import runpy
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient
import uvicorn

from platform_admin.app import create_app
from platform_admin.auth import AuthStore


ROOT = Path(__file__).resolve().parent.parent


def environment(directory):
    return {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": secrets.token_urlsafe(24),
            "ADMIN_SESSION_SECRET": secrets.token_hex(32), "ADMIN_MAINTENANCE_TOKEN": secrets.token_hex(32),
            "SERVICE_MANAGEMENT_TOKEN": secrets.token_hex(32), "ADMIN_DATA_PATH": str(directory / "data")}


def csrf(response):
    assert response.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


class OriginFixtures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="admin-origin-")
        self.addCleanup(temporary.cleanup)
        self.env = environment(Path(temporary.name))
        self.domain = secrets.token_hex(8) + ".example.test"
        self.foreign_origin = "https://" + secrets.token_hex(8) + ".invalid"

    def login(self, authority, origin, root_path=""):
        with TestClient(create_app(self.env), base_url="https://" + self.domain,
                        root_path=root_path, follow_redirects=False) as client:
            token = csrf(client.get("/admin/login"))
            return client.post("/admin/login", headers={"host": authority, "origin": origin},
                               data={"csrf": token, "username": "admin", "password": self.env["ADMIN_PASSWORD"]})

    def test_https_default_port_and_host_case_are_the_same_origin(self):
        for authority in (self.domain, self.domain + ":443", self.domain.upper()):
            with self.subTest(authority=authority):
                self.assertEqual(self.login(authority, "https://" + self.domain).status_code, 303)
        self.assertEqual(self.login(self.domain, "https://" + self.domain + ":443").status_code, 303)

    def test_origin_does_not_include_asgi_root_path(self):
        self.assertEqual(self.login(self.domain, "https://" + self.domain, root_path="/gateway").status_code, 303)

    def test_invalid_or_cross_site_origins_remain_forbidden(self):
        valid = "https://" + self.domain
        for origin in ("null", "", "http://" + self.domain, valid + ":444", valid + "/path",
                       valid + "/", valid + "?", valid + "#", "https://user@" + self.domain,
                       "https://" + self.domain + ".invalid", valid + " " + self.foreign_origin,
                       valid + ":bad", valid + ":", valid + ":65536", valid + "\\" + self.domain):
            with self.subTest(origin=origin):
                result = self.login(self.domain, origin)
                self.assertEqual(result.status_code, 403)
                self.assertIn("origin_rejected", result.text)

    def test_duplicate_origin_headers_are_rejected(self):
        with TestClient(create_app(self.env), base_url="https://" + self.domain, follow_redirects=False) as client:
            token = csrf(client.get("/admin/login"))
            result = client.post("/admin/login", headers=[("origin", "https://" + self.domain), ("origin", self.foreign_origin)],
                                 data={"csrf": token, "username": "admin", "password": self.env["ADMIN_PASSWORD"]})
            self.assertEqual(result.status_code, 403)


class NginxHTTPSFixtures(unittest.TestCase):
    def setUp(self):
        self.nginx = os.environ.get("TEST_NGINX_BINARY") or shutil.which("nginx")
        self.openssl = os.environ.get("TEST_OPENSSL_BINARY") or shutil.which("openssl")
        if not self.nginx or not self.openssl:
            if os.environ.get("REQUIRE_ADMIN_PROXY_TEST") == "true":
                self.fail("Real Nginx and OpenSSL are required for the HTTPS integration suite.")
            self.skipTest("Real Nginx/OpenSSL not available; see TEST_NGINX_BINARY/TEST_OPENSSL_BINARY.")
        temporary = tempfile.TemporaryDirectory(prefix="admin-https-proxy-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.env = environment(self.directory)
        self.domain = secrets.token_hex(8) + ".example.test"
        self.foreign_domain = secrets.token_hex(8) + ".invalid"
        self.schemes = []
        self.start_backend()
        self.start_proxy()
        self.client = httpx.Client(base_url=self.origin, verify=ssl.create_default_context(cafile=str(self.certificate)),
                                   trust_env=False, follow_redirects=False, timeout=5)
        self.addCleanup(self.client.close)

    def start_backend(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        self.backend_port = listener.getsockname()[1]
        # Capture the actual production entry point's proxy settings, not an
        # idealized TestClient scheme. Only the test bind address is restricted.
        with patch.dict(os.environ, {**self.env, "AI_ADMIN_CONTAINER_PORT": str(self.backend_port)}), patch("uvicorn.run") as launch:
            runpy.run_module("platform_admin", run_name="__main__")
        args, options = launch.call_args
        self.assertTrue(options["proxy_headers"])
        self.assertEqual(options["forwarded_allow_ips"], "*")
        app = args[0]

        @app.middleware("http")
        async def observe(request, call_next):
            self.schemes.append((request.url.scheme, request.headers["host"], str(request.base_url)))
            return await call_next(request)

        options["host"] = "127.0.0.1"
        server = uvicorn.Server(uvicorn.Config(app, **options))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def stop():
            server.should_exit = True
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "Test Uvicorn did not stop.")

        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                self.fail("Test Uvicorn did not start.")
            time.sleep(0.02)

    @staticmethod
    def available_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def extra_nginx_servers(self):
        return ""

    def extra_nginx_locations(self):
        return ""

    def leading_nginx_servers(self):
        return ""

    def start_proxy(self):
        self.certificate = self.directory / "certificate.pem"
        key = self.directory / "certificate.key"
        subprocess.run([self.openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(self.certificate), "-subj", "/CN=" + self.domain,
                        "-addext", "subjectAltName=DNS:" + self.domain], check=True, capture_output=True)
        self.https_port = self.available_port()
        self.http_port = self.available_port()
        self.origin = "https://" + self.domain + ":" + str(self.https_port)
        values = {"DOMAIN": self.domain, "NGINX_LISTEN_PORT": str(self.http_port), "DOMAIN_HTTPS_PORT": str(self.https_port),
                  "HTTPS_PORT_SUFFIX": ":" + str(self.https_port),
                  "CERTBOT_FULLCHAIN_PATH": self.certificate.as_posix(), "CERTBOT_PRIVATE_KEY_PATH": key.as_posix(),
                  "WEBSITE_CERTBOT_WEBROOT": self.directory.as_posix(),
                  "NGINX_CLIENT_MAX_BODY_SIZE": "1m", "NGINX_UPSTREAM_HOST": "127.0.0.1",
                  "CHATWOOT_HTTP_PORT": str(self.backend_port), "NGINX_PROXY_READ_TIMEOUT_SECONDS": "5",
                  "ADMIN_UPSTREAM_HOST": "127.0.0.1", "AI_ADMIN_HTTP_PORT": str(self.backend_port)}
        site = (ROOT / getattr(self, "site_template", "templates/domain/chatwoot-https.conf.template")).read_text(encoding="utf-8")
        admin = (ROOT / "templates/nginx/admin.conf.template").read_text(encoding="utf-8")
        site = site.replace("# PLATFORM_ADMIN_LOCATION", admin + self.extra_nginx_locations())
        for name, value in values.items():
            site = site.replace("{{" + name + "}}", value)
        self.assertNotIn("{{", site)
        # Keep the real templates/directives; only restrict listeners to IPv4
        # loopback. Nothing touches system Nginx, hosts, DNS or certificates.
        site = re.sub(r"^\s*listen \[::\]:[^;]+;", "", site, flags=re.MULTILINE)
        site = re.sub(r"listen (\d+)", r"listen 127.0.0.1:\1", site)
        config = self.directory / "nginx.conf"
        config.write_text("daemon off;\nmaster_process off;\nerror_log stderr warn;\npid nginx.pid;\n"
                          "events { worker_connections 64; }\nhttp { access_log off;\n"
                          "client_body_temp_path temp/client; proxy_temp_path temp/proxy;\n"
                          "fastcgi_temp_path temp/fastcgi; uwsgi_temp_path temp/uwsgi; scgi_temp_path temp/scgi;\n"
                          + self.leading_nginx_servers() + site + self.extra_nginx_servers() + "\n}\n", encoding="utf-8")
        (self.directory / "logs").mkdir()
        (self.directory / "temp").mkdir()
        options = {"cwd": self.directory, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        command = [self.nginx, "-p", self.directory.as_posix() + "/", "-c", config.as_posix(), "-e", "stderr"]
        syntax = subprocess.run(command + ["-t"], cwd=self.directory, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr.decode(errors="replace"))
        process = subprocess.Popen(command, **options)

        def stop():
            process.terminate()
            process.wait(timeout=10)

        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail("Isolated test Nginx did not start.")
            try:
                with socket.create_connection(("127.0.0.1", self.https_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.02)

    def request(self, method, path, **options):
        resolve = socket.getaddrinfo

        def local_domain(host, *args, **kwargs):
            return resolve("127.0.0.1" if host == self.domain else host, *args, **kwargs)

        with patch("socket.getaddrinfo", side_effect=local_domain):
            return self.client.request(method, path, **options)

    def login(self, *, origin=None, headers=None, password=None):
        token = csrf(self.request("GET", "/admin/login"))
        return self.request("POST", "/admin/login", headers={"Origin": origin or self.origin, **(headers or {})},
                            data={"csrf": token, "username": "admin", "password": password or self.env["ADMIN_PASSWORD"]})

    def test_https_login_and_password_change_over_http_upstream(self):
        self.assertEqual(self.login().status_code, 303)
        cookie = self.client.cookies.get("platform_admin_session")
        response = self.request("GET", "/admin/password")
        replacement = secrets.token_urlsafe(24)
        result = self.request("POST", "/admin/password", headers={"Origin": self.origin},
                              data={"csrf": csrf(response), "current_password": self.env["ADMIN_PASSWORD"], "new_password": replacement})
        self.assertEqual(result.status_code, 303)
        self.assertIsNone(AuthStore(self.env).session(cookie))
        self.assertEqual(self.login(password=self.env["ADMIN_PASSWORD"]).status_code, 401)
        result = self.login(password=replacement)
        self.assertEqual(result.status_code, 303)
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/admin"):
            self.assertIn(flag, result.headers["set-cookie"])
        token = csrf(self.request("GET", "/admin"))
        self.assertEqual(self.request("POST", "/admin/logout", data={"csrf": token}, headers={"Origin": self.origin}).status_code, 303)
        self.assertEqual(self.request("GET", "/admin/api/services").status_code, 401)
        self.assertTrue(all(scheme == "https" and host == urlsplit(self.origin).netloc for scheme, host, _ in self.schemes))

    def test_foreign_origin_is_rejected_even_with_valid_session_and_csrf(self):
        result = self.login(origin="https://" + self.foreign_domain)
        self.assertEqual(result.status_code, 403)
        self.assertIn("origin_rejected", result.text)

    def test_http_redirect_uses_configured_https_port(self):
        result = self.request("GET", "http://" + self.domain + ":" + str(self.http_port) + "/admin/login?next=home")
        self.assertEqual(result.status_code, 301)
        self.assertEqual(result.headers["location"], self.origin + "/admin/login?next=home")

    def test_default_443_authority_is_valid_over_http_upstream(self):
        for authority in (self.domain, self.domain + ":443"):
            with self.subTest(authority=authority):
                self.assertEqual(self.login(origin="https://" + self.domain, headers={"Host": authority}).status_code, 303)

    def test_client_cannot_override_nginx_protocol_or_authority(self):
        result = self.login(headers={"X-Forwarded-Proto": "http", "X-Forwarded-Host": self.foreign_domain,
                                     "X-Forwarded-Port": "80", "Forwarded": "proto=http;host=" + self.foreign_domain})
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.schemes[-1], ("https", urlsplit(self.origin).netloc, self.origin + "/"))

    def test_csrf_and_session_validation_are_not_bypassed(self):
        csrf(self.request("GET", "/admin/login"))
        result = self.request("POST", "/admin/login", headers={"Origin": self.origin},
                              data={"username": "admin", "password": self.env["ADMIN_PASSWORD"]})
        self.assertEqual(result.status_code, 403)
        self.assertIn("csrf_rejected", result.text)
        result = self.request("POST", "/admin/login", headers={"Origin": "https://" + self.foreign_domain},
                              data={"username": "admin", "password": self.env["ADMIN_PASSWORD"]})
        self.assertIn("csrf_rejected", result.text)
        self.client.cookies.clear()
        self.assertEqual(self.request("POST", "/admin/api/password", headers={"Origin": self.origin}, json={}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
