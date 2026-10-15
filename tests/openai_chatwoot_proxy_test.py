"""Real TLS Nginx webhook location + HTTP Runtime worker + protocol fixtures."""

import hashlib
import hmac
import json
import socket
import threading
import time
import unittest

import uvicorn

import admin_proxy_test as proxy
import openai_chatwoot_test as integration


class WebhookProxyFixtures(unittest.TestCase):
    def test_signed_https_webhook_reaches_runtime_but_management_is_not_public(self):
        fixture = integration.IntegrationFixtures()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepared()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(fixture.app, host="127.0.0.1", port=port, access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def stop():
            server.should_exit = True
            thread.join(10)
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not server.started:
            self.assertTrue(thread.is_alive() and time.monotonic() < deadline)
            time.sleep(.02)
        driver = proxy.NginxHTTPSFixtures()
        self.addCleanup(lambda: self.assertTrue(driver.doCleanups()))
        template = (proxy.ROOT / "templates/nginx/chatwoot-ai.conf.template").read_text(encoding="utf-8")
        driver.extra_nginx_locations = lambda: template.replace("{{AI_RUNTIME_UPSTREAM_HOST}}", "127.0.0.1").replace("{{AI_SERVICE_HTTP_PORT}}", str(port))
        driver.setUp()
        row = fixture.protocol.visitor(content="通过 HTTPS 代理发送")
        raw = json.dumps(fixture.protocol.webhook(row), ensure_ascii=False).encode()
        timestamp = str(int(time.time()))
        secret = fixture.protocol.bots[fixture.protocol.links[7]]["secret"]
        signature = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        path = "/integrations/chatwoot/service_a/webhook"
        headers = {"X-Chatwoot-Timestamp": timestamp, "X-Chatwoot-Signature": signature, "Content-Type": "application/json"}
        self.assertEqual(driver.request("POST", path, content=raw, headers=headers).status_code, 200)
        deadline = time.monotonic() + 10
        while not fixture.protocol.sent and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(len(fixture.protocol.sent), 1)
        self.assertIn("通过 HTTPS 代理发送", fixture.protocol.sent[0]["content"])
        self.assertEqual(driver.request("POST", path, content=raw, headers=headers).status_code, 200)
        self.assertEqual(driver.request("POST", path, content=raw).status_code, 401)
        self.assertEqual(driver.request("GET", path).status_code, 403)
        self.assertEqual(driver.request("GET", "/management/models/service_a/credential").status_code, 404)
        self.assertEqual(driver.request("POST", "/chat", json={"message": "not exposed"}).status_code, 404)
        time.sleep(.2)
        self.assertEqual(len(fixture.protocol.sent), 1)


if __name__ == "__main__":
    unittest.main()
