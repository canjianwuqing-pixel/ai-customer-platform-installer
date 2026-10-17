"""Actual generated vhosts, Nginx -t/reload, HTTP/TLS and signed Runtime hooks.

All domains, certificates and files are disposable. DNS/Certbot are local I/O
doubles, not evidence of public DNS resolution or Let's Encrypt issuance.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import Request
from fastapi.responses import Response

import admin_proxy_test as proxy
import openai_chatwoot_test as integration
from platform_admin.app import create_app
from platform_admin.clients import InternalClient
from platform_admin.site_domains import WebsiteDomains, SiteError, atomic


ROOT = Path(__file__).resolve().parents[1]


class WebsiteNginxFixtures(unittest.TestCase):
    def setUp(self):
        self.f = integration.IntegrationFixtures()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.prepared()
        management = InternalClient('http://runtime.test', self.f.fixture.env['SERVICE_MANAGEMENT_TOKEN'],
            httpx.ASGITransport(app=self.f.app))
        self.backend = create_app(self.f.fixture.env, management, self.f.fixture.maintenance)
        self.driver = proxy.NginxHTTPSFixtures()
        self.driver.nginx_master_process = 'on'  # exercise actual graceful reload
        self.addCleanup(lambda: self.assertTrue(self.driver.doCleanups()))
        temporary = tempfile.TemporaryDirectory(prefix='website-render-')
        self.addCleanup(temporary.cleanup)
        # The real site manager supplies ALL vhosts through a managed include.
        empty = Path(temporary.name) / 'empty.conf'
        empty.write_text('', encoding='utf-8')
        self.driver.site_template = empty
        self.domains = []
        self.calls = []
        self.failure = ''

        @self.backend.post('/integrations/chatwoot/{service_id}/webhook')
        async def runtime(request: Request, service_id: str):
            # Test-only transport adapter. The unmodified Runtime validates the
            # actual signature/body after the actual generated Nginx location.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.f.app), base_url='http://runtime.test') as client:
                result = await client.post(request.url.path, content=await request.body(), headers=request.headers)
            return Response(result.content, status_code=result.status_code, media_type='application/json')

    @staticmethod
    def loopback(content):
        # Change listeners only; preserve locations/TLS/proxy directives.
        content = re.sub(r'^\s*listen \[::\]:[^;]+;', '', content, flags=re.M)
        return re.sub(r'listen (\d+)(?=[;\s])', r'listen 127.0.0.1:\1', content)

    def certificate(self, domain):
        base = self.driver.directory / 'certbot/live' / domain
        base.mkdir(parents=True, exist_ok=True)
        subprocess.run([self.driver.openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes', '-days', '1',
            '-keyout', str(base / 'privkey.pem'), '-out', str(base / 'fullchain.pem'), '-subj', '/CN=' + domain,
            '-addext', 'subjectAltName=DNS:' + domain], capture_output=True, check=True)
        return {'domain': domain, 'fullchain': (base / 'fullchain.pem').as_posix(), 'private_key': (base / 'privkey.pem').as_posix()}

    def start(self, *, nonce='', http_stage=False, lost_certificate=False):
        def included_sites():
            d = self.driver
            self.domains = [d.domain, 'b-' + secrets.token_hex(4) + '.example.test', 'c-' + secrets.token_hex(4) + '.example.test']
            self.pending = 'd-' + secrets.token_hex(4) + '.example.test'
            rows = [self.certificate(domain) for domain in self.domains]
            if lost_certificate:
                self.lost = 'lost-' + secrets.token_hex(4) + '.example.test'
                rows.append({'domain': self.lost, 'fullchain': (d.directory / 'absent.pem').as_posix(),
                             'private_key': (d.directory / 'absent.key').as_posix()})
            site = d.directory / 'sites-available/platform.conf'
            env = {'INSTALLER_ROOT': str(ROOT), 'ADMIN_MAINTENANCE_STATE_DIR': str(d.directory / 'state'),
                'DOMAIN': d.domain, 'NGINX_SITE_CONFIG_PATH': str(site),
                'CERTBOT_FULLCHAIN_PATH': rows[0]['fullchain'], 'CERTBOT_PRIVATE_KEY_PATH': rows[0]['private_key'],
                'CERTBOT_CONFIG_DIR': (d.directory / 'certbot').as_posix(),
                'DOMAIN_HTTPS_TEMPLATE_FILE': str(ROOT / 'templates/domain/chatwoot-https.conf.template'),
                'WEBSITE_CERTBOT_WEBROOT': d.directory.as_posix(),
                'NGINX_LISTEN_PORT': str(d.http_port), 'DOMAIN_HTTPS_PORT': str(d.https_port),
                'ADMIN_ENABLED': 'true', 'AI_RUNTIME_BUILD': 'true', 'AI_SERVICE_BIND_ADDRESS': '127.0.0.1',
                'AI_SERVICE_HTTP_PORT': str(d.backend_port), 'NGINX_UPSTREAM_HOST': '127.0.0.1',
                'CHATWOOT_HTTP_PORT': str(d.backend_port), 'NGINX_CLIENT_MAX_BODY_SIZE': '40m',
                'NGINX_PROXY_READ_TIMEOUT_SECONDS': '5', 'ADMIN_UPSTREAM_HOST': '127.0.0.1', 'AI_ADMIN_HTTP_PORT': str(d.backend_port)}
            self.manager = WebsiteDomains(env)
            registry = {'version': 1, 'primary': d.domain, 'domains': rows}
            self.manager.save(registry)
            atomic(site, self.loopback(self.manager.render(registry, nonce=nonce,
                challenge=self.pending if http_stage else '')).encode())
            # This site belongs to other software; the production apply helper
            # must never replace it or acquire its server_name/default listener.
            self.foreign = d.directory / 'foreign.conf'
            self.foreign_bytes = (f'server {{ listen 127.0.0.1:{d.http_port}; server_name foreign.invalid; return 418; }}\n').encode()
            atomic(self.foreign, self.foreign_bytes)
            return f'include "{site.as_posix()}";\ninclude "{self.foreign.as_posix()}";'

        self.driver.extra_nginx_servers = included_sites
        self.driver.leading_nginx_servers = lambda: ('server_names_hash_bucket_size 128;\n' +
            (f'server {{ listen 127.0.0.1:{self.driver.http_port} default_server; server_name _; return 404; }}\n'
             if getattr(self, 'http_default_404', False) else ''))
        with patch('platform_admin.app.create_app', return_value=self.backend):
            self.driver.setUp()  # includes a REAL nginx -t, never a stub
        self.original = self.manager.site.read_bytes()
        self.manager.task = self.task
        self.manager.execute = self.execute
        self.manager.addresses = lambda domain: ['127.0.0.1']  # local-only DNS fixture
        self.manager.probe = self.probe

    def execute(self, args, **kwargs):
        if args[0] == 'nginx':
            args = [self.driver.nginx, '-p', self.driver.directory.as_posix() + '/',
                    '-c', str(self.driver.directory / 'nginx.conf'), *args[1:]]
        return subprocess.run(args, **kwargs)

    def request(self, domain, path, *, https=True, method='GET', **kwargs):
        context = ssl.create_default_context()
        for cert in (self.driver.directory / 'certbot/live').glob('*/fullchain.pem'):
            context.load_verify_locations(cafile=str(cert))
        original = socket.getaddrinfo
        with patch('socket.getaddrinfo', side_effect=lambda host, *a, **kw: original('127.0.0.1' if host.endswith('.example.test') else host, *a, **kw)):
            scheme, port = ('https', self.driver.https_port) if https else ('http', self.driver.http_port)
            with httpx.Client(verify=context, trust_env=False, follow_redirects=False, timeout=5) as client:
                return client.request(method, f'{scheme}://{domain}:{port}' + path, **kwargs)

    def wait_check(self, domain, nonce, *, https=True):
        deadline = time.monotonic() + 8
        while True:
            try:
                result = self.request(domain, '/.__platform-domain-check', https=https)
            except httpx.TransportError:
                # Reload is asynchronous: the retiring worker may briefly use
                # the old SNI table. Retry a bounded time, with TLS verification
                # ALWAYS enabled; do not bypass a wrong/missing certificate.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.03)
                continue
            if result.status_code == (200 if nonce else 404) and (not nonce or result.text == nonce):
                return
            if time.monotonic() >= deadline:
                self.fail('Isolated Nginx did not apply the expected domain-check configuration.')
            time.sleep(.03)

    def task(self, action, *args):
        self.calls.append((action, *args))
        if action == 'issue':
            if self.failure == 'issue':
                raise SiteError('fixture_certificate_failure')
            self.certificate(args[0])  # disposable CA, not Let's Encrypt
        elif action == 'apply':
            content = self.loopback(Path(args[0]).read_text(encoding='utf-8'))
            if self.failure == 'syntax':
                self.failure = ''  # rollback must use the valid original
                content += '\ninvalid_fixture_directive;\n'
            candidate = self.driver.directory / 'candidate.conf'
            atomic(candidate, content.encode())
            env = {**os.environ, 'INSTALLER_ROOT': ROOT.as_posix(), 'TEST_NGINX_BINARY': str(self.driver.nginx),
                'TEST_WEBSITE_PREFIX': self.driver.directory.as_posix(),
                'TEST_WEBSITE_DEFER_RELOAD': 'true' if getattr(self, 'defer_reload', False) else 'false',
                'NGINX_SITE_CONFIG_PATH': self.manager.site.as_posix(),
                'NGINX_SITES_AVAILABLE_DIR': self.manager.site.parent.as_posix(),
                'NGINX_SITES_ENABLED_DIR': (self.driver.directory / 'sites-enabled').as_posix(),
                'NGINX_SITE_ENABLED_PATH': (self.driver.directory / 'sites-enabled/platform.conf').as_posix()}
            bash = os.environ.get('TEST_BASH_BINARY') or ('C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else shutil.which('bash'))
            result = subprocess.run([bash, str(ROOT / 'tests/fixtures/website-nginx-apply.sh'), str(candidate)],
                env=env, capture_output=True, timeout=30)
            self.syntax_results.append(result.returncode)
            self.manager.nginx_events(result.stdout)
            if result.returncode:
                raise SiteError('fixture_apply_failure: ' + result.stderr.decode(errors='replace')[-1500:]
                    + result.stdout.decode(errors='replace')[-1500:])
        elif action != 'alias':
            self.fail('Unexpected maintenance operation: ' + action)

    def probe(self, domain, nonce, *, https=True):
        self.calls.append(('probe', domain, https))
        self.wait_check(domain, nonce, https=https)
        if https:
            if self.failure == 'https':
                raise SiteError('fixture_https_failure')
            self.assertEqual(self.request(domain, '/admin/login').status_code, 200)

    def assert_webhook(self, domain, conversation=100):
        incoming = self.f.protocol.visitor(conversation, 7, '域名验证后的合法访客消息')
        raw = json.dumps(self.f.protocol.webhook(incoming), ensure_ascii=False).encode()
        stamp = str(int(time.time()))
        bot = self.f.protocol.bots[self.f.bridge.binding('service_a')['bot_id']]
        signed = 'sha256=' + hmac.new(bot['secret'].encode(), stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        path = '/integrations/chatwoot/service_a/webhook'
        headers = {'x-chatwoot-timestamp': stamp, 'x-chatwoot-signature': signed, 'content-type': 'application/json'}
        self.assertEqual(self.request(domain, path, method='POST', content=raw, headers=headers).status_code, 200)
        self.assertEqual(self.request(domain, path, method='POST', content=raw,
            headers={**headers, 'x-chatwoot-signature': 'sha256=' + '0' * 64}).status_code, 401)
        self.assertEqual(self.request(domain, path).status_code, 403)  # POST-only
        self.f.tick()
        self.assertEqual(self.f.protocol.sent[-1]['conversation_id'], conversation)

    def test_http_stage_and_multiple_https_vhosts_with_runtime_nonce(self):
        nonce = secrets.token_hex(24)
        self.start(nonce=nonce, http_stage=True)
        self.wait_check(self.pending, nonce, https=False)
        for i, domain in enumerate(self.domains):
            self.wait_check(domain, nonce)
            self.assertEqual(self.request(domain, '/admin/login').status_code, 200)
            self.assertEqual(self.request(domain, '/support/session.js').status_code, 200)
            self.assert_webhook(domain, 100 + i)
        content = self.manager.site.read_text(encoding='utf-8')
        self.assertEqual(content.count('location = /.__platform-domain-check'), 4)
        self.assertEqual(content.count('client_max_body_size 1m;'), 3)

    def test_no_nonce_has_no_check_even_with_http_only_certificate_fallback(self):
        self.start(http_stage=True, lost_certificate=True)
        self.assertNotIn('location = /.__platform-domain-check', self.manager.site.read_text(encoding='utf-8'))
        for domain in self.domains:
            self.wait_check(domain, '')
        for domain in (self.pending, self.lost):
            self.wait_check(domain, '', https=False)

    def test_real_apply_add_cleanup_repeat_and_webhook_preserved(self):
        self.start()
        self.syntax_results = []
        self.manager.operation('add', self.pending)
        self.assertIn(self.pending, [row['domain'] for row in self.manager.read()['domains']])
        http = self.calls.index(('probe', self.pending, False))
        issue = self.calls.index(('issue', self.pending))
        https = self.calls.index(('probe', self.pending, True))
        alias = self.calls.index(('alias', self.pending))
        self.assertLess(http, issue); self.assertLess(issue, https); self.assertLess(https, alias)
        self.wait_check(self.pending, '')
        self.assert_webhook(self.pending)
        self.manager.operation('reconcile')
        self.wait_check(self.domains[0], '')
        self.assertEqual(self.foreign.read_bytes(), self.foreign_bytes)
        self.assertTrue(self.syntax_results and all(code == 0 for code in self.syntax_results))
        self.assertFalse((self.manager.directory / 'website-pending.json').exists())

    def test_real_syntax_cert_and_https_failure_restore_running_site_and_registry(self):
        self.start()
        before = deepcopy(self.manager.read())
        self.syntax_results = []
        for i, failure in enumerate(('syntax', 'issue', 'https')):
            with self.subTest(failure=failure):
                self.calls.clear()
                self.failure = failure
                with self.assertRaises(SiteError):
                    self.manager.operation('add', self.pending)
                self.failure = ''
                self.assertEqual(self.manager.site.read_bytes(), self.original)
                self.assertEqual(self.manager.read(), before)
                self.assertEqual(self.foreign.read_bytes(), self.foreign_bytes)
                self.assertFalse((self.manager.directory / 'website-pending.json').exists())
                self.wait_check(self.domains[0], '')
                self.assert_webhook(self.domains[0], 200 + i)
                if failure == 'syntax':
                    self.assertFalse(any(call[0] == 'issue' for call in self.calls))
        self.assertTrue(any(code != 0 for code in self.syntax_results))


if __name__ == '__main__':
    unittest.main()
