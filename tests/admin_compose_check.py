"""Validate with a real Compose binary, without printing resolved secrets."""

import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
command = [os.environ.get("TEST_COMPOSE_BINARY", "docker" )]
if "TEST_COMPOSE_BINARY" not in os.environ:
    command.append("compose")
for file in ("config/default.env", "config/images.env", "tests/fixtures/chatwoot.test.env", "tests/fixtures/admin.test.env"):
    command += ["--env-file", str(root / file)]
for file in ("stacks/ai-service/compose.yaml", "stacks/ai-service/compose.runtime.yaml", "stacks/admin/compose.yaml"):
    command += ["--file", str(root / file)]
result = subprocess.run(command + ["config", "--format", "json"], cwd=root, capture_output=True)
if result.returncode:
    sys.exit("Admin Compose validation failed (resolved output suppressed).")
config = json.loads(result.stdout)
assert set(config["services"]) == {"ai-service", "ai-admin"}
admin = config["services"]["ai-admin"]
assert admin["environment"]["ADMIN_PASSWORD"] == "admin"
assert not admin["environment"]["ADMIN_PASSWORD_HASH"]
assert not {"SESSION_SECRET", "AI_SERVICE_INTERNAL_TOKEN", "OPENAI_API_KEY"} & set(admin["environment"])
assert admin["read_only"] and admin["healthcheck"]
assert len(admin["ports"]) == 1 and admin["ports"][0]["host_ip"] == "127.0.0.1"
assert all("docker.sock" not in volume.get("source", "") and volume.get("target") != "/app/.env" for volume in admin["volumes"])
assert any(volume.get("source") == "ai_admin_data" for volume in admin["volumes"])
assert any(volume.get("target") == "/run/platform-health/readiness.py" and volume.get("read_only")
           and Path(volume["source"]) == root / "modules/admin/readiness.py" for volume in admin["volumes"])
print("Real Compose validation passed: one Admin, direct password configuration, restricted mounts and health check.")
