#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/nginx-https-fixture.XXXXXX")"
readonly SITE_CONFIG="${TEST_TEMP_ROOT}/nginx/sites-available/ai-customer-platform-chatwoot"
readonly SITE_ENABLED="${TEST_TEMP_ROOT}/nginx/sites-enabled/ai-customer-platform-chatwoot"
readonly ORIGINAL_HTTP_MARKER='proxy_pass http://127.0.0.1:3000;'

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/nginx-https-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${TEST_TEMP_ROOT}/nginx.log"
export CERTBOT_FIXTURE_LOG="${TEST_TEMP_ROOT}/certbot.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"
export CERTBOT_WORK_DIR="${TEST_TEMP_ROOT}/certbot-work"
export CERTBOT_LOGS_DIR="${TEST_TEMP_ROOT}/certbot-logs"

INSTALLER_ROOT="${TEST_ROOT}"
CONFIG_FILE="${TEST_ROOT}/tests/fixtures/chatwoot.test.env"
CONFIG_LOADED="true"
export INSTALLER_ROOT CONFIG_FILE CONFIG_LOADED

source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/modules/nginx/install.sh"
source "${TEST_ROOT}/modules/nginx/configure.sh"
source "${TEST_ROOT}/modules/domain/check.sh"
source "${TEST_ROOT}/modules/domain/ssl.sh"

config_load_file "${CONFIG_FILE}"
nginx_configure
domain_initialize_config
domain_request_certificate
domain_install_renew_hook
domain_activate_https

for expected_directive in \
  'listen 443 ssl;' \
  'server_name chatwoot.example.test;' \
  'ssl_protocols TLSv1.2 TLSv1.3;' \
  'return 301 https://$host$request_uri;' \
  'proxy_set_header X-Forwarded-Proto $scheme;' \
  'proxy_set_header Upgrade $http_upgrade;' \
  'proxy_set_header Connection "upgrade";' \
  "${ORIGINAL_HTTP_MARKER}"; do
  grep -Fq -- "${expected_directive}" "${SITE_CONFIG}"
done

export NGINX_FIXTURE_HTTPS_FAILURE="true"
printf '%s\n' '# Managed by AI Customer Platform Installer.' "${ORIGINAL_HTTP_MARKER}" >"${SITE_CONFIG}"
cp -- "${SITE_CONFIG}" "${SITE_ENABLED}"
if domain_activate_https; then
  printf '%s\n' 'HTTPS activation unexpectedly succeeded with invalid Nginx syntax.' >&2
  exit 1
fi

if ! grep -Fq -- "${ORIGINAL_HTTP_MARKER}" "${SITE_CONFIG}" || grep -Fq -- 'listen 443 ssl;' "${SITE_CONFIG}"; then
  printf '%s\n' 'The previous HTTP configuration was not restored after HTTPS validation failed.' >&2
  exit 1
fi

printf '%s\n' 'Nginx HTTPS configuration fixture test passed.'
