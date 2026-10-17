"""Execute tagged Ruby protocol producers, not hand-created lifecycle payloads.

The vendored MIT source is pinned in SOURCE.json. Set TEST_RUBY_BINARY to MRI
with Prism (Ruby >=3.3), REQUIRE_CHATWOOT_SOURCE_TEST=true to forbid skipping.
Rails persistence/HTTP I/O are doubles, NOT a live Chatwoot deployment claim.
"""

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import threading
import unittest

import httpx

import admin_proxy_test as proxy
import openai_chatwoot_test as integration
from ai_runtime.chatwoot import ChatwootBridge


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "tests/fixtures/chatwoot-v4.18.0"


class SourceProtocol(integration.ChatwootProtocol):
    def __init__(self, binary):
        super().__init__()
        self.process = subprocess.Popen([binary, str(ROOT / "tests/chatwoot_source_harness.rb")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", cwd=ROOT)
        self.lock = threading.Lock()
        self.output = queue.Queue()
        self.callbacks = deque()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            self.output.put(line)
        self.output.put(None)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(5)
        self.process.stdout.close()
        self.process.stderr.close()

    def command(self, op, **values):
        with self.lock:
            self.process.stdin.write(json.dumps({"op": op, "bots": self.bots, **values}, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            line = self.output.get(timeout=15)
            if not line:
                raise AssertionError("Upstream Ruby harness failed: " + self.process.stderr.read())
            result = json.loads(line)
            self.callbacks.extend(result["deliveries"])
            return result["response"]

    def visitor(self, conversation_id=100, inbox_id=7, content="你好", **_):
        return self.command("visitor", cid=conversation_id, inbox=inbox_id, content=content, bot=self.links[inbox_id])

    def human(self, conversation_id=100, content="人工答复"):
        return self.command("human", cid=conversation_id, content=content)

    def resolve(self, conversation_id=100):
        return self.command("resolve", cid=conversation_id)

    def __call__(self, request):
        prefix = "/api/v1/accounts/42/conversations/"
        if not request.url.path.startswith(prefix):
            return super().__call__(request)
        self.calls.append((request.method, request.url.path))
        token = request.headers.get("api_access_token")
        bot = next((row for row in self.bots.values() if row["access_token"]["token"] == token), None)
        if token != self.token and not bot or bot and request.method == "GET":
            return httpx.Response(401)
        parts = request.url.path[len(prefix):].split("/")
        body = json.loads(request.content) if request.content else {}
        result = self.command("api", cid=int(parts[0]), part=parts[1] if len(parts) > 1 else None,
            method=request.method, body=body, params=dict(request.url.params), is_bot=bool(bot),
            bot=bot["id"] if bot else None)
        if request.method == "POST" and parts[-1] == "messages":
            self.sent.append(result)
        return httpx.Response(200, json=result)


class SourceProtocolFixtures(unittest.TestCase):
    def setUp(self):
        binary = os.environ.get("TEST_RUBY_BINARY") or shutil.which("ruby")
        if not binary:
            if os.environ.get("REQUIRE_CHATWOOT_SOURCE_TEST") == "true":
                self.fail("MRI Ruby with Prism is required for real upstream protocol execution.")
            self.skipTest("Set TEST_RUBY_BINARY (MRI with Prism) to execute tagged Chatwoot code.")
        manifest = json.loads((SOURCES / "SOURCE.json").read_text(encoding="utf-8"))
        for file, checksum in manifest["sha256"].items():
            self.assertEqual(hashlib.sha256((SOURCES / file).read_bytes()).hexdigest(), checksum)
        self.f = integration.IntegrationFixtures()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        protocol = SourceProtocol(binary)
        self.addCleanup(protocol.close)
        self.f.protocol = self.protocol = protocol
        self.f.engine.provider.transport = httpx.MockTransport(protocol.provider)
        self.f.bridge.transport = httpx.MockTransport(protocol)
        self.f.prepared()

    def deliver(self, *, reverse=False):
        callbacks = list(self.protocol.callbacks)
        self.protocol.callbacks.clear()
        for callback in reversed(callbacks) if reverse else callbacks:
            response = self.f.api.post(httpx.URL(callback["url"]).path,
                content=callback["body"].encode(), headers=callback["headers"])
            self.assertEqual(response.status_code, 200, response.text)
        return callbacks

    def tick(self):
        self.f.tick()
        self.deliver()

    def test_actual_inbox_bot_events_after_handoff_resolve_and_same_id_reopen(self):
        first = self.protocol.visitor(content="正常咨询")
        first_delivery = self.deliver()
        self.tick()
        self.assertEqual(len(self.protocol.sent), 1)
        bot_id = self.f.bridge.binding("service_a")["bot_id"]
        before_credentials = dict(self.f.bridge.client(self.f.bridge.binding("service_a")).credentials)
        backup_url = "https://b-" + secrets.token_hex(4) + ".example.test"
        self.assertEqual(self.f.api.post("/management/website-origin", json={
            "old_url": "https://platform.example.test", "new_url": backup_url}).status_code, 200)
        self.assertEqual(self.f.bridge.binding("service_a")["bot_id"], bot_id)
        self.assertEqual(self.f.bridge.client(self.f.bridge.binding("service_a")).credentials, before_credentials)
        self.protocol.visitor(content="我要人工客服")
        self.assertTrue(all(row["url"].startswith(backup_url + "/") for row in self.protocol.callbacks))
        self.deliver()
        self.tick()
        self.protocol.human()
        human_events = self.deliver()
        self.assertIn("message_created", [json.loads(row["body"])["event"] for row in human_events])
        self.protocol.visitor(content="人工期间追问")
        self.deliver()
        self.tick()
        self.assertEqual(len(self.protocol.sent), 1)
        self.protocol.resolve()
        closing = self.deliver()
        self.assertIn("conversation_resolved", [json.loads(row["body"])["event"] for row in closing])
        self.assertTrue(self.f.bridge.round_state("service_a", 100)["awaiting_reopen"])
        # Recreate Runtime over the SAME persistent database, no reset.
        restarted = ChatwootBridge(self.f.engine, self.f.bridge.transport)
        self.assertTrue(restarted.round_state("service_a", 100)["awaiting_reopen"])
        self.f.bridge = restarted
        self.protocol.visitor(content="解决后重新咨询")
        pending = list(self.protocol.callbacks)
        transitions = [json.loads(row["body"]) for row in pending if json.loads(row["body"])["event"] == "conversation_status_changed"]
        self.assertEqual(transitions[0]["status"], "pending")
        self.assertIsNone(transitions[0]["changed_attributes"])
        updates = [json.loads(row["body"]) for row in pending if json.loads(row["body"])["event"] == "conversation_updated"]
        self.assertIn({"status": {"previous_value": "resolved", "current_value": "pending"}}, updates[0]["changed_attributes"])
        # Actual Message#reopen_resolved_conversation retains the human owner.
        self.assertEqual(transitions[0]["meta"]["assignee_type"], "User")
        self.deliver(reverse=True)  # New message can arrive before status jobs.
        self.tick()
        self.assertEqual(len(self.protocol.sent), 2)
        self.assertIn("解决后重新咨询", self.protocol.sent[-1]["content"])
        self.assertEqual(self.protocol.sent[-1]["conversation_id"], first["conversation_id"])
        context = self.protocol.provider_calls[-1][1]["messages"]
        self.assertNotIn("人工期间", str(context))
        for callback in first_delivery + closing + pending:
            response = self.f.api.post(httpx.URL(callback["url"]).path, content=callback["body"].encode(), headers=callback["headers"])
            self.assertEqual(response.status_code, 200)
        self.assertFalse(self.f.tick())
        self.assertEqual(len(self.protocol.sent), 2)
        self.protocol.visitor(200, content="另一位访客")
        self.deliver()
        self.tick()
        self.assertEqual(len(self.protocol.sent), 3)
        self.assertNotIn("解决后重新咨询", str(self.protocol.provider_calls[-1][1]))

    def test_upstream_signed_lifecycle_through_real_https_nginx(self):
        driver = proxy.NginxHTTPSFixtures()
        self.addCleanup(lambda: self.assertTrue(driver.doCleanups()))
        # Use Nginx's actual HTTP proxy with a disposable Runtime server, not
        # synthetic Origin/signature headers. Bodies/signatures come from Ruby.
        import socket
        import time
        import uvicorn
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        server = uvicorn.Server(uvicorn.Config(self.f.app, access_log=False, log_level="error"))
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
        location = (ROOT / "templates/nginx/chatwoot-ai.conf.template").read_text(encoding="utf-8")
        driver.extra_nginx_locations = lambda: location.replace("{{AI_RUNTIME_UPSTREAM_HOST}}", "127.0.0.1").replace("{{AI_SERVICE_HTTP_PORT}}", str(listener.getsockname()[1]))
        driver.setUp()

        def deliver():
            while self.protocol.callbacks:
                callback = self.protocol.callbacks.popleft()
                response = driver.request("POST", httpx.URL(callback["url"]).path,
                    content=callback["body"].encode(), headers=callback["headers"])
                self.assertEqual(response.status_code, 200)

        def wait_for(predicate):
            deadline = time.monotonic() + 15
            while not predicate() and time.monotonic() < deadline:
                deliver()
                time.sleep(.03)
            self.assertTrue(predicate())
            deliver()

        self.protocol.visitor(content="需要人工客服")
        deliver()
        wait_for(lambda: self.f.bridge.conversation_state("service_a", 100)[0])
        self.protocol.human()
        deliver()
        self.protocol.visitor(content="人工接待中")
        deliver()
        wait_for(lambda: all(status not in {"waiting", "working", "sending"} for status, _ in self.f.status()))
        self.assertEqual(self.protocol.sent, [])
        self.protocol.resolve()
        deliver()
        self.protocol.visitor(content="同一会话重新咨询")
        deliver()
        wait_for(lambda: len(self.protocol.sent) == 1)
        self.assertIn("同一会话重新咨询", self.protocol.sent[0]["content"])

    def test_actual_updated_event_recovers_missing_close_and_worker_delivery_order(self):
        self.protocol.visitor(content="我要人工客服")
        self.deliver()
        self.tick()
        self.protocol.resolve()
        self.protocol.callbacks.clear()  # Lose both close notifications.
        self.protocol.visitor(content="接管结束后的下一问")
        callbacks = list(self.protocol.callbacks)
        self.protocol.callbacks.clear()
        # The first message job can complete before status/update jobs arrive.
        first = [row for row in callbacks if json.loads(row["body"])["event"] == "message_created"]
        rest = [row for row in callbacks if json.loads(row["body"])["event"] != "message_created"]
        for group in (first, rest):
            self.protocol.callbacks.extend(group)
            self.deliver()
            self.tick()
        self.assertEqual(len(self.protocol.sent), 1)
        self.assertIn("接管结束后的下一问", self.protocol.sent[0]["content"])


if __name__ == "__main__":
    unittest.main()
