#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/docker-fixture.XXXXXX")"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/docker-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_ROOT}/tests/fixtures/chatwoot.test.env"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${TEST_TEMP_ROOT}/nginx.log"
export DOMAIN_FIXTURE_LOG="${TEST_TEMP_ROOT}/domain.log"
export CERTBOT_FIXTURE_LOG="${TEST_TEMP_ROOT}/certbot.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"
export CERTBOT_WORK_DIR="${TEST_TEMP_ROOT}/certbot-work"
export CERTBOT_LOGS_DIR="${TEST_TEMP_ROOT}/certbot-logs"
export SYSTEM_OS_RELEASE_FILE="${TEST_ROOT}/tests/fixtures/os-release.ubuntu-24.04"
export SYSTEM_NETWORK_TIMEOUT_SECONDS="2"

"${TEST_ROOT}/install.sh"
