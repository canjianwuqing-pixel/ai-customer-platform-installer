"""Unified Admin fixtures: real auth/Runtime storage, no live infrastructure."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import yaml

from platform_admin.app import create_app
from platform_admin.auth import AdminError, AuthStore
from platform_admin.clients import InternalClient
from platform_admin.maintenance_worker import MaintenanceError, Runner
from platform_admin.maintenance_worker import make_handler
from ai_runtime.app import create_app as runtime_app
from ai_runtime.config import ServiceResolver
from ai_runtime.engine import RuntimeEngine
from ai_runtime.management_store import ManagementStore, SCHEMA_VERSION, MIGRATIONS
from modules.admin import readiness


class AdminFixtures(unittest.TestCase):
    password = "fixture-password-not-real-123"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="unified-admin-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": self.password,
                    "ADMIN_SESSION_SECRET": "fixture-session-secret-not-real-123456789",
                    "ADMIN_MAINTENANCE_TOKEN": "fixture-worker-token-not-real-123456789",
                    "SERVICE_MANAGEMENT_TOKEN": "fixture-management-token-not-real-123456789",
                    "ADMIN_DATA_PATH": str(self.root / "admin"), "ADMIN_SESSION_TTL_SECONDS": "3600"}
        config = self.root / "config" / "services"
        domains = self.root / "config" / "domains"
        config.mkdir(parents=True)
        domains.mkdir()
        (config / "services.yaml").write_text("version: 1\nservices: []\n", encoding="utf-8")
        (domains / "domains.yaml").write_text("version: 1\ndomains: []\n", encoding="utf-8")
        self.resolver = ServiceResolver(config, self.root / "data", {**self.env,
            "AI_API_BASE_URL": "https://provider.example.test/v1"})
        self.runtime = runtime_app(RuntimeEngine(self.resolver))
        self.management = InternalClient("http://runtime.test", self.env["SERVICE_MANAGEMENT_TOKEN"],
                                         httpx.ASGITransport(app=self.runtime))
        self.calls = []
        self.runner = Runner(self.root, self.root / ".env", self.root / "worker", self.execute)
        self.maintenance = InternalClient("http://worker.test", self.env["ADMIN_MAINTENANCE_TOKEN"], httpx.MockTransport(self.worker))
        self.app = create_app(self.env, self.management, self.maintenance)
        self.client = TestClient(self.app, base_url="https://platform.example.test", follow_redirects=False)
        self.addCleanup(self.client.close)

    def execute(self, args, **options):
        self.calls.append((args, options))
        return subprocess.CompletedProcess(args, 0, b"20261014T010203Z metadata\nSECRET-DO-NOT-RETURN\n", b"")

    def worker(self, request):
        self.assertEqual(request.headers["authorization"], "Bearer " + self.env["ADMIN_MAINTENANCE_TOKEN"])
        if request.url.path == "/state":
            return httpx.Response(200, json=self.runner.state())
        try:
            return httpx.Response(200, json=self.runner.submit(json.loads(request.content), background=False))
        except MaintenanceError:
            return httpx.Response(409, json={"error": "unsafe-worker-error-with-secret"})

    def csrf(self, path="/admin"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]

    def login(self, password=None):
        csrf = self.csrf("/admin/login")
        return self.client.post("/admin/login", data={"csrf": csrf, "username": "admin", "password": password or self.password})

    def post(self, path, data, method="POST"):
        return self.client.request(method, path, json=data, headers={"x-csrf-token": self.csrf()})

    def create_service(self, service_id="service_a", enabled=False):
        return self.post("/admin/api/services", {"id": service_id, "name": "Generic service", "enabled": enabled,
            "ai_config": "model_a" if enabled else "", "prompt_config": "", "knowledge_config": "", "channel_config": ""})

    def test_login_rotation_cookie_security_and_logout_revocation(self):
        csrf = self.csrf("/admin/login")
        old = self.client.cookies.get("platform_admin_session")
        result = self.client.post("/admin/login", data={"csrf": csrf, "username": "admin", "password": self.password})
        self.assertEqual(result.status_code, 303)
        self.assertNotEqual(old, self.client.cookies.get("platform_admin_session"))
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/admin"):
            self.assertIn(flag, result.headers["set-cookie"])
        self.assertIsNone(self.app.state.auth.session(old))
        active = self.client.cookies.get("platform_admin_session")
        self.assertEqual(self.post("/admin/logout", {}).status_code, 303)
        self.assertIsNone(self.app.state.auth.session(active))
        self.assertEqual(self.client.get("/admin/api/services").status_code, 401)

    def test_anonymous_invalid_credentials_csrf_and_login_throttle(self):
        self.assertEqual(self.client.get("/admin").status_code, 303)
        self.assertEqual(self.client.get("/admin/api/services").status_code, 401)
        for _ in range(5):
            self.assertEqual(self.login("wrong").status_code, 401)
        self.assertEqual(self.login().status_code, 429)

    def test_csrf_required_for_login_all_writes_and_logout(self):
        self.csrf("/admin/login")
        self.assertEqual(self.client.post("/admin/login", data={"username": "admin", "password": self.password}).status_code, 403)
        self.login()
        for path in ("/admin/api/services", "/admin/system/run", "/admin/logout", "/admin/api/password"):
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(self.client.post("/admin/api/services", json={}, headers={"x-csrf-token": self.csrf(), "origin": "https://attacker.test"}).status_code, 403)

    def test_native_login_form_uses_same_origin_referrer_policy(self):
        response = self.client.get("/admin/login")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["referrer-policy"], "same-origin")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("form-action 'self'", response.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertIn('<form method="post" action="/admin/login">', response.text)

    def test_session_expiry_and_password_change_persist_across_restart(self):
        self.login()
        old_token = self.client.cookies.get("platform_admin_session")
        replacement = "replacement-password-not-real-1234"
        self.assertEqual(self.post("/admin/api/password", {"current_password": "bad", "new_password": replacement}).status_code, 401)
        self.assertEqual(self.post("/admin/api/password", {"current_password": self.password, "new_password": replacement}).status_code, 200)
        self.assertIsNone(self.app.state.auth.session(old_token))
        fresh = AuthStore(self.env)
        with self.assertRaises(AdminError):
            fresh.login("admin", self.password)
        fresh.login("admin", replacement)
        token, _ = fresh.create_session(True)
        with patch("platform_admin.auth.time.time", return_value=time.time() + 3601):
            self.assertIsNone(fresh.session(token))
        raw = (self.root / "admin" / "auth.sqlite3").read_bytes()
        self.assertNotIn(self.password.encode(), raw)
        self.assertIn(replacement.encode(), raw)
        with closing(sqlite3.connect(self.root / "admin" / "auth.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT value FROM admin_settings WHERE name='ADMIN_PASSWORD'").fetchone()[0], replacement)

    def test_service_page_create_edit_enable_disable_and_validation(self):
        self.login()
        self.assertEqual(self.post("/admin/api/ai", {"id": "model_a", "model": "model-alpha"}).status_code, 201)
        self.assertEqual(self.create_service(enabled=True).status_code, 201)
        self.assertEqual(self.post("/admin/api/services/service_a", {"name": "Renamed", "enabled": False}, "PATCH").status_code, 200)
        page = self.client.get("/admin/manage/services")
        self.assertIn("Renamed", page.text)
        self.assertEqual(self.post("/admin/api/services/service_a", {"enabled": True}, "PATCH").status_code, 200)
        self.assertEqual(self.resolver.resolve("service_a").model, "model-alpha")
        self.assertEqual(self.create_service("../escape").status_code, 409)
        self.assertEqual(self.post("/admin/api/services/service_a", {"api_key": "not-allowed"}, "PATCH").status_code, 409)
        edit = self.client.get("/admin/edit/services/service_a")
        self.assertIn("service_a", edit.text)
        data = self.client.get("/admin/api/services/service_a").json()
        data["name"] = "Saved via form"
        result = self.client.post("/admin/edit/services/service_a", data={"csrf": self.csrf(), "record": json.dumps(data)})
        self.assertEqual(result.status_code, 303)
        self.assertIn("Saved via form", self.client.get("/admin/manage/services").text)
        result = self.client.post("/admin/toggle/services/service_a", data={"csrf": self.csrf(), "enabled": "false"})
        self.assertEqual(result.status_code, 303)
        self.assertFalse(self.client.get("/admin/api/services/service_a").json()["enabled"])

    def test_domain_page_create_edit_disable_and_invalid_binding(self):
        self.login()
        self.create_service()
        data = {"domain": "entry.example.test", "service_id": "service_a", "enabled": False, "default_entry": False}
        self.assertEqual(self.post("/admin/api/domains", data).status_code, 201)
        self.assertIn("entry.example.test", self.client.get("/admin/manage/domains").text)
        self.assertEqual(self.post("/admin/api/domains/entry.example.test", {"service_id": "missing"}, "PATCH").status_code, 409)
        self.create_service("service_b")
        self.assertEqual(self.post("/admin/api/domains/entry.example.test", {"service_id": "service_b"}, "PATCH").status_code, 200)
        self.assertEqual(self.client.get("/admin/api/domains/entry.example.test").json()["service_id"], "service_b")
        self.assertEqual(self.post("/admin/api/domains/entry.example.test", {}, "DELETE").status_code, 200)

    def test_profiles_runtime_read_and_legacy_yaml_preserved(self):
        self.login()
        original = (self.resolver.config_dir / "services.yaml").read_bytes()
        for kind, record in (("ai", {"id": "model_a", "model": "alpha"}),
            ("prompts", {"id": "prompt_a", "system_prompt": "Reply briefly.", "reply_rules": ["Be accurate."]}),
            ("knowledge", {"id": "knowledge_a", "source": "", "max_characters": 5000}),
            ("channels", {"id": "channel_a", "enabled": False, "provider": "", "config_ref": ""})):
            self.assertEqual(self.post("/admin/api/" + kind, record).status_code, 201)
            self.assertEqual(self.client.get("/admin/manage/" + kind).status_code, 200)
        self.create_service(enabled=True)
        self.post("/admin/api/services/service_a", {"prompt_config": "prompt_a"}, "PATCH")
        self.assertEqual(self.post("/admin/api/runtime/default", {"default_service_id": "service_a"}, "PUT").status_code, 200)
        self.assertEqual(self.resolver.resolve().system_prompt, "Reply briefly.")
        self.assertEqual(self.post("/admin/api/ai/model_a", {"model": "beta"}, "PUT").status_code, 200)
        self.assertEqual(self.resolver.resolve().model, "beta")
        self.assertEqual(self.post("/admin/api/services/service_a", {"enabled": False}, "PATCH").status_code, 409)
        self.assertEqual(self.post("/admin/api/ai/model_a", {"model": "beta", "api_key_env": "SERVICE_MANAGEMENT_TOKEN"}, "PUT").status_code, 409)
        self.assertEqual(self.post("/admin/api/ai/model_a", {"model": "beta", "api_key": "forbidden"}, "PUT").status_code, 409)
        self.assertEqual((self.resolver.config_dir / "services.yaml").read_bytes(), original)

    def test_backup_and_upgrade_call_existing_scripts_without_shell(self):
        self.login()
        for operation in ("backup", "upgrade", "restore", "rollback"):
            version = "20261014T010203Z" if operation in {"restore", "rollback"} else ""
            result = self.post("/admin/system/run", {"operation": operation, "version": version, "confirmation": operation.upper()})
            self.assertEqual(result.status_code, 202)
            args, options = self.calls[-1]
            self.assertEqual(args[:2], ["/bin/bash", str(self.root / (operation + ".sh"))])
            self.assertFalse(options["shell"])
            self.assertEqual(options["stdout"], subprocess.DEVNULL)
            self.assertNotIn("SERVICE_MANAGEMENT_TOKEN", options["env"])
        self.assertIn("succeeded", self.client.get("/admin/maintenance").text)
        state = self.client.get("/admin/system/state").json()
        self.assertEqual(state["backups"], ["20261014T010203Z"])
        self.assertNotIn("SECRET-DO-NOT-RETURN", json.dumps(state))

    def test_maintenance_confirmation_injection_lock_and_failure(self):
        self.login()
        for data in ({"operation": "upgrade", "version": "", "confirmation": ""},
                     {"operation": "restore", "version": "../../.env;whoami", "confirmation": "RESTORE"},
                     {"operation": "shell", "version": "", "confirmation": "SHELL"}):
            self.assertEqual(self.post("/admin/system/run", data).status_code, 409)
        self.assertFalse(self.calls)
        self.runner.lock.acquire()
        with self.assertRaises(MaintenanceError):
            self.runner.submit({"operation": "backup", "version": "", "confirmation": "BACKUP"})
        self.runner.lock.release()
        self.runner.execute = lambda *a, **kw: subprocess.CompletedProcess(a, 1)
        self.runner.submit({"operation": "upgrade", "version": "", "confirmation": "UPGRADE"}, background=False)
        self.assertEqual(self.runner.state()["jobs"][0]["status"], "failed")

    def test_tokens_never_in_pages_api_errors_cookies_or_maintenance_state(self):
        self.login()
        responses = [self.client.get(path) for path in ("/admin", "/admin/manage/services", "/admin/edit/services/_new", "/admin/maintenance", "/admin/system/state")]
        self.create_service()
        responses.append(self.post("/admin/api/services/service_a", {"name": self.env["SERVICE_MANAGEMENT_TOKEN"]}, "PATCH"))
        responses.append(self.post("/admin/system/run", {"operation": "shell", "version": "", "confirmation": "SHELL"}))
        combined = "".join(response.text + str(response.headers) for response in responses)
        for key in ("SERVICE_MANAGEMENT_TOKEN", "ADMIN_MAINTENANCE_TOKEN", "ADMIN_SESSION_SECRET", "ADMIN_PASSWORD"):
            self.assertNotIn(self.env[key], combined)
        self.assertNotIn(self.password, combined)

    def test_html_is_escaped_and_oversized_or_duplicate_form_rejected(self):
        self.login()
        self.create_service()
        self.post("/admin/api/services/service_a", {"name": "<script>alert(1)</script>"}, "PATCH")
        page = self.client.get("/admin/manage/services")
        self.assertNotIn("<script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.assertEqual(self.client.post("/admin/api/services", content="x" * 65537).status_code, 413)
        self.assertEqual(self.client.post("/admin/logout", content="csrf=x&csrf=y").status_code, 422)
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])

    def test_v1_database_migrates_and_bad_profile_edit_rolls_back(self):
        store = ManagementStore(self.resolver)
        store._check_path(True)
        with closing(sqlite3.connect(store.path)) as db:
            # Build a genuine v1 database. Downgrading only user_version on a
            # current schema would leave newer tables behind and is not a
            # supported historical deployment or a valid migration fixture.
            for sql in MIGRATIONS[1]:
                db.execute(sql)
            db.execute("PRAGMA user_version=1")
            db.commit()
        self.login()
        self.assertEqual(self.post("/admin/api/ai", {"id": "model_a", "model": "alpha"}).status_code, 201)
        self.create_service(enabled=True)
        self.assertEqual(self.post("/admin/api/ai/model_a", {"model": "beta", "api_base_url": "file:///etc/passwd"}, "PUT").status_code, 409)
        self.assertEqual(self.resolver.resolve("service_a").model, "alpha")
        with closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_worker_http_authentication_and_token_rotation(self):
        from http.server import ThreadingHTTPServer
        token = [self.env["ADMIN_MAINTENANCE_TOKEN"]]
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.runner, lambda: token[0]))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url="http://127.0.0.1:" + str(server.server_port), trust_env=False) as client:
                self.assertEqual(client.get("/state").status_code, 401)
                self.assertEqual(client.get("/state", headers={"authorization": "Bearer " + token[0]}).status_code, 200)
                old = token[0]
                token[0] = "rotated-fixture-token-not-real-1234567890"
                self.assertEqual(client.get("/state", headers={"authorization": "Bearer " + old}).status_code, 401)
                self.assertEqual(client.post("/jobs", headers={"authorization": "Bearer " + token[0]}, json={"operation": "shell"}).status_code, 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_default_admin_login_and_six_character_password_change(self):
        self.client.close()
        default_env = {**self.env, "ADMIN_USERNAME": "retired-name", "ADMIN_PASSWORD": "admin"}
        default_env.pop("ADMIN_PASSWORD")
        self.app = create_app(default_env, self.management, self.maintenance)
        self.client = TestClient(self.app, base_url="https://platform.example.test", follow_redirects=False)
        self.addCleanup(self.client.close)
        self.assertEqual(self.login("admin").status_code, 303)
        for replacement in ("", "12345", "admin"):
            result = self.post("/admin/api/password", {"current_password": "admin", "new_password": replacement})
            self.assertEqual(result.status_code, 422)
            self.assertEqual(result.json()["error"], "password_must_be_6_to_1024_characters")
        self.assertEqual(self.post("/admin/api/password", {"current_password": "admin", "new_password": "123456"}).status_code, 200)
        fresh = AuthStore(default_env)
        fresh.login("admin", "123456")
        with self.assertRaises(AdminError):
            fresh.login("admin", "admin")
        with self.assertRaises(AdminError):
            fresh.login("retired-name", "123456")
        self.assertEqual(self.login("123456").status_code, 303)

    def test_readiness_checks_management_authentication_and_basic_interfaces(self):
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})
        original = self.management.token
        self.management.token = "incorrect-fixture-token-not-real-123456789"
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready"})
        self.assertNotIn(original, response.text)

    def test_readiness_rejects_unauthenticated_or_incompatible_management(self):
        for response in (httpx.Response(200, json=[]), httpx.Response(200, json={"status": "ok"}), httpx.Response(404)):
            client = InternalClient("http://runtime.test", self.env["SERVICE_MANAGEMENT_TOKEN"],
                                    httpx.MockTransport(lambda _request: response))
            app = create_app(self.env, client, self.maintenance)
            with TestClient(app) as browser:
                self.assertEqual(browser.get("/ready").status_code, 503)

    def test_readiness_fails_when_maintenance_authentication_fails(self):
        maintenance = InternalClient("http://worker.test", self.env["ADMIN_MAINTENANCE_TOKEN"],
                                     httpx.MockTransport(lambda _request: httpx.Response(401)))
        app = create_app(self.env, self.management, maintenance)
        with TestClient(app) as browser:
            self.assertEqual(browser.get("/ready").status_code, 503)

    def test_portable_probe_supports_legacy_admin_without_ready_endpoint(self):
        calls = []
        def request(url, token=""):
            calls.append((url, bool(token)))
            if url.endswith("/health"):
                return {"status": "ok"}
            if token:
                return []
            raise urllib.error.HTTPError(url, 401, "unauthorized", {}, None)
        env = {**self.env, "AI_ADMIN_CONTAINER_PORT": "8080", "ADMIN_API_URL": "http://runtime.test"}
        with patch.object(readiness, "request_json", side_effect=request), patch.object(readiness, "maintenance_state", return_value={"jobs": []}):
            readiness.check(env)
        self.assertTrue(calls)
        self.assertFalse(any(url.endswith("/ready") for url, _ in calls))

    def test_portable_probe_rejects_incompatible_or_unauthenticated_api(self):
        env = {**self.env, "AI_ADMIN_CONTAINER_PORT": "8080", "ADMIN_API_URL": "http://runtime.test"}
        def request(url, token=""):
            return {"status": "ok"} if url.endswith("/health") else []
        with patch.object(readiness, "request_json", side_effect=request):
            with self.assertRaises(ValueError):
                readiness.check(env)

    def test_portable_probe_rejects_invalid_maintenance_state(self):
        env = {**self.env, "AI_ADMIN_CONTAINER_PORT": "8080", "ADMIN_API_URL": "http://runtime.test"}
        def request(url, token=""):
            if url.endswith("/health"):
                return {"status": "ok"}
            if token:
                return []
            raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)
        with patch.object(readiness, "request_json", side_effect=request), patch.object(readiness, "maintenance_state", return_value={}):
            with self.assertRaises(ValueError):
                readiness.check(env)

    def test_password_has_no_complexity_rule_and_persists_after_app_restart(self):
        self.login()
        replacement = "密码 空 格!"
        self.assertEqual(self.post("/admin/api/password", {"current_password": self.password, "new_password": replacement}).status_code, 200)
        self.client.close()
        self.app = create_app(self.env, self.management, self.maintenance)
        self.client = TestClient(self.app, base_url="https://platform.example.test", follow_redirects=False)
        self.addCleanup(self.client.close)
        self.assertEqual(self.login(self.password).status_code, 401)
        self.assertEqual(self.login(replacement).status_code, 303)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertIn('minlength="6"', self.client.get("/admin/password").text)

    @staticmethod
    def legacy_hash(password):
        # Simulate the prior format; production only retains its verifier.
        salt = "a1" * 16
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
        return "pbkdf2_sha256:600000:" + salt + ":" + digest

    def test_existing_hash_environment_migrates_only_after_correct_login(self):
        legacy = self.legacy_hash(self.password)
        legacy_env = {**self.env, "ADMIN_PASSWORD": "admin", "ADMIN_PASSWORD_HASH": legacy}
        auth = AuthStore(legacy_env)
        with self.assertRaises(AdminError):
            auth.login("admin", "admin")
        with auth.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM admin_settings").fetchone())
        auth.login("admin", self.password)
        with auth.db() as db:
            self.assertEqual(db.execute("SELECT value FROM admin_settings WHERE name='ADMIN_PASSWORD'").fetchone()[0], self.password)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='account'").fetchone())
        AuthStore({**legacy_env, "ADMIN_PASSWORD_HASH": ""}).login("admin", self.password)

    def test_existing_database_password_wins_over_stale_environment_hash(self):
        path = self.root / "admin" / "auth.sqlite3"
        path.parent.mkdir(mode=0o700)
        actual = "existing-current-password-not-real"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE account (id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT NOT NULL)")
            db.execute("INSERT INTO account VALUES (1, ?)", (self.legacy_hash(actual),))
            db.commit()
        auth = AuthStore({**self.env, "ADMIN_PASSWORD": "admin", "ADMIN_PASSWORD_HASH": self.legacy_hash(self.password)})
        for wrong in ("admin", self.password):
            with self.assertRaises(AdminError):
                auth.login("admin", wrong)
        auth.login("admin", actual)
        auth.change_password(actual, "abcdef")
        AuthStore({**self.env, "ADMIN_PASSWORD": "admin"}).login("admin", "abcdef")

    def test_legacy_session_can_change_password_without_resetting_account(self):
        auth = AuthStore({**self.env, "ADMIN_PASSWORD_HASH": self.legacy_hash(self.password)})
        token, _ = auth.create_session(True)
        auth.change_password(self.password, "      ")
        self.assertIsNone(auth.session(token))
        AuthStore(self.env).login("admin", "      ")

    def test_bad_legacy_hash_fails_closed_instead_of_installing_default(self):
        auth = AuthStore({**self.env, "ADMIN_PASSWORD_HASH": "invalid-hash", "ADMIN_PASSWORD": "admin"})
        with self.assertRaises(AdminError):
            auth.login("admin", "admin")

    def test_failed_legacy_migration_preserves_the_existing_account(self):
        auth = AuthStore({**self.env, "ADMIN_PASSWORD_HASH": self.legacy_hash(self.password)})
        with auth.db() as db:
            db.execute("CREATE TRIGGER fixture_block BEFORE INSERT ON admin_settings BEGIN SELECT RAISE(ABORT, 'fixture_failure'); END")
        with self.assertRaises(sqlite3.Error):
            auth.login("admin", self.password)
        with auth.db() as db:
            self.assertTrue(db.execute("SELECT hash FROM account WHERE id=1").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM admin_settings").fetchone())
            db.execute("DROP TRIGGER fixture_block")
        auth.login("admin", self.password)

    def test_passwords_are_not_exposed_by_management_or_password_responses(self):
        self.login()
        replacement = "private-password-after-change-123"
        response = self.post("/admin/api/password", {"current_password": self.password, "new_password": replacement})
        self.assertNotIn(replacement, response.text)
        self.assertEqual(self.login(replacement).status_code, 303)
        response = self.post("/admin/api/services", {"id": "service_a", "name": replacement, "enabled": False})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(replacement, response.text)


if __name__ == "__main__":
    unittest.main()
