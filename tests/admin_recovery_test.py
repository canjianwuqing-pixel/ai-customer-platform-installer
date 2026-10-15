"""Local recovery controller + real transactional SQLite/HTTP authentication.

Only the terminal/Docker transport is simulated. The portable worker runs in a
separate Python process with its password on stdin, as it does under docker exec.
"""

from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from platform_admin.app import create_app
from platform_admin.auth import AdminError, AuthStore
from platform_admin import recovery
from platform_admin.password_reset import ResetError, reset_existing_password

ROOT = Path(__file__).resolve().parents[1]
RUN = subprocess.run


class Console:
    def __init__(self, passwords=()):
        self.stream = io.StringIO()
        self.passwords = iter(passwords)
        self.prompts = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def write(self, message):
        self.stream.write(message)

    def password(self, prompt):
        self.prompts.append(prompt)
        return next(self.passwords)


class RecoveryFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="admin-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {"ADMIN_PASSWORD": "original-fixture-password", "ADMIN_USERNAME": "admin",
                    "ADMIN_SESSION_SECRET": "fixture-session-secret-not-real-123456789",
                    "SERVICE_MANAGEMENT_TOKEN": "fixture-management-token-not-real-123456789",
                    "ADMIN_MAINTENANCE_TOKEN": "fixture-maintenance-token-not-real-123456789",
                    "ADMIN_DATA_PATH": str(self.root / "admin")}
        self.auth = AuthStore(self.env)
        self.auth.login("admin", self.env["ADMIN_PASSWORD"])
        self.console = Console()
        self.calls = []
        self.worker_results = []
        self.ids = b"fixture-admin-container\n"
        self.compatible = True
        self.direct_access = True
        self.deny_worker = False
        self.metadata = {"State": {"Running": True}, "Config": {"Labels": {
            "com.docker.compose.project.config_files": "/opt/project/stacks/ai-service/compose.yaml,/opt/project/stacks/admin/compose.yaml"}},
            "Mounts": [{"Type": "volume", "Name": "fixture_existing_admin_data", "RW": True, "Destination": "/app/data"}]}
        self.enterContext(patch.dict(os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""}))
        self.enterContext(patch.object(recovery, "Terminal", side_effect=lambda: self.console))
        self.enterContext(patch.object(recovery.shutil, "which", side_effect=lambda _: "/fixture/tool"))
        self.enterContext(patch.object(recovery.subprocess, "run", side_effect=self.execute))

    def execute(self, args, **options):
        self.calls.append((args, options))
        if args[:2] == ["sudo", "-v"]:
            return subprocess.CompletedProcess(args, 0)
        docker = args[args.index("docker") + 1:]
        if docker[:1] == ["--host"]:
            docker = docker[2:]
        command = docker[0]
        result = b""
        if command == "context":
            result = b"unix:///fixture/docker.sock\n"
        elif command == "version":
            if not self.direct_access and args[0] != "sudo":
                return subprocess.CompletedProcess(args, 1, b"", b"permission denied")
        elif command == "ps":
            result = self.ids
        elif command == "inspect":
            result = json.dumps(self.metadata).encode()
        elif command == "exec" and docker[1] != "--interactive":
            result = json.dumps({"path": "/app/data/auth.sqlite3", "compatible": self.compatible}).encode()
        elif command == "exec":
            if self.deny_worker:
                return subprocess.CompletedProcess(args, 1, options["input"], options["input"])
            worker_env = {**os.environ, **self.env, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
            worker = RUN([sys.executable, "-c", docker[-1]], input=options["input"],
                         capture_output=True, cwd=ROOT, env=worker_env, timeout=30)
            self.worker_results.append(worker)
            return worker
        else:
            raise AssertionError("Recovery attempted a non-recovery Docker operation")
        return subprocess.CompletedProcess(args, 0, result, b"")

    def reset(self, manual=False):
        # stdout/stderr are deliberately separate from the controlling terminal.
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = recovery.main(["--project", "fixture-project", "--service", "ai-admin"] + (["--manual"] if manual else []))
        return result, stdout.getvalue() + stderr.getvalue()

    def login_http(self, password):
        with TestClient(create_app(self.env), base_url="https://platform.example.test", follow_redirects=False) as client:
            page = client.get("/admin/login")
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
            return client.post("/admin/login", data={"username": "admin", "password": password, "csrf": csrf},
                               headers={"origin": "https://platform.example.test"}).status_code

    def unchanged(self):
        self.auth.login("admin", self.env["ADMIN_PASSWORD"])

    def test_random_reset_unlocks_revokes_persists_and_does_not_log(self):
        old, _ = self.auth.create_session(True)
        prelogin, _ = self.auth.create_session()
        business = self.root / "business-data"
        business.write_bytes(b"chatwoot/runtime/services/knowledge/chat unchanged")
        for _ in range(5):
            with self.assertRaises(AdminError):
                self.auth.login("admin", "wrong")
        self.assertEqual(self.login_http(self.env["ADMIN_PASSWORD"]), 429)
        result, logs = self.reset()
        self.assertEqual(result, 0, logs)
        password = self.console.stream.getvalue().splitlines()[-1]
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{43}", password) is not None)
        self.assertEqual(self.console.stream.getvalue().count(password), 1)
        self.assertTrue(password not in logs)
        for args, options in self.calls:
            self.assertTrue(password not in repr(args))
            self.assertTrue(password not in repr(options.get("env", {})))
        self.assertEqual([(r.returncode, r.stdout.strip(), r.stderr) for r in self.worker_results], [(0, b"reset-ok", b"")])
        self.assertIsNone(self.auth.session(old))
        self.assertIsNone(self.auth.session(prelogin))
        with closing(sqlite3.connect(self.auth.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(self.login_http(self.env["ADMIN_PASSWORD"]), 401)
        self.assertEqual(self.login_http(password), 303)
        # New process/image gets stale bootstrap env, but the same volume wins.
        fresh = AuthStore({**self.env, "ADMIN_PASSWORD": "stale-bootstrap-password"})
        fresh.login("admin", password)
        self.assertEqual(business.read_bytes(), b"chatwoot/runtime/services/knowledge/chat unchanged")

    def test_manual_hidden_confirmation_accepts_six_characters_and_unicode(self):
        self.console = Console(("密码 简单!", "密码 简单!"))
        result, logs = self.reset(True)
        self.assertEqual(result, 0, logs)
        self.assertEqual(len(self.console.prompts), 2)
        self.assertNotIn("密码 简单!", self.console.stream.getvalue() + logs)
        self.assertEqual(self.login_http("密码 简单!"), 303)
        self.assertEqual(self.login_http(self.env["ADMIN_PASSWORD"]), 401)

    def test_mismatch_or_short_manual_input_keeps_password(self):
        for passwords in (("short",), ("sixsix", "other1")):
            with self.subTest():
                self.console = Console(passwords)
                result, _ = self.reset(True)
                self.assertEqual(result, 1)
                self.unchanged()
        self.assertFalse(self.worker_results)

    def legacy_account(self):
        salt = b"fixture-salt-123"
        encoded = "pbkdf2_sha256:600000:" + salt.hex() + ":" + hashlib.pbkdf2_hmac(
            "sha256", self.env["ADMIN_PASSWORD"].encode(), salt, 600000).hex()
        with closing(sqlite3.connect(self.auth.path)) as db:
            db.execute("DELETE FROM admin_settings")
            db.execute("CREATE TABLE account (id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT NOT NULL)")
            db.execute("INSERT INTO account VALUES (1, ?)", (encoded,))
            db.commit()
        self.env["ADMIN_PASSWORD_HASH"] = encoded
        return encoded

    def test_legacy_account_migrates_without_restoring_old_environment_hash(self):
        self.legacy_account()
        self.console = Console(("legacy-new", "legacy-new"))
        self.assertEqual(self.reset(True)[0], 0)
        self.assertEqual(self.login_http("legacy-new"), 303)
        self.assertEqual(self.login_http(self.env["ADMIN_PASSWORD"]), 401)
        with closing(sqlite3.connect(self.auth.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='account'").fetchone())

    def test_failed_legacy_migration_keeps_valid_old_hash_and_session(self):
        token, _ = self.auth.create_session(True)
        encoded = self.legacy_account()
        with closing(sqlite3.connect(self.auth.path)) as db:
            db.execute("CREATE TRIGGER fail_reset BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT,'private storage error'); END")
            db.commit()
        self.console = Console(("replacement", "replacement"))
        self.assertEqual(self.reset(True)[0], 1)
        with closing(sqlite3.connect(self.auth.path)) as db:
            self.assertTrue(db.execute("SELECT hash FROM account").fetchone()[0] == encoded)
            self.assertEqual(db.execute("SELECT count(*) FROM admin_settings").fetchone()[0], 0)
        self.assertIsNotNone(self.auth.session(token))
        self.unchanged()

    def test_transaction_failure_restores_password_sessions_and_lockout(self):
        token, _ = self.auth.create_session(True)
        with closing(sqlite3.connect(self.auth.path)) as db:
            db.execute("INSERT INTO attempts VALUES (1,5,9999999999)")
            db.execute("CREATE TRIGGER fail_reset BEFORE DELETE ON attempts BEGIN SELECT RAISE(ABORT,'private storage error'); END")
            db.commit()
        self.console = Console(("replacement", "replacement"))
        result, logs = self.reset(True)
        self.assertEqual(result, 1)
        self.assertNotIn("replacement", logs + self.console.stream.getvalue())
        self.assertEqual(self.worker_results[0].stdout.strip(), b"reset-failed")
        self.assertEqual(self.worker_results[0].stderr, b"")
        with closing(sqlite3.connect(self.auth.path)) as db:
            self.assertTrue(db.execute("SELECT value FROM admin_settings").fetchone()[0] == self.env["ADMIN_PASSWORD"])
            self.assertEqual(db.execute("SELECT failures FROM attempts").fetchone()[0], 5)
        self.assertIsNotNone(self.auth.session(token))

    def test_no_database_creation_or_unrecognized_schema_writes(self):
        absent = self.root / "absent"
        with self.assertRaises(ResetError):
            reset_existing_password("replacement", {**self.env, "ADMIN_DATA_PATH": str(absent)})
        self.assertFalse(absent.exists())
        with closing(sqlite3.connect(self.auth.path)) as db:
            db.execute("PRAGMA user_version=999")
        before = self.auth.path.read_bytes()
        with self.assertRaises(ResetError):
            reset_existing_password("replacement", self.env)
        self.assertTrue(before == self.auth.path.read_bytes())

    def test_reject_remote_docker_and_unsafe_targets_before_new_password(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote.invalid:2375"}):
            self.assertEqual(self.reset()[0], 1)
        cases = [lambda: self.metadata["State"].update(Running=False),
                 lambda: self.metadata["Mounts"][0].update(RW=False),
                 lambda: self.metadata["Mounts"][0].update(Type="bind"),
                 lambda: self.metadata["Config"]["Labels"].clear()]
        original = json.dumps(self.metadata)
        for change in cases:
            self.metadata = json.loads(original)
            change()
            with self.subTest():
                self.assertEqual(self.reset()[0], 1)
        self.metadata = json.loads(original)
        self.ids = b"one\ntwo\n"
        self.assertEqual(self.reset()[0], 1)
        self.ids = b"one\n"
        self.compatible = False
        self.assertEqual(self.reset()[0], 1)
        self.assertFalse(self.worker_results)
        self.unchanged()

    def test_access_requires_docker_authority_or_sudo_before_password_input(self):
        self.direct_access = False
        with patch.object(recovery.os, "geteuid", return_value=0, create=True):
            self.assertEqual(self.reset()[0], 1)
        self.unchanged()
        with patch.object(recovery.os, "geteuid", return_value=1000, create=True):
            self.assertEqual(self.reset()[0], 0)
        self.assertIn(["sudo", "-v"], [args for args, _ in self.calls])
        privileged = [args for args, _ in self.calls if args[0] == "sudo" and args[1] != "-v"]
        self.assertTrue(all(args[:3] == ["sudo", "-n", "--"] for args in privileged))

    def test_docker_failure_never_exposes_password_from_error_output(self):
        self.console = Console(("private-value", "private-value"))
        self.deny_worker = True
        result, logs = self.reset(True)
        self.assertEqual(result, 1)
        self.assertNotIn("private-value", logs + self.console.stream.getvalue())
        self.unchanged()

    def test_inflight_old_password_login_cannot_recreate_revoked_session(self):
        pending, _ = self.auth.create_session()
        self.auth.login("admin", self.env["ADMIN_PASSWORD"])
        reset_existing_password("replacement", self.env)
        with self.assertRaises(AdminError) as error:
            self.auth.create_session(True, pending)
        self.assertEqual(error.exception.status, 401)

    def test_outdated_image_without_session_revocation_guard_is_read_only(self):
        before = self.auth.path.read_bytes()
        with patch.object(AuthStore, "recovery_protocol", None):
            with self.assertRaises(ResetError):
                reset_existing_password("replacement", self.env)
        self.assertTrue(before == self.auth.path.read_bytes())

    def test_no_public_password_recovery_http_route(self):
        with TestClient(create_app(self.env), base_url="https://platform.example.test", follow_redirects=False) as client:
            page = client.get("/admin/login")
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
            self.assertEqual(client.post("/admin/login", data={"username": "admin", "password": self.env["ADMIN_PASSWORD"],
                                                              "csrf": csrf}).status_code, 303)
            page = client.get("/admin/password")
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
            for route in ("/admin/reset-admin-password", "/admin/api/reset-admin-password"):
                self.assertEqual(client.post(route, json={}, headers={"x-csrf-token": csrf}).status_code, 404)
        self.unchanged()

    def test_without_controlling_terminal_fails_before_docker(self):
        # Test the actual implementation, rather than the Console test double.
        # The class name is patched in this fixture; recover the original via a
        # short fresh process so its real /dev/tty refusal is also exercised.
        source = ("from unittest.mock import patch;from platform_admin.recovery import Terminal,RecoveryError;"
                  "patch('os.O_NOCTTY',0,create=True).start();patch('os.O_CLOEXEC',0,create=True).start();"
                  "p=patch('os.open',side_effect=OSError());p.start();\n"
                  "try:\n with Terminal(): pass\n"
                  "except RecoveryError: pass\n"
                  "else: raise SystemExit(1)\n")
        result = RUN([sys.executable, "-c", source], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_manual_input_does_not_allow_getpass_visible_fallback(self):
        # Use a fresh interpreter for the real Terminal implementation.
        source = ("import getpass,warnings;from unittest.mock import patch;"
                  "from platform_admin.recovery import Terminal;\n"
                  "def fallback(*a,**k): warnings.warn('no echo control',getpass.GetPassWarning)\n"
                  "t=Terminal();t.stream=None\n"
                  "with patch('getpass.getpass',side_effect=fallback):\n"
                  " try: t.password('hidden')\n"
                  " except getpass.GetPassWarning: pass\n"
                  " else: raise SystemExit(1)\n")
        result = RUN([sys.executable, "-c", source], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
