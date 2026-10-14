#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/domain-fixture.XXXXXX")"
readonly DOMAIN_CALL_LOG="${TEST_TEMP_ROOT}/domain.log"
readonly CERTBOT_CALL_LOG="${TEST_TEMP_ROOT}/certbot.log"
readonly NGINX_CALL_LOG="${TEST_TEMP_ROOT}/nginx.log"
readonly SITE_CONFIG="${TEST_TEMP_ROOT}/nginx/sites-available/ai-customer-platform-chatwoot"
readonly CERTIFICATE_DIR="${TEST_TEMP_ROOT}/letsencrypt/live/chatwoot.example.test"
readonly RENEW_HOOK="${TEST_TEMP_ROOT}/letsencrypt/renewal-hooks/deploy/ai-customer-platform-nginx-reload"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/domain-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_TEMP_ROOT}/.env"
cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${CONFIG_FILE}"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${NGINX_CALL_LOG}"
export DOMAIN_FIXTURE_LOG="${DOMAIN_CALL_LOG}"
export CERTBOT_FIXTURE_LOG="${CERTBOT_CALL_LOG}"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"
export CERTBOT_WORK_DIR="${TEST_TEMP_ROOT}/certbot-work"
export CERTBOT_LOGS_DIR="${TEST_TEMP_ROOT}/certbot-logs"
export SYSTEM_OS_RELEASE_FILE="${TEST_ROOT}/tests/fixtures/os-release.ubuntu-24.04"
export SYSTEM_NETWORK_TIMEOUT_SECONDS="2"

"${TEST_ROOT}/install.sh"

grep -Fq -- 'getent ahosts chatwoot.example.test' "${DOMAIN_CALL_LOG}"
grep -Fq -- 'server_name chatwoot.example.test;' "${SITE_CONFIG}"
grep -Fq -- 'listen 443 ssl;' "${SITE_CONFIG}"
grep -Fq -- 'return 301 https://$host$request_uri;' "${SITE_CONFIG}"
grep -Fq -- "ssl_certificate ${CERTIFICATE_DIR}/fullchain.pem;" "${SITE_CONFIG}"
grep -Fq -- "ssl_certificate_key ${CERTIFICATE_DIR}/privkey.pem;" "${SITE_CONFIG}"

[[ -s "${CERTIFICATE_DIR}/fullchain.pem" ]]
[[ -s "${CERTIFICATE_DIR}/privkey.pem" ]]
[[ -x "${RENEW_HOOK}" ]]
grep -Fq -- '# Managed by AI Customer Platform Installer domain module.' "${RENEW_HOOK}"

if grep -Rqs -- 'chatwoot.example.test\|certificates@example.test' "${TEST_ROOT}/templates/domain"; then
  printf '%s\n' 'Domain templates must not contain fixture domains or email addresses.' >&2
  exit 1
fi

printf '%s\n' 'Domain module fixture test passed.'
