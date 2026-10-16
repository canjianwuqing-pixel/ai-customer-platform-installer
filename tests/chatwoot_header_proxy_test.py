"""Actual Nginx header parsing, TLS/default-vhost selection and API forwarding.

Upstream is a tiny Chatwoot-protocol auth fixture, not a live Rails server.
Credentials/hosts/ports/certificates are temporary and never printed.
"""

import hmac
import http.client
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest

import admin_proxy_test as proxy


class ChatwootHeaderFixtures(unittest.TestCase):
    def start(self, *, http_only=False, legacy=False, server_only=False):
        driver = proxy.NginxHTTPSFixtures()
        self.addCleanup(lambda: self.assertTrue(driver.doCleanups()))
        token = secrets.token_urlsafe(32)
        observed = []

        class Chatwoot(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                supplied = self.headers.get("api_access_token", "")
                authenticated = hmac.compare_digest(supplied, token)
                record = {"authenticated": authenticated, "underscore_seen": bool(supplied),
                          "invalid_seen": "x.invalid-header" in self.headers,
                          "host": self.headers.get("Host"), "scheme": self.headers.get("X-Forwarded-Proto"),
                          "upgrade": self.headers.get("Upgrade"), "connection": self.headers.get("Connection")}
                observed.append(record)
                if authenticated and self.path == "/cable":
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "upgrade")
                    self.end_headers()
                    return
                body = json.dumps({"payload": [{"id": 1, "channel_type": "Channel::WebWidget"}], **record}).encode()
                self.send_response(200 if authenticated else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def backend():
            server = ThreadingHTTPServer(("127.0.0.1", 0), Chatwoot)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            driver.backend_port = server.server_port

            def stop():
                server.shutdown()
                server.server_close()
                thread.join(5)
                self.assertFalse(thread.is_alive())

            driver.addCleanup(stop)

        def default_servers():
            # A normal existing default vhost: it has no underscores directive,
            # and must inherit the http policy even if parsed before our site.
            return ("server_names_hash_bucket_size 128;\n"
                    f"server {{ listen 127.0.0.1:{driver.http_port} default_server; server_name _; return 418; }}\n"
                    f"server {{ listen 127.0.0.1:{driver.https_port} ssl default_server; server_name _;\n"
                    f'ssl_certificate "{driver.certificate.as_posix()}";\n'
                    f'ssl_certificate_key "{(driver.directory / "certificate.key").as_posix()}"; return 418; }}\n')

        driver.start_backend = backend
        driver.leading_nginx_servers = default_servers
        if http_only:
            driver.site_template = "templates/nginx/chatwoot.conf.template"
        if legacy or server_only:
            temporary = tempfile.TemporaryDirectory(prefix="chatwoot-header-migration-")
            driver.addCleanup(temporary.cleanup)
            directory = Path(temporary.name)
            template = (proxy.ROOT / getattr(driver, "site_template", "templates/domain/chatwoot-https.conf.template")).read_text(encoding="utf-8")
            before, after = directory / "legacy.conf", directory / "migrated.conf"
            before.write_text("\n".join(line for line in template.splitlines() if
                              ("underscores_in_headers" not in line if legacy else line != "underscores_in_headers on;")
                              and "# Managed Chatwoot API header policy" not in line) + "\n", encoding="utf-8")
            if legacy:
                bash = os.environ.get("TEST_BASH_BINARY") or ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash"))
                self.assertTrue(bash, "Bash is required to verify the actual production migration")
                migrated = subprocess.run([bash, "-c", 'source "$1/modules/nginx/configure.sh"; run_as_root() { "$@"; }; nginx_prepare_api_header_policy "$2" "$3"',
                                           "header-migration", proxy.ROOT.as_posix(), before.as_posix(), after.as_posix()], capture_output=True, timeout=20)
                self.assertEqual(migrated.returncode, 0, migrated.stderr.decode(errors="replace"))
                self.assertEqual(after.read_text(encoding="utf-8").count("underscores_in_headers on;"), 1)
            else:
                after = before
            driver.site_template = after
        driver.setUp()
        with closing(http.client.HTTPConnection("127.0.0.1", driver.backend_port, timeout=5)) as direct:
            direct.request("GET", "/api/v1/accounts/1/inboxes", headers={"api_access_token": token})
            response = direct.getresponse()
            self.assertEqual(response.status, 200, "Control: the same token must authenticate without Nginx")
            response.read()
        observed.clear()
        return driver, token, observed

    def raw(self, driver, token="", *, tls=False, sni="matched", token_first=False, extra=(), path="/api/v1/accounts/1/inboxes"):
        port = driver.https_port if tls else driver.http_port
        authority = driver.domain + ":" + str(port)
        headers = [("Host", authority)]
        if token:
            headers.insert(0 if token_first else 1, ("api_access_token", token))
        headers.extend(extra)
        headers.append(("Connection", "close" if path != "/cable" else "upgrade"))
        request = "GET " + path + " HTTP/1.1\r\n" + "".join(name + ": " + value + "\r\n" for name, value in headers) + "\r\n"
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            stream = connection
            if tls:
                context = ssl.create_default_context(cafile=str(driver.certificate))
                if sni != "matched":
                    # Keep CA verification; only permit deliberately absent or
                    # nonmatching SNI in this isolated vhost-selection test.
                    context.check_hostname = False
                stream = context.wrap_socket(connection, server_hostname=driver.domain if sni == "matched" else driver.foreign_domain if sni == "other" else None)
            try:
                stream.sendall(request.encode("ascii"))
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    chunk = stream.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                if path != "/cable":
                    while True:
                        chunk = stream.recv(4096)
                        if not chunk:
                            break
                        response.extend(chunk)
            finally:
                if tls:
                    stream.close()
        return int(response.split(b" ", 2)[1]), bytes(response)

    def test_http_api_token_survives_another_default_server_and_header_order(self):
        driver, token, observed = self.start(http_only=True)
        for first in (False, True):
            with self.subTest(token_before_host=first):
                status, _ = self.raw(driver, token, token_first=first)
                self.assertEqual(status, 200)
                self.assertTrue(observed[-1]["underscore_seen"])
                self.assertEqual(observed[-1]["host"], driver.domain)
                self.assertEqual(observed[-1]["scheme"], "http")

    def test_https_api_token_survives_sni_and_default_server_selection(self):
        driver, token, observed = self.start()
        for sni in ("matched", "other", "absent"):
            for first in (False, True):
                with self.subTest(sni=sni, token_before_host=first):
                    status, _ = self.raw(driver, token, tls=True, sni=sni, token_first=first)
                    self.assertEqual(status, 200)
                    self.assertTrue(observed[-1]["underscore_seen"])
                    self.assertEqual(observed[-1]["scheme"], "https")

    def test_https_redirect_keeps_location_and_does_not_forward_http_credentials(self):
        driver, token, observed = self.start()
        status, response = self.raw(driver, token, token_first=True)
        self.assertEqual(status, 301)
        self.assertIn(("Location: " + driver.origin + "/api/v1/accounts/1/inboxes").encode(), response)
        self.assertEqual(observed, [])

    def test_real_proxy_after_production_migration_of_legacy_http_and_https(self):
        for http_only in (True, False):
            with self.subTest(http_only=http_only):
                driver, token, _ = self.start(http_only=http_only, legacy=True)
                status, _ = self.raw(driver, token, tls=not http_only, sni="absent", token_first=True)
                self.assertEqual(status, 200)

    def test_server_only_fix_would_still_lose_token_before_host(self):
        driver, token, _ = self.start(http_only=True, server_only=True)
        self.assertEqual(self.raw(driver, token, token_first=False)[0], 200)
        self.assertEqual(self.raw(driver, token, token_first=True)[0], 401)

    def test_missing_wrong_and_hyphen_alias_tokens_still_fail_and_invalid_headers_stay_filtered(self):
        driver, token, observed = self.start()
        for supplied, extra in (("", ()), ("incorrect", ()), ("", (("api-access-token", token),))):
            status, _ = self.raw(driver, supplied, tls=True, extra=extra)
            self.assertEqual(status, 401)
        status, _ = self.raw(driver, token, tls=True, extra=(("x.invalid-header", "discard-me"),))
        self.assertEqual(status, 200)
        self.assertFalse(observed[-1]["invalid_seen"])

    def test_websocket_upgrade_host_and_scheme_remain_intact(self):
        driver, token, observed = self.start()
        status, _ = self.raw(driver, token, tls=True, path="/cable", extra=(("Upgrade", "websocket"),))
        self.assertEqual(status, 101)
        self.assertEqual(observed[-1]["upgrade"], "websocket")
        self.assertEqual(observed[-1]["connection"], "upgrade")
        self.assertEqual(observed[-1]["scheme"], "https")


if __name__ == "__main__":
    unittest.main()
