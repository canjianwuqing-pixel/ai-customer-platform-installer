#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/nginx-fixture.XXXXXX")"
readonly NGINX_CALL_LOG="${TEST_TEMP_ROOT}/nginx.log"
readonly SITE_NAME="ai-customer-platform-chatwoot"
readonly SITE_CONFIG="${TEST_TEMP_ROOT}/nginx/sites-available/${SITE_NAME}"
readonly SITE_ENABLED="${TEST_TEMP_ROOT}/nginx/sites-enabled/${SITE_NAME}"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/nginx-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_ROOT}/tests/fixtures/chatwoot.test.env"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${NGINX_CALL_LOG}"
export DOMAIN_FIXTURE_LOG="${TEST_TEMP_ROOT}/domain.log"
export CERTBOT_FIXTURE_LOG="${TEST_TEMP_ROOT}/certbot.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"
export CERTBOT_WORK_DIR="${TEST_TEMP_ROOT}/certbot-work"
export CERTBOT_LOGS_DIR="${TEST_TEMP_ROOT}/certbot-logs"
export SYSTEM_OS_RELEASE_FILE="${TEST_ROOT}/tests/fixtures/os-release.ubuntu-24.04"
export SYSTEM_NETWORK_TIMEOUT_SECONDS="2"

"${TEST_ROOT}/install.sh"

if [[ ! -f "${SITE_CONFIG}" ]]; then
  printf 'Generated Nginx site configuration is missing: %s\n' "${SITE_CONFIG}" >&2
  exit 1
fi

if [[ ! -e "${SITE_ENABLED}" && ! -L "${SITE_ENABLED}" ]]; then
  printf 'Generated Nginx site is not enabled: %s\n' "${SITE_ENABLED}" >&2
  exit 1
fi

for expected_directive in \
  'server_name chatwoot.example.test;' \
  'proxy_pass http://127.0.0.1:3000;' \
  'proxy_set_header Host $host;' \
  'proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;' \
  'proxy_set_header X-Forwarded-Proto $scheme;' \
  'location /cable {' \
  'proxy_set_header Upgrade $http_upgrade;' \
  'proxy_set_header Connection "upgrade";'; do
  if ! grep -Fq -- "${expected_directive}" "${SITE_CONFIG}"; then
    printf 'Generated Nginx config is missing: %s\n' "${expected_directive}" >&2
    exit 1
  fi
done

if grep -Fq 'chatwoot.example.test' "${TEST_ROOT}/templates/nginx/chatwoot.conf.template"; then
  printf '%s\n' 'Nginx template must not contain the fixture domain.' >&2
  exit 1
fi

syntax_test_line="$(grep -nF 'nginx -t' "${NGINX_CALL_LOG}" | head -n 1 | cut -d: -f1)"
reload_line="$(grep -nF 'systemctl reload nginx' "${NGINX_CALL_LOG}" | head -n 1 | cut -d: -f1)"
if [[ -z "${syntax_test_line}" || -z "${reload_line}" ]] || ((syntax_test_line >= reload_line)); then
  printf '%s\n' 'Nginx must pass syntax validation before reload.' >&2
  exit 1
fi

printf '%s\n' 'Nginx Ubuntu fixture test passed.'
