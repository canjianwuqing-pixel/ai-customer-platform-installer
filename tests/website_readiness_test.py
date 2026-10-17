"""Production probe against old/new real Nginx workers; no public DNS/server."""
import os
import json
import secrets
import shutil
import subprocess
import threading
import unittest
from fastapi import WebSocket
from fastapi.responses import HTMLResponse, Response

import website_nginx_test as nginx
from platform_admin.site_domains import WebsiteDomains, SiteError, atomic


class RealReadinessFixtures(unittest.TestCase):
    def setUp(self):
        self.h = nginx.WebsiteNginxFixtures()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        @self.h.backend.get('/')
        def home():
            return HTMLResponse('<script src="/vite/assets/probe.js"></script>')

        @self.h.backend.get('/vite/assets/probe.js')
        def asset():
            return Response('// disposable Chatwoot asset protocol fixture', media_type='text/javascript')

        @self.h.backend.websocket('/cable')
        async def cable(websocket: WebSocket):
            await websocket.accept()
            await websocket.close()

        self.h.http_default_404 = True
        self.h.start()
        self.h.syntax_results = []
        self.nonce = secrets.token_hex(24)
        self.curl = os.environ.get('TEST_CURL_BINARY') or shutil.which('curl')
        self.assertTrue(self.curl, 'Real curl is required for the readiness regression.')

    def execute(self, args, **kwargs):
        if args[0] == 'curl':
            ca = self.h.driver.directory / 'fixture-ca.pem'
            ca.write_bytes(b''.join(p.read_bytes() for p in (self.h.driver.directory / 'certbot/live').glob('*/fullchain.pem')))
            args = [self.curl, '--cacert', str(ca), *args[1:]]
        return self.h.execute(args, **kwargs)

    def report(self):
        directory = self.h.manager.directory / 'website-diagnostics'
        return json.loads(max(directory.glob('*.json'), key=lambda p: p.stat().st_mtime_ns).read_text())

    def production_probe(self):
        self.h.manager.execute = self.execute
        self.h.manager.probe = WebsiteDomains.probe.__get__(self.h.manager)

    def test_valid_staged_include_can_404_until_reload_reaches_workers(self):
        h = self.h
        h.defer_reload = True
        h.manager.apply(h.manager.render(h.manager.read(), challenge=h.pending, nonce=self.nonce))
        # nginx -t passed, the complete configuration includes the intended
        # server_name, and the request's real Host reaches an OLD worker: 404.
        self.assertTrue(all(code == 0 for code in h.syntax_results))
        result = subprocess.run([h.driver.nginx, '-p', h.driver.directory.as_posix() + '/',
            '-c', str(h.driver.directory / 'nginx.conf'), '-T'], capture_output=True, check=True)
        self.assertTrue(('server_name ' + h.pending + ';').encode() in result.stdout)
        response = h.request(h.pending, '/.__platform-domain-check', https=False)
        self.assertEqual(response.status_code, 404)
        errors = []

        def reload():
            result = subprocess.run([h.driver.nginx, '-p', h.driver.directory.as_posix() + '/',
                '-c', str(h.driver.directory / 'nginx.conf'), '-s', 'reload'], capture_output=True)
            if result.returncode:
                errors.append(result.returncode)

        timer = threading.Timer(.5, reload)
        timer.start()
        self.addCleanup(timer.join)
        h.manager.execute = self.execute
        WebsiteDomains.probe(h.manager, h.pending, self.nonce, https=False)
        self.assertEqual(errors, [])
        h.wait_check(h.pending, self.nonce, https=False)
        h.assert_webhook(h.domains[0])

    def test_real_https_and_http_workflow_records_every_stage_without_credentials(self):
        h = self.h
        self.production_probe()
        h.manager.operation('add', h.pending)
        report = self.report()
        self.assertEqual(report['result'], 'ok')
        stages = {e['stage'] for e in report['events'] if e['status'] == 'ok'}
        self.assertTrue({'dns', 'nginx_staging', 'nginx_test', 'nginx_reload', 'nginx_inspect',
            'http_local', 'http_public', 'certbot', 'certificate', 'https_local', 'https_public',
            'admin', 'static_assets', 'websocket', 'save'} <= stages, stages)
        text = json.dumps(report)
        for credential in (h.f.protocol.token, h.f.key_a, h.f.fixture.password, h.f.fixture.env['SERVICE_MANAGEMENT_TOKEN']):
            self.assertNotIn(credential, text)
        self.assertEqual(set(h.manager.state()), {'domains'})
        h.wait_check(h.pending, '')
        h.assert_webhook(h.pending)

    def test_no_worker_readiness_times_out_without_certbot_and_rolls_back(self):
        h = self.h
        h.defer_reload = True
        self.production_probe()
        h.manager.PROBE_READY_SECONDS = .75
        before = h.manager.read()
        with self.assertRaisesRegex(SiteError, '^domain_http_local_timeout$'):
            h.manager.operation('add', h.pending)
        self.assertFalse(any(call[0] == 'issue' for call in h.calls))
        self.assertEqual(h.manager.site.read_bytes(), h.original)
        self.assertEqual(h.manager.read(), before)
        report = self.report()
        self.assertEqual(report['failed_stage'], 'http_local')
        self.assertTrue(any(e['stage'] == 'rollback' and e['status'] == 'ok' for e in report['events']))
        h.assert_webhook(h.domains[0])

    def test_included_file_with_conflicting_server_name_is_not_misdiagnosed_as_delay(self):
        h = self.h
        # Existing unrelated site claims the target name. The fixture owns its
        # initial setup, but the operation must neither rewrite nor delete it.
        conflict = f'server {{ listen 127.0.0.1:{h.driver.http_port}; server_name {h.pending}; return 404; }}\n'.encode()
        atomic(h.foreign, conflict)
        self.production_probe()
        with self.assertRaisesRegex(SiteError, '^nginx_server_name_conflict$'):
            h.manager.operation('add', h.pending)
        self.assertFalse(any(call[0] == 'issue' for call in h.calls))
        self.assertEqual(h.foreign.read_bytes(), conflict)
        self.assertEqual(h.manager.site.read_bytes(), h.original)
        self.assertEqual(self.report()['failed_stage'], 'nginx_inspect')
        h.assert_webhook(h.domains[0])

    def test_site_not_included_is_reported_and_existing_site_stays_available(self):
        h = self.h
        shadow = h.manager.site.parent / 'not-included.conf'
        atomic(shadow, h.original)
        h.manager.site = shadow
        self.production_probe()
        with self.assertRaisesRegex(SiteError, '^nginx_site_not_included$'):
            h.manager.operation('add', h.pending)
        self.assertFalse(any(call[0] == 'issue' for call in h.calls))
        self.assertEqual(shadow.read_bytes(), h.original)
        self.assertEqual(self.report()['error_code'], 'nginx_site_not_included')
        h.assert_webhook(h.domains[0])


if __name__ == '__main__':
    unittest.main()
