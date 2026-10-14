"""Management HTTP fixtures with real SQLite transactions and mock model calls."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

import httpx
import yaml

from ai_runtime.app import create_app
from ai_runtime.config import RuntimeFailure, ServiceResolver
from ai_runtime.engine import ChatProvider, RuntimeEngine
from ai_runtime.management_store import ManagementStore, SCHEMA_VERSION
from ai_runtime import management_migrate


class ManagementFixtures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="management-api-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.services = self.root / "config" / "services"
        self.domains = self.root / "config" / "domains"
        self.env = {
            "SERVICE_MANAGEMENT_TOKEN": "fixture-management-token-not-real-123456789",
            "INTERNAL_API_TOKEN": "fixture-chat-token-not-real-123456789",
            "OPENAI_API_KEY": "fixture-provider-key-not-real",
            "AI_API_BASE_URL": "https://provider.example.test/v1",
            "AI_MODEL": "legacy-model",
        }
        self.provider_calls = []
        self.write(self.services / "services.yaml", {"version": 1, "services": []})
        self.write(self.domains / "domains.yaml", {"version": 1, "domains": []})
        for model in ("model-a", "model-b"):
            self.write(self.services / "ai" / (model + ".yaml"), {"model": model})

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        path.chmod(0o600)

    def resolver(self):
        return ServiceResolver(self.services, self.root / "data", self.env)

    def store(self):
        return ManagementStore(self.resolver())

    @staticmethod
    def service(service_id="service_a", *, enabled=True):
        return {
            "id": service_id, "name": "Generic Service", "enabled": enabled,
            "ai_config": "model-a", "prompt_config": "", "knowledge_config": "", "channel_config": "",
        }

    @staticmethod
    def domain(domain="entry.example.test", service_id="service_a", *, default=False):
        return {"domain": domain, "service_id": service_id, "enabled": True, "default_entry": default}

    async def provider(self, request):
        data = json.loads(request.content)
        self.provider_calls.append(data)
        return httpx.Response(200, json={"choices": [{"message": {"content": "reply-" + data["model"]}}]})

    def client(self, token=None):
        engine = RuntimeEngine(self.resolver(), ChatProvider(httpx.MockTransport(self.provider)))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://management.test",
            headers={"Authorization": "Bearer " + (token or self.env["SERVICE_MANAGEMENT_TOKEN"])},
        )

    async def test_service_crud_and_enable_disable(self):
        async with self.client() as client:
            self.assertEqual((await client.get("/management/services")).json(), [])
            created = await client.post("/management/services", json=self.service(enabled=False))
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["id"], "service_a")
            self.assertEqual((await client.get("/management/services/service_a")).json(), created.json())
            self.assertEqual(len((await client.get("/management/services")).json()), 1)
            changed = await client.patch("/management/services/service_a", json={"name": "Renamed", "ai_config": "model-b"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["ai_config"], "model-b")
            self.assertTrue((await client.post("/management/services/service_a/enable")).json()["enabled"])
            self.assertFalse((await client.post("/management/services/service_a/disable")).json()["enabled"])
            replaced = await client.put("/management/services/service_a", json={"name": "Replacement"})
            self.assertEqual(replaced.status_code, 200)
            self.assertEqual(replaced.json()["ai_config"], "")
            self.assertEqual((await client.delete("/management/services/service_a")).status_code, 204)
            self.assertEqual((await client.get("/management/services/service_a")).status_code, 404)

    async def test_domain_crud_rebinding_and_enable_disable(self):
        async with self.client() as client:
            await client.post("/management/services", json=self.service())
            await client.post("/management/services", json=self.service("service_b"))
            created = await client.post("/management/domains", json=self.domain("Entry.Example.Test", default=True))
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["domain"], "entry.example.test")
            self.assertEqual(len((await client.get("/management/domains")).json()), 1)
            self.assertEqual((await client.get("/management/domains/entry.example.test")).json(), created.json())
            changed = await client.patch("/management/domains/entry.example.test", json={"service_id": "service_b"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["service_id"], "service_b")
            disabled = await client.post("/management/domains/entry.example.test/disable")
            self.assertFalse(disabled.json()["enabled"])
            self.assertFalse(disabled.json()["default_entry"])
            self.assertTrue((await client.post("/management/domains/entry.example.test/enable")).json()["enabled"])
            replaced = await client.put("/management/domains/entry.example.test", json={"service_id": "service_a"})
            self.assertEqual(replaced.status_code, 200)
            self.assertFalse(replaced.json()["enabled"])
            self.assertEqual((await client.delete("/management/domains/entry.example.test")).status_code, 204)
            self.assertEqual((await client.get("/management/domains")).json(), [])

    async def test_chat_token_has_no_management_permission(self):
        for token in ("wrong", self.env["INTERNAL_API_TOKEN"]):
            async with self.client(token) as client:
                for method, route in (("GET", "/management/services"), ("POST", "/management/services"), ("GET", "/management/domains"), ("DELETE", "/management/domains/entry.example.test")):
                    response = await client.request(method, route, json=self.service())
                    self.assertEqual(response.status_code, 401)
        self.assertFalse(self.store().path.exists())

    async def test_no_token_and_no_configured_management_token_fail_closed(self):
        async with self.client() as client:
            response = await client.get("/management/services", headers={"Authorization": ""})
            self.assertEqual(response.status_code, 401)
        self.env["SERVICE_MANAGEMENT_TOKEN"] = ""
        async with self.client("unused") as client:
            response = await client.get("/management/services")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"error": "management_auth_not_configured"})
        self.assertFalse(self.store().path.exists())

    async def test_management_token_does_not_authenticate_chat(self):
        async with self.client() as client:
            response = await client.post("/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.provider_calls, [])

    async def test_input_validation_and_no_plaintext_credential_fields(self):
        invalid_services = [
            {**self.service(), "id": "../invalid"},
            {**self.service(), "id": "UPPERCASE"},
            {**self.service(), "enabled": "true"},
            {**self.service(), "api_key": "sensitive-input"},
            {**self.service(), "ai_config": "../../private"},
            {**self.service(), "name": " "},
        ]
        with self.assertLogs("ai_runtime", level="WARNING") as logs:
            async with self.client() as client:
                for body in invalid_services:
                    response = await client.post("/management/services", json=body)
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn("sensitive-input", response.text)
                response = await client.get("/management/services/INVALID")
                self.assertEqual(response.status_code, 422)
                # Capture a managed failure too; request-validation errors do not log bodies.
                self.assertEqual((await client.get("/management/services/missing")).status_code, 404)
        self.assertNotIn("sensitive-input", "\n".join(logs.output))
        self.assertFalse(self.store().path.exists())

    async def test_invalid_domains_and_nonexistent_service_rejected(self):
        async with self.client() as client:
            for domain in ("https://entry.example.test", "*.example.test", "bad..example.test", "-bad.example.test", "127.0.0.1", "localhost"):
                response = await client.post("/management/domains", json=self.domain(domain))
                self.assertEqual(response.status_code, 422)
            response = await client.post("/management/domains", json=self.domain())
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), {"error": "invalid_service_binding"})
            self.assertEqual((await client.get("/management/domains")).json(), [])

    async def test_default_uniqueness_and_binding_invariants_are_transactional(self):
        async with self.client() as client:
            await client.post("/management/services", json=self.service())
            await client.post("/management/services", json=self.service("service_b", enabled=False))
            self.assertEqual((await client.post("/management/domains", json=self.domain(default=True))).status_code, 201)
            duplicate_default = await client.post("/management/domains", json=self.domain("other.example.test", default=True))
            self.assertEqual(duplicate_default.status_code, 409)
            response = await client.patch("/management/domains/entry.example.test", json={"service_id": "service_b"})
            self.assertEqual(response.status_code, 409)
            self.assertEqual((await client.post("/management/services/service_a/disable")).status_code, 409)
            self.assertEqual((await client.delete("/management/services/service_a")).status_code, 409)
            self.assertTrue((await client.get("/management/services/service_a")).json()["enabled"])
            self.assertEqual(len((await client.get("/management/domains")).json()), 1)
            self.assertEqual((await client.get("/management/domains/entry.example.test")).json()["service_id"], "service_a")

    async def test_conflicts_missing_records_and_immutable_keys(self):
        async with self.client() as client:
            await client.post("/management/services", json=self.service())
            self.assertEqual((await client.post("/management/services", json=self.service())).status_code, 409)
            self.assertEqual((await client.patch("/management/services/missing", json={"name": "Missing"})).status_code, 404)
            for patch in ({"id": "replacement"}, {"name": None}, {}):
                response = await client.patch("/management/services/service_a", json=patch)
                self.assertEqual(response.status_code, 422)
            await client.post("/management/domains", json=self.domain())
            self.assertEqual((await client.post("/management/domains", json=self.domain())).status_code, 409)
            self.assertEqual((await client.delete("/management/domains/missing.example.test")).status_code, 404)
            self.assertEqual((await client.patch("/management/domains/entry.example.test", json={"domain": "renamed.example.test"})).status_code, 422)

    async def test_yaml_compatibility_and_api_override_priority_without_rewriting_files(self):
        baseline = self.service()
        self.write(self.services / "services.yaml", {"version": 1, "services": [baseline]})
        self.write(self.services / "local.yaml", {"version": 1, "services": [{"id": "service_a", "name": "User Name"}]})
        self.write(self.domains / "domains.yaml", {"version": 1, "domains": [self.domain()]})
        self.write(self.domains / "local.yaml", {"version": 1, "domains": [{"domain": "entry.example.test", "enabled": False}]})
        paths = [self.services / "services.yaml", self.services / "local.yaml", self.domains / "domains.yaml", self.domains / "local.yaml"]
        before = {path: path.read_bytes() for path in paths}
        async with self.client() as client:
            self.assertEqual((await client.get("/management/services/service_a")).json()["name"], "User Name")
            self.assertFalse(self.store().path.exists())
            self.assertEqual((await client.patch("/management/services/service_a", json={"name": "API Name"})).status_code, 200)
            self.assertEqual((await client.post("/management/domains/entry.example.test/enable")).status_code, 200)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        async with self.client() as client:
            self.assertEqual((await client.get("/management/services/service_a")).json()["name"], "API Name")
            self.assertTrue((await client.get("/management/domains/entry.example.test")).json()["enabled"])

    async def test_deleted_yaml_records_do_not_reappear_and_can_be_recreated(self):
        self.write(self.services / "services.yaml", {"version": 1, "services": [self.service()]})
        self.write(self.domains / "domains.yaml", {"version": 1, "domains": [self.domain()]})
        async with self.client() as client:
            self.assertEqual((await client.delete("/management/domains/entry.example.test")).status_code, 204)
            self.assertEqual((await client.delete("/management/services/service_a")).status_code, 204)
        async with self.client() as client:
            self.assertEqual((await client.get("/management/domains")).json(), [])
            self.assertEqual((await client.get("/management/services")).json(), [])
            self.assertEqual((await client.post("/management/services", json=self.service())).status_code, 201)

    async def test_runtime_reads_api_changes_on_the_next_request_without_restart(self):
        chat_headers = {"Authorization": "Bearer " + self.env["INTERNAL_API_TOKEN"]}
        async with self.client() as client:
            self.assertEqual((await client.post("/management/services", json=self.service())).status_code, 201)
            first = await client.post("/chat", headers=chat_headers, json={"service_id": "service_a", "message": "hello"})
            self.assertEqual(first.json()["reply"], "reply-model-a")
            self.assertEqual((await client.patch("/management/services/service_a", json={"ai_config": "model-b"})).status_code, 200)
            second = await client.post("/chat", headers=chat_headers, json={"service_id": "service_a", "message": "hello"})
            self.assertEqual(second.json()["reply"], "reply-model-b")
            self.assertEqual((await client.post("/management/services/service_a/disable")).status_code, 200)
            blocked = await client.post("/chat", headers=chat_headers, json={"service_id": "service_a", "message": "hello"})
            self.assertEqual(blocked.status_code, 409)
        self.assertEqual(len(self.provider_calls), 2)

    async def test_invalid_runtime_reference_rejected_without_replacing_active_configuration(self):
        async with self.client() as client:
            self.assertEqual((await client.post("/management/services", json=self.service())).status_code, 201)
            failed = await client.patch("/management/services/service_a", json={"ai_config": "missing-profile"})
            self.assertEqual(failed.status_code, 422)
            self.assertEqual(failed.json(), {"error": "invalid_runtime_configuration"})
            self.assertEqual((await client.get("/management/services/service_a")).json()["ai_config"], "model-a")
            draft = {**self.service("draft", enabled=False), "ai_config": "missing-profile"}
            self.assertEqual((await client.post("/management/services", json=draft)).status_code, 201)
            self.assertEqual((await client.post("/management/services/draft/enable")).status_code, 422)
            self.assertFalse((await client.get("/management/services/draft")).json()["enabled"])

    async def test_parallel_writers_preserve_all_created_records_and_field_updates(self):
        async with self.client() as client:
            responses = await asyncio.gather(*(
                client.post("/management/services", json=self.service(f"service_{index}"))
                for index in range(10)
            ))
            self.assertTrue(all(response.status_code == 201 for response in responses))
            updates = await asyncio.gather(
                client.patch("/management/services/service_0", json={"name": "Concurrent Name"}),
                client.patch("/management/services/service_0", json={"ai_config": "model-b"}),
            )
            self.assertTrue(all(response.status_code == 200 for response in updates))
            result = (await client.get("/management/services/service_0")).json()
            self.assertEqual(result["name"], "Concurrent Name")
            self.assertEqual(result["ai_config"], "model-b")
            self.assertEqual(len((await client.get("/management/services")).json()), 10)

    def test_versioned_migration_and_repeatable_yaml_import_preserve_api_changes(self):
        self.write(self.services / "services.yaml", {"version": 1, "services": [self.service()]})
        self.write(self.domains / "domains.yaml", {"version": 1, "domains": [self.domain()]})
        store = self.store()
        result = store.migrate_yaml()
        self.assertEqual(result, {"services": 1, "domains": 1, "schema_version": SCHEMA_VERSION})
        with closing(sqlite3.connect(store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        store.mutate("service", "update", "service_a", {"name": "Keep API Value"})
        self.assertEqual(store.migrate_yaml(), result)
        self.assertEqual(store.read_state()[0]["service_a"]["name"], "Keep API Value")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.path.parent.stat().st_mode), 0o700)

    async def test_yaml_migration_endpoint_requires_authentication_and_preserves_api_overrides(self):
        self.write(self.services / "services.yaml", {"version": 1, "services": [self.service()]})
        async with self.client(self.env["INTERNAL_API_TOKEN"]) as client:
            response = await client.post("/management/migrations/import-yaml")
            self.assertEqual(response.status_code, 401)
        self.assertFalse(self.store().path.exists())
        async with self.client() as client:
            first = await client.post("/management/migrations/import-yaml")
            self.assertEqual(first.json(), {"services": 1, "domains": 0, "schema_version": SCHEMA_VERSION})
            await client.patch("/management/services/service_a", json={"name": "Preserved API Name"})
            self.assertEqual((await client.post("/management/migrations/import-yaml")).status_code, 200)
            self.assertEqual((await client.get("/management/services/service_a")).json()["name"], "Preserved API Name")

    def test_migration_cli_reports_only_counts_or_stable_error_codes(self):
        environment = {
            **self.env,
            "AI_RUNTIME_CONFIG_PATH": str(self.services),
            "AI_RUNTIME_DOMAINS_PATH": str(self.domains),
            "AI_SERVICE_DATA_PATH": str(self.root / "data"),
        }
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
            self.assertEqual(management_migrate.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {"services": 0, "domains": 0, "schema_version": SCHEMA_VERSION})
        with closing(sqlite3.connect(self.store().path)) as connection:
            connection.execute("PRAGMA user_version=999")
        error_output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), redirect_stderr(error_output):
            self.assertEqual(management_migrate.main(), 1)
        self.assertEqual(json.loads(error_output.getvalue()), {"error": "unsupported_management_schema"})

    async def test_unknown_future_schema_and_corrupt_database_fail_closed(self):
        store = self.store()
        store.migrate_yaml()
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute("PRAGMA user_version=999")
        async with self.client() as client:
            response = await client.get("/management/services")
            self.assertEqual(response.json(), {"error": "unsupported_management_schema"})
            response = await client.post("/management/services", json=self.service())
            self.assertEqual(response.status_code, 503)
        store.path.write_bytes(b"not a valid SQLite database")
        async with self.client() as client:
            response = await client.get("/management/services")
            self.assertEqual(response.json(), {"error": "management_storage_unavailable"})

    def test_failed_storage_write_rolls_back_without_changing_runtime_configuration(self):
        store = self.store()
        store.mutate("service", "create", "service_a", self.service())
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute("""CREATE TRIGGER fixture_failure BEFORE UPDATE ON management_records
                BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END""")
        with self.assertRaises(RuntimeFailure):
            store.mutate("service", "update", "service_a", {"ai_config": "model-b"})
        self.assertEqual(self.resolver().resolve("service_a").model, "model-a")

    def test_independent_process_style_connections_share_atomic_state(self):
        self.store().migrate_yaml()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda index: self.store().mutate("service", "create", f"worker_{index}", self.service(f"worker_{index}")),
                range(8),
            ))
        self.assertEqual(len(results), 8)
        self.assertEqual(len(self.store().read_state()[0]), 8)


if __name__ == "__main__":
    unittest.main()
