"""Deterministic finite deadlines, all-address probes and secret-free reports."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from platform_admin.site_domains import WebsiteDomains, SiteError
from platform_admin.site_diagnostics import SiteDiagnostics
import website_domains_test as lifecycle


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        assert 0 < seconds <= .25
        self.value += seconds


class ProbeFixtures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='website-probe-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.host = lifecycle.Host(self.directory)
        self.manager = WebsiteDomains(self.host.env, self.execute)
        self.manager.PROBE_READY_SECONDS = 1.0
        self.manager.PROBE_INTERVAL_SECONDS = .25
        self.domain = 'backup.example.test'
        self.nonce = 'a-cryptographic-probe-value-not-for-logs'
        self.clock = Clock()
        self.addCleanup(patch.stopall)
        patch('platform_admin.site_domains.time.monotonic', self.clock.now).start()
        patch('platform_admin.site_domains.time.sleep', self.clock.sleep).start()
        self.addresses = ['203.0.113.7', '2001:db8::7']
        self.manager.addresses = lambda domain: list(self.addresses)
        self.manager.diagnostics = SiteDiagnostics(self.manager.directory, 'add', self.domain)
        self.calls = []
        self.responses = lambda address, count: (0, 200, self.nonce.encode())

    def execute(self, args, **kwargs):
        address = args[args.index('--resolve') + 1].split(':', 2)[2].strip('[]')
        self.calls.append((address, args, kwargs))
        count = sum(ip == address for ip, _, _ in self.calls)
        result = self.responses(address, count)
        if isinstance(result, Exception):
            raise result
        code, status, body = result
        return subprocess.CompletedProcess(args, code, body + b'\n' + str(status).zfill(3).encode())

    def report(self):
        return json.loads(self.manager.diagnostics.path.read_text())

    def test_immediate_success_checks_local_then_all_a_aaaa_and_strict_transport(self):
        self.manager.probe(self.domain, self.nonce, https=False)
        self.assertEqual([ip for ip, _, _ in self.calls], ['127.0.0.1', *self.addresses])
        for address, args, options in self.calls:
            self.assertIn('--noproxy', args)
            self.assertEqual(args[-1], 'http://' + self.domain + ':80/.__platform-domain-check')
            self.assertNotIn('-k', args); self.assertNotIn('--insecure', args); self.assertNotIn('--location', args)
            self.assertLessEqual(options['timeout'], 3.25)

    def test_first_404_then_correct_nonce_is_not_a_failure(self):
        self.responses = lambda ip, count: (0, 404, b'not found') if ip == '127.0.0.1' and count == 1 else (0, 200, self.nonce.encode())
        self.manager.probe(self.domain, self.nonce, https=False)
        self.assertEqual(self.clock.value, .25)
        attempts = [e for e in self.report()['events'] if e['status'] == 'attempt' and 'http_status' in e]
        self.assertEqual([e['http_status'] for e in attempts], [404, 200, 200, 200])
        self.assertFalse(attempts[0]['nonce_match'])

    def test_transient_connection_failure_and_subprocess_timeout_retry_safely(self):
        self.responses = lambda ip, count: ((7, 0, b'') if count == 1 else
            subprocess.TimeoutExpired('curl', .1, output=b'sensitive-must-not-be-logged') if count == 2 else (0, 200, self.nonce.encode())) if ip == '127.0.0.1' else (0, 200, self.nonce.encode())
        self.manager.probe(self.domain, self.nonce, https=False)
        self.assertEqual(self.clock.value, .5)
        self.assertNotIn('sensitive-must-not-be-logged', self.manager.diagnostics.path.read_text())

    def test_permanent_404_times_out_once_for_whole_probe_not_per_address(self):
        self.responses = lambda ip, count: (0, 404, b'not found')
        with self.assertRaisesRegex(SiteError, '^domain_http_local_timeout$'):
            self.manager.probe(self.domain, self.nonce, https=False)
        self.assertEqual(self.clock.value, 1.0)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.report()['events'][-1]['stage'], 'http_local')

    def test_wrong_nonce_and_500_with_matching_body_never_pass(self):
        for status, body in ((200, b'wrong'), (500, self.nonce.encode()), (302, self.nonce.encode())):
            with self.subTest(status=status):
                self.responses = lambda ip, count: (0, status, body)
                with self.assertRaisesRegex(SiteError, '^domain_http_local_timeout$'):
                    self.manager.probe(self.domain, self.nonce, https=False)

    def test_successful_local_probe_cannot_hide_unreachable_public_or_bad_aaaa(self):
        self.responses = lambda ip, count: (0, 404, b'not found') if ip == self.addresses[-1] else (0, 200, self.nonce.encode())
        with self.assertRaisesRegex(SiteError, '^domain_http_public_timeout$'):
            self.manager.probe(self.domain, self.nonce, https=False)
        report = self.report()
        self.assertTrue(any(e['stage'] == 'http_local' and e['status'] == 'ok' for e in report['events']))
        self.assertEqual(report['events'][-1]['address'], self.addresses[-1])
        self.assertEqual(self.clock.value, 1.0)

    def test_https_tls_failure_never_falls_back_to_http_or_insecure_curl(self):
        self.responses = lambda ip, count: (60, 0, b'')
        with self.assertRaisesRegex(SiteError, '^domain_https_local_timeout$'):
            self.manager.probe(self.domain, self.nonce)
        self.assertTrue(all(args[-1].startswith('https://') and '-k' not in args and '--insecure' not in args for _, args, _ in self.calls))
        self.assertTrue(any(e['code'] == 'tls_verification_failed' for e in self.report()['events']))

    def test_reports_redact_nonce_keys_cookies_commands_and_arbitrary_errors(self):
        self.manager.probe(self.domain, self.nonce, https=False)
        secret = 'sk-private-credential-do-not-record'
        self.manager.event('save', 'start', command=secret, response=self.nonce, cookie=secret, token=secret)
        with self.assertRaises(SiteError):
            with self.manager.stage('save'):
                raise SiteError(secret)
        self.manager.diagnostics.finish(False, SiteError(secret))
        text = self.manager.diagnostics.path.read_text()
        self.assertNotIn(secret, text); self.assertNotIn(self.nonce, text)
        self.assertEqual(self.report()['error_code'], 'site_save_failed')
        if os.name != 'nt':
            self.assertEqual(self.manager.diagnostics.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.manager.diagnostics.directory.stat().st_mode & 0o777, 0o700)

    def test_report_rotation_and_storage_failure_do_not_block_rollback(self):
        for i in range(34):
            SiteDiagnostics(self.manager.directory, 'reconcile').finish(True)
        self.assertEqual(len(list((self.manager.directory / 'website-diagnostics').glob('*.json'))), 32)
        self.host.fail = 'http'
        original = self.host.site.read_bytes()
        with self.assertRaises(SiteError):
            self.host.operation('add', self.domain)
        self.assertEqual(self.host.site.read_bytes(), original)
        latest = max((self.manager.directory / 'website-diagnostics').glob('*.json'), key=lambda p: p.stat().st_mtime_ns)
        report = json.loads(latest.read_text())
        self.assertTrue(any(e['stage'] == 'rollback' and e['status'] == 'ok' for e in report['events']))
        trace = SiteDiagnostics(self.manager.directory, 'recover')
        with patch('platform_admin.site_domains.atomic', side_effect=OSError('full disk')):
            trace.record('rollback', 'ok')  # diagnostic I/O must not raise
        self.assertTrue(trace.write_failed)

    def test_symlinked_diagnostic_directory_is_rejected(self):
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda p: p.name == 'website-diagnostics' or original(p)):
            with self.assertRaisesRegex(SiteError, '^unsafe_site_storage$'):
                SiteDiagnostics(self.manager.directory, 'add')


if __name__ == '__main__':
    unittest.main()
