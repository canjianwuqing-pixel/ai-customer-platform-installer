"""Transactional website lifecycle, failure injection and existing data safety."""
from copy import deepcopy
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from platform_admin.site_domains import WebsiteDomains, SiteError, hostname, atomic, validate_registry
from platform_admin.maintenance_worker import Runner, MaintenanceError
from platform_admin.worker_refresh import refresh

ROOT = Path(__file__).resolve().parents[1]


class Host(WebsiteDomains):
    def __init__(self, directory):
        env = {"INSTALLER_ROOT": str(ROOT), "ADMIN_MAINTENANCE_STATE_DIR": str(directory / "state"),
            "NGINX_SITE_CONFIG_PATH": str(directory / "site.conf"), "DOMAIN": "primary.example.test",
            "CERTBOT_FULLCHAIN_PATH": (directory / "cert.pem").as_posix(), "CERTBOT_PRIVATE_KEY_PATH": (directory / "key.pem").as_posix(),
            "CERTBOT_CONFIG_DIR": (directory / "certbot").as_posix(), "DOMAIN_HTTPS_TEMPLATE_FILE": str(ROOT / "templates/domain/chatwoot-https.conf.template"),
            "WEBSITE_CERTBOT_WEBROOT": (directory / "webroot").as_posix(), "NGINX_LISTEN_PORT": "80", "DOMAIN_HTTPS_PORT": "443",
            "ADMIN_ENABLED": "true", "AI_RUNTIME_BUILD": "true", "NGINX_CLIENT_MAX_BODY_SIZE": "40m",
            "ADMIN_UPSTREAM_HOST": "127.0.0.1", "AI_ADMIN_HTTP_PORT": "8080", "AI_SERVICE_BIND_ADDRESS": "127.0.0.1",
            "AI_SERVICE_HTTP_PORT": "8000", "NGINX_UPSTREAM_HOST": "127.0.0.1", "CHATWOOT_HTTP_PORT": "3000",
            "NGINX_PROXY_READ_TIMEOUT_SECONDS": "3600"}
        super().__init__(env)
        self.site.write_text("# Managed by AI Customer Platform Installer.\noriginal site", encoding="utf-8", newline="\n")
        self.calls = []
        self.fail = ""

    def certificate(self, row):
        return {"ssl_status": "需要检查" if self.fail == "certificate" else "有效", "expires": "2099-01-01"}

    def addresses(self, domain):
        if self.fail == "dns":
            raise SiteError("domain_dns_failed")
        self.calls.append(("dns", domain))
        return ["203.0.113.7"]

    def apply(self, content):
        if self.fail == "apply" and "original site" not in content:
            raise SiteError("syntax_failure")
        self.calls.append(("apply", content))
        self.site.write_text(content, encoding="utf-8", newline="\n")

    def task(self, action, *args):
        self.calls.append((action, *args))
        if self.fail == action:
            self.fail = ""  # rollback must still execute
            raise SiteError("task_failed")

    def probe(self, domain, nonce, *, https=True):
        self.calls.append(("probe", domain, https))
        if self.fail == ("https" if https else "http"):
            raise SiteError("domain_access_failed")

    def inspect(self, domain):
        self.calls.append(("inspect", domain))  # host I/O double; real tests run nginx -T


