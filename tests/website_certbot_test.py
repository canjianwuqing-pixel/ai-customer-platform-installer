"""Renewal deadline/process tests; no CA, production paths or real keys are used."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from platform_admin.site_domains import SiteError, WebsiteDomains
from website_domains_test import Host


ROOT = Path(__file__).resolve().parents[1]
PROCESS = ROOT / 'tests/fixtures/website-certbot-process.py'
ISSUE = r'''
source "$1"
domain_initialize_config() { :; }
validation_is_hostname() { :; }
run_as_root() { :; }
domain_install_renew_hook() { :; }
domain_enable_automatic_renewal() { :; }
domain_certbot() { "$TEST_CERTBOT_PYTHON" "$TEST_CERTBOT_PROCESS" certbot "$TEST_CERTBOT_DIRECTORY" "$@"; }
CERTBOT_WORK_DIR="$TEST_CERTBOT_DIRECTORY"
LETSENCRYPT_EMAIL=certificates@example.test
CERTBOT_RENEW_DRY_RUN=true
website_task issue backup.example.test
'''


def running(pid):
    try:
        # A terminated orphan may briefly await init's reap on Linux.
        stat = Path('/proc') / str(pid) / 'stat'
        if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            return False
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RenewalContractFixtures(unittest.TestCase):
    def test_bounded_alias_dry_run_disables_random_sleep_without_disabling_validation(self):
        source = (ROOT / 'modules/domain/websites.sh').read_text(encoding='utf-8')
        self.assertIn('domain_certbot renew --dry-run --no-random-sleep-on-renew --cert-name "$1"', source)
        self.assertIn('if [[ "${CERTBOT_RENEW_DRY_RUN}" == true ]]', source)
        self.assertIn('CERTBOT_RENEW_DRY_RUN=true', (ROOT / 'config/default.env').read_text())
        self.assertIn('--keep-until-expiring', source)

    def test_command_input_output_and_exit_status_keep_existing_contract(self):
        from platform_admin.site_process import run_site_command
        result = run_site_command([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'],
                                  input=b'isolated nginx candidate', timeout=5,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'isolated nginx candidate')
        result = run_site_command([sys.executable, '-c', 'raise SystemExit(23)'], timeout=5,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
        self.assertEqual(result.returncode, 23)


@unittest.skipUnless(os.name == 'posix', 'Requires POSIX sessions/signals; execute on Ubuntu or a POSIX test runtime.')
class WebsiteProcessFixtures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='website-certbot-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.host = Host(self.directory)
        self.manager = WebsiteDomains(self.host.env)
        execute = self.manager.execute
        self.manager.execute = lambda args, **options: execute(args, **{**options, 'timeout': 3.0})
        # Register after directory cleanup so fixture-owned PIDs are stopped first.
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        # PID files are written only by our fixture. Never kill a process group
        # here: the original implementation shares the test runner's group.
        for path in self.directory.glob('*.pid'):
            pid = int(path.read_text())
            if running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # An asynchronously terminating fixture exited.

    def assert_no_live_processes(self):
        for path in self.directory.glob('*.pid'):
            self.assertFalse(running(int(path.read_text())), path.name)

    def test_random_delay_is_bypassed_and_existing_certificate_is_preserved(self):
        env = dict(os.environ, TEST_CERTBOT_PYTHON=sys.executable, TEST_CERTBOT_PROCESS=str(PROCESS),
                   TEST_CERTBOT_DIRECTORY=str(self.directory))
        for name in ('fullchain.pem', 'privkey.pem'):
            (self.directory / name).write_bytes(('existing-fixture-' + name).encode())
        before = {p.name: p.read_bytes() for p in self.directory.glob('*.pem')}
        # The deadline runner still enforces a timeout; only unnecessary random
        # delay is removed. The simulated 368-second wait is scaled to 5 seconds.
        execute = self.manager.execute
        self.manager.execute = lambda args, **options: execute(args, **options, env=env)
        for _ in range(2):
            self.manager.command(['/bin/bash', '-c', ISSUE, 'fixture', str(ROOT / 'modules/domain/websites.sh')])
        calls = [json.loads(line) for line in (self.directory / 'calls.jsonl').read_text().splitlines()]
        renewals = [call for call in calls if call[0] == 'renew']
        self.assertEqual(len(renewals), 2)
        for call in renewals:
            self.assertEqual(call, ['renew', '--dry-run', '--no-random-sleep-on-renew', '--cert-name', 'backup.example.test'])
        self.assertFalse((self.directory / 'random-delay').exists())
        self.assertTrue((self.directory / 'renew-passed').exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.glob('*.pem')})

    def test_renewal_failure_is_not_accepted_or_followed_by_forced_reissuance(self):
        env = dict(os.environ, TEST_CERTBOT_PYTHON=sys.executable, TEST_CERTBOT_PROCESS=str(PROCESS),
                   TEST_CERTBOT_DIRECTORY=str(self.directory), TEST_CERTBOT_RENEW_FAILURE='true')
        execute = self.manager.execute
        self.manager.execute = lambda args, **options: execute(args, **options, env=env)
        with self.assertRaises(SiteError):
            self.manager.command(['/bin/bash', '-c', ISSUE, 'fixture', str(ROOT / 'modules/domain/websites.sh')])
        calls = [json.loads(line) for line in (self.directory / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual([call[0] for call in calls], ['certonly', 'renew'])
        self.assertFalse((self.directory / 'renew-passed').exists())
        self.assertNotIn('--force-renewal', str(calls))
        self.assertNotIn('revoke', str(calls))
        self.assertNotIn('delete', str(calls))

    def test_simulated_368_second_random_wait_times_out_without_late_renewal(self):
        # Deliberately omit the new flag in a stand-in command to exercise a
        # random-wait timeout independently of the fixed website_task arguments.
        certificate = self.directory / 'fullchain.pem'
        certificate.write_bytes(b'previously issued fixture certificate')
        with self.assertRaisesRegex(SiteError, '^site_task_timeout$'):
            self.manager.command([sys.executable, str(PROCESS), 'certbot', str(self.directory),
                                  'renew', '--dry-run', '--cert-name', 'backup.example.test'])
        self.assertIn('368', (self.directory / 'random-delay').read_text())
        self.assert_no_live_processes()
        self.assertFalse((self.directory / 'renew-passed').exists())
        self.assertEqual(certificate.read_bytes(), b'previously issued fixture certificate')

    def test_timeout_reaps_launcher_and_cooperative_children_before_return(self):
        with self.assertRaises(SiteError):
            self.manager.command([sys.executable, str(PROCESS), 'cooperative-parent', str(self.directory)])
        self.assertTrue((self.directory / 'cooperative-leaf.ready').exists())
        self.assert_no_live_processes()
        for role in ('parent', 'child', 'leaf'):
            self.assertTrue((self.directory / ('cooperative-' + role + '.stopped')).exists())

    def test_timeout_kills_term_ignoring_descendants_before_nginx_rollback(self):
        original = self.host.site.read_bytes()
        old_apply, old_task = self.host.apply, self.host.task
        rollback_checks = []
        issued = Path(self.host.env['CERTBOT_CONFIG_DIR']) / 'live' / 'backup.example.test'
        certificates = {issued / 'fullchain.pem': b'newly issued fixture certificate',
                        issued / 'privkey.pem': b'newly issued fixture private key',
                        Path(self.host.env['CERTBOT_FULLCHAIN_PATH']): b'original main fixture certificate',
                        Path(self.host.env['CERTBOT_PRIVATE_KEY_PATH']): b'original main fixture private key'}
        for path, content in certificates.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        def task(action, *args):
            if action == 'issue':
                self.manager.command([sys.executable, str(PROCESS), 'stubborn-parent', str(self.directory)])
            else:
                old_task(action, *args)

        def apply(content):
            if content.encode() == original:
                self.assert_no_live_processes()
                rollback_checks.append(True)
            old_apply(content)

        self.host.task, self.host.apply = task, apply
        with self.assertRaises(SiteError):
            self.host.operation('add', 'backup.example.test')
        self.assertTrue((self.directory / 'stubborn-leaf.ready').exists())
        self.assert_no_live_processes()
        self.assertEqual(rollback_checks, [True])
        self.assertEqual(self.host.site.read_bytes(), original)
        self.assertFalse(self.host.path.exists())
        self.assertFalse((self.host.directory / 'website-pending.json').exists())
        self.assertEqual({path: path.read_bytes() for path in certificates}, certificates)
        report = json.loads(next((self.host.directory / 'website-diagnostics').glob('*.json')).read_text())
        self.assertEqual(report['failed_stage'], 'certbot')
        self.assertEqual(report['error_code'], 'certbot_timeout')

    def test_launcher_exit_and_closed_pipes_do_not_leave_stubborn_descendants(self):
        with self.assertRaisesRegex(SiteError, '^site_task_timeout$'):
            self.manager.command([sys.executable, str(PROCESS), 'exiting-parent', str(self.directory)])
        self.assertTrue((self.directory / 'exiting-parent.stopped').exists())
        self.assertTrue((self.directory / 'stubborn-leaf.ready').exists())
        self.assert_no_live_processes()

    def test_timeout_does_not_terminate_an_unrelated_process_group(self):
        unrelated = subprocess.Popen([sys.executable, str(PROCESS), 'cooperative-leaf', str(self.directory / 'unrelated')],
                                     start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def cleanup():
            if unrelated.poll() is None:
                unrelated.terminate()
                try:
                    unrelated.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    unrelated.kill()
                    unrelated.wait()

        self.addCleanup(cleanup)
        with self.assertRaises(SiteError):
            self.manager.command([sys.executable, str(PROCESS), 'stubborn-parent', str(self.directory)])
        self.assert_no_live_processes()
        self.assertIsNone(unrelated.poll())


if __name__ == '__main__':
    unittest.main()
