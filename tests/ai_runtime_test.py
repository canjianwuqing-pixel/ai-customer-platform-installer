"""Offline request-to-provider fixtures; no real key or paid API is used."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

import httpx
import yaml

from ai_runtime.app import create_app
from ai_runtime.config import RuntimeFailure, ServiceResolver
from ai_runtime.engine import ChatProvider, RuntimeEngine


class RuntimeFixtures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-runtime-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.env = {
            "INTERNAL_API_TOKEN": "fixture-internal-token",
            "KEY_A": "fixture-provider-key-a",
            "KEY_B": "fixture-provider-key-b",
            "OPENAI_API_KEY": "fixture-legacy-key",
            "AI_API_BASE_URL": "https://provider.example.test/v1",
            "AI_MODEL": "legacy-model",
            "AI_SYSTEM_PROMPT": "legacy-prompt",
        }
        self.requests = []
        self.write(self.config / "services.yaml", {"version": 1, "services": []})

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
        path.chmod(0o600)

    def record(self, key, *, enabled=True):
        return {
            "id": "service_" + key,
            "name": "Service " + key,
            "enabled": enabled,
            "ai_config": "ai-" + key,
            "prompt_config": "prompt-" + key,
            "knowledge_config": "knowledge-" + key,
            "channel_config": "",
        }

    def configure(self, *keys):
        self.write(self.config / "local.yaml", {
            "version": 1, "services": [self.record(key) for key in keys],
        })
        for key in keys:
            self.write(self.config / "ai" / f"ai-{key}.yaml", {
                "model": "model-" + key,
                "api_base_url": f"https://provider-{key}.example.test/v1",
                "api_key_env": "KEY_" + key.upper(),
            })
            self.write(self.config / "prompts" / f"prompt-{key}.yaml", {
                "system_prompt": "prompt-" + key,
                "reply_rules": ["rule-" + key],
            })
            self.write(self.config / "knowledge" / f"knowledge-{key}.yaml", {
                "source": "context.txt", "max_characters": 200,
            })
            knowledge_file = self.data / "services" / ("service_" + key) / "context.txt"
            knowledge_file.parent.mkdir(parents=True, exist_ok=True)
            knowledge_file.write_text("private-knowledge-" + key, encoding="utf-8")

    async def provider(self, request):
        await asyncio.sleep(0)
        body = json.loads(request.content)
        self.requests.append((str(request.url), request.headers["authorization"], body))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "reply-from-" + body["model"]}}],
        })

    def resolver(self):
        return ServiceResolver(self.config, self.data, self.env)

    def client(self, handler=None):
        engine = RuntimeEngine(
            self.resolver(), ChatProvider(httpx.MockTransport(handler or self.provider))
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(engine)),
            base_url="http://runtime.test",
            headers={"Authorization": "Bearer fixture-internal-token"},
        )

    async def test_service_id_routes_to_model_prompt_rules_and_knowledge(self):
        self.configure("a", "b")
        async with self.client() as client:
            response = await client.post("/chat", json={"service_id": "service_a", "message": "message-a"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"service_id": "service_a", "reply": "reply-from-model-a"})
        url, credential, body = self.requests[0]
        self.assertEqual(url, "https://provider-a.example.test/v1/chat/completions")
        self.assertEqual(credential, "Bearer fixture-provider-key-a")
        self.assertEqual(body["messages"][0]["content"], "prompt-a\nrule-a")
        self.assertIn("private-knowledge-a", body["messages"][1]["content"])
        self.assertEqual(body["messages"][-1]["content"], "message-a")

    async def test_parallel_instances_never_share_context_or_credentials(self):
        self.configure("a", "b")
        async with self.client() as client:
            responses = await asyncio.gather(*(
                client.post("/chat", json={"service_id": "service_" + key, "message": "message-" + key})
                for key in ["a", "b"] * 10
            ))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(self.requests), 20)
        for url, credential, body in self.requests:
            key = body["model"][-1]
            other = "b" if key == "a" else "a"
            self.assertIn("provider-" + key, url)
            self.assertEqual(credential, "Bearer fixture-provider-key-" + key)
            serialized = json.dumps(body)
            for prefix in ("prompt-", "rule-", "private-knowledge-", "message-"):
                self.assertIn(prefix + key, serialized)
                self.assertNotIn(prefix + other, serialized)

    async def test_configured_default_instance(self):
        self.configure("a", "b")
        self.env["AI_DEFAULT_SERVICE_ID"] = "service_b"
        async with self.client() as client:
            response = await client.post("/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service_id"], "service_b")

    async def test_single_enabled_instance_becomes_default(self):
        self.configure("a")
        async with self.client() as client:
            response = await client.post("/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service_id"], "service_a")

    async def test_empty_registry_preserves_single_instance_environment_mode(self):
        async with self.client() as client:
            response = await client.post("/chat", json={"message": "legacy-message"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service_id"], "default")
        self.assertEqual(self.requests[0][2]["model"], "legacy-model")
        self.assertEqual(self.requests[0][2]["messages"][0]["content"], "legacy-prompt")

    async def test_ambiguous_default_does_not_select_an_arbitrary_instance(self):
        self.configure("a", "b")
        async with self.client() as client:
            response = await client.post("/chat", json={"message": "hello"})
        self.assertEqual(response.json(), {"error": "default_service_not_configured"})
        self.assertEqual(self.requests, [])

    async def test_invalid_or_unknown_id_never_falls_back(self):
        self.configure("a")
        async with self.client() as client:
            for value, status in [("missing", 404), ("../service_a", 422), ("", 422)]:
                response = await client.post("/chat", json={"service_id": value, "message": "hello"})
                self.assertEqual(response.status_code, status)
        self.assertEqual(self.requests, [])

    async def test_disabled_instance_rejected(self):
        self.configure("a")
        self.write(self.config / "local.yaml", {"version": 1, "services": [self.record("a", enabled=False)]})
        async with self.client() as client:
            response = await client.post("/chat", json={"service_id": "service_a", "message": "hello"})
        self.assertEqual(response.json(), {"error": "service_disabled"})
        self.assertEqual(self.requests, [])

    async def test_missing_referenced_profile_fails_without_legacy_fallback(self):
        self.configure("a")
        (self.config / "ai" / "ai-a.yaml").unlink()
        async with self.client() as client:
            response = await client.post("/chat", json={"service_id": "service_a", "message": "hello"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "configuration_unavailable"})
        self.assertEqual(self.requests, [])

    async def test_missing_model_or_credentials_are_explicit_errors(self):
        for missing in ("model", "credential"):
            with self.subTest(missing=missing):
                self.configure("a")
                self.env["KEY_A"] = "fixture-provider-key-a"
                if missing == "model":
                    self.write(self.config / "ai" / "ai-a.yaml", {"api_key_env": "KEY_A"})
                else:
                    self.env["KEY_A"] = ""
                async with self.client() as client:
                    response = await client.post("/chat", json={"service_id": "service_a", "message": "hello"})
                self.assertEqual(response.status_code, 503)
        self.assertEqual(self.requests, [])

    async def test_missing_knowledge_body_or_parent_is_empty_without_creating_files(self):
        self.configure("a")
        body = self.data / "services" / "service_a" / "context.txt"
        body.unlink()
        for remove_parent in (False, True):
            if remove_parent:
                body.parent.rmdir()
            async with self.client() as client:
                response = await client.post("/chat", json={"service_id": "service_a", "message": "hello"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.requests[-1][2]["messages"], [
                {"role": "system", "content": "prompt-a\nrule-a"},
                {"role": "user", "content": "hello"},
            ])
            self.assertFalse(body.exists())
            self.assertEqual(body.parent.exists(), not remove_parent)

    def test_user_configuration_overrides_default_fields(self):
        self.configure("a")
        self.write(self.config / "services.yaml", {"version": 1, "services": [self.record("a")]})
        self.write(self.config / "local.yaml", {"version": 1, "services": [{"id": "service_a", "ai_config": "ai-b"}]})
        self.write(self.config / "ai" / "ai-b.yaml", {"model": "user-model", "api_key_env": "KEY_B"})
        config = self.resolver().resolve("service_a")
        self.assertEqual(config.model, "user-model")
        self.assertEqual(config.system_prompt, "prompt-a")

    def test_path_traversal_and_embedded_keys_rejected(self):
        self.configure("a")
        for profile in (
            {"model": "model-a", "api_key": "must-not-appear"},
            {"model": "model-a", "api_base_url": "https://user:secret@provider.test/v1"},
        ):
            self.write(self.config / "ai" / "ai-a.yaml", profile)
            with self.assertRaises(RuntimeFailure):
                self.resolver().resolve("service_a")
        self.configure("a")
        self.write(self.config / "knowledge" / "knowledge-a.yaml", {"source": "../service_b/context.txt"})
        with self.assertRaises(RuntimeFailure):
            self.resolver().resolve("service_a")

    def test_duplicate_ids_and_duplicate_yaml_fields_rejected(self):
        self.write(self.config / "services.yaml", {"version": 1, "services": [self.record("a"), self.record("a")]})
        with self.assertRaises(RuntimeFailure):
            self.resolver().resolve()
        (self.config / "services.yaml").write_text("version: 1\nservices: []\nservices: []\n", encoding="utf-8")
        with self.assertRaises(RuntimeFailure):
            self.resolver().resolve()

    def test_missing_or_empty_registry_is_an_error_not_a_legacy_fallback(self):
        registry = self.config / "services.yaml"
        registry.write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeFailure):
            self.resolver().resolve()
        registry.unlink()
        with self.assertRaises(RuntimeFailure):
            self.resolver().resolve()

    def test_knowledge_symlink_cannot_cross_instance_boundary(self):
        self.configure("a", "b")
        source = self.data / "services" / "service_a" / "context.txt"
        source.unlink()
        try:
            source.symlink_to(self.data / "services" / "service_b" / "context.txt")
        except OSError:
            self.skipTest("Symlink creation is unavailable on this host")
        with self.assertRaises(RuntimeFailure):
            self.resolver().resolve("service_a")

    async def test_empty_instance_optional_references_do_not_inherit_legacy_context(self):
        self.configure("a")
        record = self.record("a")
        record["prompt_config"] = ""
        record["knowledge_config"] = ""
        self.write(self.config / "local.yaml", {"version": 1, "services": [record]})
        async with self.client() as client:
            response = await client.post("/chat", json={"service_id": "service_a", "message": "hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[0][2]["messages"], [{"role": "user", "content": "hello"}])

    async def test_authentication_and_errors_do_not_echo_inputs(self):
        self.configure("a")
        async with self.client() as client:
            response = await client.post("/chat", headers={"Authorization": "Bearer wrong"}, json={"message": "private-message"})
            self.assertEqual(response.status_code, 401)
            response = await client.post("/chat", json={"message": "private-message", "api_key": "private-api-key"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("private-", response.text)
            self.assertEqual((await client.get("/health")).json(), {"status": "ok"})

    async def test_provider_errors_do_not_log_or_return_config_credentials_or_messages(self):
        self.configure("a")

        async def failing_provider(request):
            return httpx.Response(401, text="fixture-provider-key-a private-message prompt-a")

        with self.assertLogs("ai_runtime", level="WARNING") as logs:
            async with self.client(failing_provider) as client:
                response = await client.post("/chat", json={"service_id": "service_a", "message": "private-message"})
        self.assertEqual(response.status_code, 502)
        visible = response.text + "\n".join(logs.output)
        for forbidden in ("fixture-provider-key-a", "private-message", "prompt-a", "knowledge-a", "model-a"):
            self.assertNotIn(forbidden, visible)


if __name__ == "__main__":
    unittest.main()
