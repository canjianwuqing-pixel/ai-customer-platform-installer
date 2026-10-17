"""Host-only, allow-listed diagnostics. Never serialize commands or responses."""
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import time


STAGES = set('operation dns nginx_staging nginx_test nginx_reload nginx_inspect http_local http_public certbot certificate https_local https_public admin static_assets websocket primary alias retire save rollback recovery'.split())
CODES = set('ok site_operation_failed invalid_site_template unsafe_site_storage domain_dns_failed domain_access_failed certificate_invalid restore_certificate_invalid nginx_test_failed nginx_reload_failed nginx_apply_failed nginx_inspect_failed nginx_site_not_included nginx_server_name_missing nginx_server_name_conflict domain_http_local_timeout domain_http_public_timeout domain_https_local_timeout domain_https_public_timeout domain_probe_failed domain_probe_timeout domain_nonce_mismatch tls_verification_failed chatwoot_assets_missing chatwoot_cross_origin_asset chatwoot_assets_unavailable chatwoot_websocket_unavailable certbot_failed site_save_failed'.split())
CODES.update('invalid_domain invalid_site_registry invalid_certificate_path invalid_domain_nonce operation_not_allowed domain_already_exists domain_not_found primary_domain_protected domain_limit managed_site_required'.split())
CODES.update('site_task_timeout certbot_timeout'.split())


def error_code(error):
    value = str(error)
    return value if value in CODES else 'site_operation_failed'


class SiteDiagnostics:
    """0600 JSON, 0700 directory, last 32 operations; no web/UI reader."""
    def __init__(self, state_dir, action, domain=''):
        from .site_domains import atomic, SiteError, hostname
        state = Path(state_dir)
        self.directory = state / 'website-diagnostics'
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise SiteError('unsafe_site_storage')
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if os.name != 'nt' and any(path.stat().st_mode & 0o077 for path in (state, self.directory)):
            raise SiteError('unsafe_site_storage')
        # Only our own regular report files can be rotated, never symlinks.
        reports = [p for p in self.directory.iterdir() if re.fullmatch(r'[0-9a-f]{32}\.json', p.name) and p.is_file() and not p.is_symlink()]
        for path in sorted(reports, key=lambda p: p.stat().st_mtime)[:-31]:
            path.unlink()
        run_id = secrets.token_hex(16)  # correlation ID, NOT the verification nonce
        self.path = self.directory / (run_id + '.json')
        self.started = time.monotonic()
        self.failed_stage = ''
        self.failed_code = ''
        self.write_failed = False
        try:
            domain = hostname(domain)
        except SiteError:
            domain = ''
        self.data = {'version': 1, 'id': run_id, 'operation': action if action in {'add', 'primary', 'delete', 'reconcile', 'restore-check', 'restore', 'recover'} else 'unknown',
                     'domain': domain,
                     'result': 'running', 'events': []}
        atomic(self.path, json.dumps(self.data).encode())

    def record(self, stage, status, code='ok', **fields):
        from .site_domains import atomic, SiteError
        if stage not in STAGES or status not in {'start', 'ok', 'error', 'attempt'} or code not in CODES:
            raise ValueError('invalid_diagnostic_event')
        event = {'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                 'elapsed_ms': round((time.monotonic() - self.started) * 1000), 'stage': stage, 'status': status, 'code': code}
        for key, value in fields.items():
            if key in {'http_status', 'curl_code', 'attempt', 'port'} and type(value) is int and 0 <= value <= 65535:
                event[key] = value
            elif key in {'nonce_match', 'site_included', 'server_name_present', 'name_conflict'} and type(value) is bool:
                event[key] = value
            elif key == 'address':
                try:
                    event[key] = str(ipaddress.ip_address(value))
                except ValueError:
                    pass
            elif key == 'addresses' and isinstance(value, list):
                try:
                    event[key] = [str(ipaddress.ip_address(ip)) for ip in value[:128]]
                except ValueError:
                    pass
        if status == 'error' and not self.failed_stage:
            self.failed_stage, self.failed_code = stage, code
        self.data['events'] = (self.data['events'] + [event])[-512:]
        try:
            atomic(self.path, json.dumps(self.data).encode())
        except (OSError, SiteError):
            # A full/read-only disk must NEVER prevent an existing site's rollback.
            self.write_failed = True

    def finish(self, success, error=None):
        self.data['result'] = 'ok' if success else 'failed'
        if not success:
            self.data['failed_stage'] = self.failed_stage or 'operation'
            self.data['error_code'] = self.failed_code or error_code(error)
        self.record('operation', 'ok' if success else 'error', 'ok' if success else error_code(error))