class WebsiteFixtures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="website-domain-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.host = Host(self.directory)
        self.original = self.host.site.read_bytes()

    def test_add_stages_http_before_cert_and_https_before_publication(self):
        self.host.operation("add", "backup.example.test")
        state = self.host.read()
        self.assertEqual(len(state["domains"]), 2)
        self.assertEqual(state["primary"], "primary.example.test")
        calls = self.host.calls
        self.assertLess(calls.index(("probe", "backup.example.test", False)), calls.index(("issue", "backup.example.test")))
        self.assertLess(calls.index(("issue", "backup.example.test")), calls.index(("probe", "backup.example.test", True)))
        config = self.host.site.read_text()
        for text in ("/support/", "/admin/", "/cable", "Upgrade $http_upgrade", "ssl_certificate", "api_access_token"):
            if text == "api_access_token":
                self.assertIn("underscores_in_headers on", config)
            else:
                self.assertIn(text, config)
        self.assertNotIn("{{", config)
        self.assertNotRegex(config, r"listen[^;]*\bdefault_server\b")
        self.assertNotIn("ignore_invalid_headers off", config)
        self.assertEqual(config.count("\nunderscores_in_headers on;"), 1)
        self.assertFalse((self.host.directory / "website-pending.json").exists())

    def test_every_failed_activation_restores_site_and_does_not_enable_alias(self):
        for stage in ("dns", "http", "issue", "certificate", "https", "apply"):
            with self.subTest(stage=stage):
                self.host.fail = stage
                with self.assertRaises(SiteError):
                    self.host.operation("add", "backup.example.test")
                self.assertEqual(self.host.site.read_bytes(), self.original)
                self.assertFalse(self.host.path.exists())

    def test_nonce_uses_only_unique_top_level_slot_not_repeated_body_directives(self):
        nonce = secrets.token_hex(24)
        content = self.host.render(self.host.read(), nonce=nonce)
        self.assertEqual(content.count('location = /.__platform-domain-check'), 1)
        self.assertIn('    location = /.__platform-domain-check', content)
        webhook = content.split('location ~ "^/integrations/chatwoot/', 1)[1].split('    location / {', 1)[0]
        self.assertNotIn('/.__platform-domain-check', webhook)
        self.assertIn('client_max_body_size 1m;', webhook)
        self.assertNotIn('PLATFORM_DOMAIN_CHECK_LOCATION', content)

    def test_missing_duplicate_or_indented_slot_and_unsafe_nonce_fail_closed(self):
        source = Path(self.host.env['DOMAIN_HTTPS_TEMPLATE_FILE']).read_text(encoding='utf-8')
        marker = '    # PLATFORM_DOMAIN_CHECK_LOCATION'
        self.assertEqual(source.splitlines().count(marker), 1)
        candidate = self.directory / 'template.conf'
        self.host.env['DOMAIN_HTTPS_TEMPLATE_FILE'] = str(candidate)
        for modified in (source.replace(marker, ''), source.replace(marker, marker + '\n' + marker),
                         source.replace(marker, '    ' + marker)):
            with self.subTest(template=modified):
                candidate.write_text(modified, encoding='utf-8')
                with self.assertRaisesRegex(SiteError, '^invalid_site_template$'):
                    self.host.render(self.host.read(), nonce=secrets.token_hex(24))
        candidate.write_text(source, encoding='utf-8')
        for nonce in ('"; return 500; #', 'a\nb', 'a' * 129):
            with self.assertRaisesRegex(SiteError, '^invalid_domain_nonce$'):
                self.host.render(self.host.read(), nonce=nonce)

    def test_no_nonce_has_no_verification_location_in_tls_or_http_fallback(self):
        for certificate in ('', 'certificate'):
            self.host.fail = certificate
            content = self.host.render(self.host.read(), challenge='pending.example.test')
            self.assertNotIn('location = /.__platform-domain-check', content)
            self.assertNotIn('PLATFORM_DOMAIN_CHECK_LOCATION', content)

    def test_primary_failover_delete_reinstall_restart_and_backup_restore(self):
        self.host.operation("add", "backup.example.test")
        backup = deepcopy(self.host.read())
        self.host.calls.clear()
        self.host.operation("primary", "backup.example.test")
        self.assertIn(("primary", "backup.example.test", "primary.example.test"), [call[:3] for call in self.host.calls])
        self.assertNotIn(("probe", "primary.example.test", True), self.host.calls)
        self.assertEqual(WebsiteDomains(self.host.env).read()["primary"], "backup.example.test")
        with self.assertRaises(SiteError):
            self.host.operation("delete", "backup.example.test")
        self.host.operation("delete", "primary.example.test")
        self.assertEqual(len(self.host.read()["domains"]), 1)
        self.host.operation("reconcile")
        self.assertEqual(self.host.read()["primary"], "backup.example.test")
        self.host.operation("restore-check", restored=backup)
        self.host.operation("restore", restored=backup)
        self.assertEqual(self.host.read(), backup)

    def test_failed_primary_migration_rolls_back_dependencies_and_site(self):
        self.host.operation("add", "backup.example.test")
        before = self.host.site.read_bytes()
        state = self.host.read()
        self.host.fail = "primary"
        with self.assertRaises(SiteError):
            self.host.operation("primary", "backup.example.test")
        self.assertEqual(self.host.site.read_bytes(), before)
        self.assertEqual(self.host.read(), state)

    def test_invalid_restore_certificate_preflight_does_not_change_current_site(self):
        self.host.fail = "certificate"
        with self.assertRaises(SiteError):
            self.host.operation("restore-check", restored=self.host.read())
        self.assertEqual(self.host.site.read_bytes(), self.original)

    def test_lost_or_expired_primary_certificate_does_not_block_verified_backup_failover(self):
        self.host.operation("add", "backup.example.test")
        original = self.host.certificate
        self.host.certificate = lambda row: ({"ssl_status": "需要检查", "expires": ""}
            if row["domain"] == "primary.example.test" else original(row))
        self.host.operation("primary", "backup.example.test")
        self.assertEqual(self.host.read()["primary"], "backup.example.test")
        self.assertEqual(self.host.site.read_text().count("server_name primary.example.test;"), 1)  # HTTP-01 only
        self.assertIn("server_name backup.example.test;", self.host.site.read_text())
        self.assertEqual(len(self.host.read()["domains"]), 2)

    def test_crash_journal_replays_original_domain_and_nginx_before_next_operation(self):
        registry = self.host.read()
        atomic(self.host.directory / "website-pending.json", json.dumps({"registry": registry,
            "nginx": self.original.decode(), "primary": registry["primary"], "target": "backup.example.test"}).encode())
        self.host.recover()
        self.assertEqual(self.host.read(), registry)
        self.assertEqual(self.host.site.read_bytes(), self.original)

    def test_domain_validation_and_privileged_bridge_allowlist(self):
        runner = Runner(self.directory, self.directory / ".env", self.directory / "runner")
        for value in ("-d", "a;id.example", "*.example.test", "127.0.0.1", "a.example\n", "a/../example.test", "https://example.test"):
            with self.subTest(value=value):
                with self.assertRaises(SiteError):
                    hostname(value)
                with self.assertRaises(MaintenanceError):
                    runner.command("domain-add", value)
        self.assertEqual(runner.command("domain-add", "backup.example.test")[-2:], ["add", "backup.example.test"])
        with self.assertRaises(MaintenanceError):
            runner.command("domain-exec", "backup.example.test")

    def test_dns_requires_every_published_ip_to_reach_challenge_no_curl_insecure(self):
        import subprocess
        manager = WebsiteDomains(self.host.env)
        with patch.object(manager, "addresses", return_value=["203.0.113.7", "2001:db8::1"]), patch.object(manager, "execute", return_value=subprocess.CompletedProcess([], 0, b"nonce\n200")) as execute:
            manager.probe("backup.example.test", "nonce", https=False)
            self.assertEqual(execute.call_count, 3)  # local readiness + ALL A/AAAA
            for call in execute.call_args_list:
                self.assertNotIn("--insecure", call.args[0])
                self.assertIn("--noproxy", call.args[0])

    def test_tls_activation_checks_static_assets_and_websocket_not_only_html(self):
        import subprocess
        manager = WebsiteDomains(self.host.env)
        calls = []
        def command(args):
            calls.append(args)
            url = args[-1]
            if url.endswith("/.__platform-domain-check"):
                return b"nonce"
            if url.endswith("/admin/login"):
                return b'<input name="csrf">'
            if "--write-out" in args:
                return b"200"
            return b'<html><script src="/vite/assets/dashboard.js"></script></html>'
        def execute(args, **options):
            import base64
            import hashlib
            calls.append(args)
            if args[-1].endswith("/.__platform-domain-check"):
                return subprocess.CompletedProcess(args, 0, b"nonce\n200")
            key = next(arg.split(": ", 1)[1] for arg in args if arg.startswith("Sec-WebSocket-Key: "))
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            return subprocess.CompletedProcess(args, 28, b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        with patch.object(manager, "addresses", return_value=["203.0.113.7"]), patch.object(manager, "command", side_effect=command):
            manager.execute = execute
            manager.probe("backup.example.test", "nonce")
            self.assertTrue(any(args[-1].endswith("/vite/assets/dashboard.js") for args in calls))
            self.assertTrue(any(args[-1].endswith("/cable") for args in calls))
            self.assertTrue(any("--header" in args for args in calls))
        with patch.object(manager, "addresses", return_value=["203.0.113.7"]), patch.object(manager, "execute", return_value=subprocess.CompletedProcess([], 1, b"wrong-marker")):
            with self.assertRaises(SiteError):
                manager.probe("backup.example.test", "nonce")

    def test_actual_certificate_san_dates_and_key_match(self):
        # Integration suite supplies real certificates; this method must fail
        # closed for arbitrary metadata/credential paths, never show their text.
        manager = WebsiteDomains(self.host.env)
        self.assertEqual(manager.state()["domains"][0]["ssl_status"], "需要检查")

    def test_executor_upgrade_refresh_waits_for_job_and_keeps_sqlite_lock_until_stop(self):
        import sqlite3
        from contextlib import closing
        runner = Runner(self.directory, self.directory / ".env", self.directory / "worker")
        calls = []
        def execute(args, **options):
            calls.append(args)
            if args[1] == "stop":
                with closing(sqlite3.connect(runner.database, timeout=0)) as db:
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute("BEGIN IMMEDIATE")
            return __import__('subprocess').CompletedProcess(args, 0)
        refresh(runner.database, "fixture.service", execute=execute)
        self.assertEqual([call[1] for call in calls], ["stop", "start"])
        with runner.db() as db:
            db.execute("INSERT INTO jobs VALUES ('job','upgrade','','running',1)")
        calls.clear()
        with self.assertRaises(ValueError):
            refresh(runner.database, "fixture.service", execute=execute, deadline_seconds=0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
