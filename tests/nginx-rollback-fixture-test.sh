#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/nginx-rollback-fixture.XXXXXX")"
readonly NGINX_CALL_LOG="${TEST_TEMP_ROOT}/nginx.log"
readonly SITE_NAME="ai-customer-platform-chatwoot"
readonly SITE_CONFIG="${TEST_TEMP_ROOT}/nginx/sites-available/${SITE_NAME}"
readonly SITE_ENABLED="${TEST_TEMP_ROOT}/nginx/sites-enabled/${SITE_NAME}"
readonly ORIGINAL_CONFIG=$'# Managed by AI Customer Platform Installer.\n# existing fixture configuration'

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/nginx-rollback-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

mkdir -p -- "$(dirname -- "${SITE_CONFIG}")" "$(dirname -- "${SITE_ENABLED}")"
printf '%s\n' "${ORIGINAL_CONFIG}" >"${SITE_CONFIG}"

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_ROOT}/tests/fixtures/chatwoot.test.env"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${NGINX_CALL_LOG}"
export NGINX_FIXTURE_TEST_FAILURE="true"
export SYSTEM_OS_RELEASE_FILE="${TEST_ROOT}/tests/fixtures/os-release.ubuntu-24.04"
export SYSTEM_NETWORK_TIMEOUT_SECONDS="2"

if "${TEST_ROOT}/install.sh"; then
  printf '%s\n' 'Installer unexpectedly succeeded with a failing Nginx syntax test.' >&2
  exit 1
fi

if [[ "$(<"${SITE_CONFIG}")" != "${ORIGINAL_CONFIG}" ]]; then
  printf '%s\n' 'Existing Nginx site configuration was not restored after failure.' >&2
  exit 1
fi

if [[ -e "${SITE_ENABLED}" || -L "${SITE_ENABLED}" ]]; then
  printf '%s\n' 'Failed Nginx configuration left a new enabled-site link behind.' >&2
  exit 1
fi

if grep -Fq 'systemctl reload nginx' "${NGINX_CALL_LOG}"; then
  printf '%s\n' 'Nginx was reloaded despite a failed syntax check.' >&2
  exit 1
fi

printf '%s\n' 'Nginx rollback fixture test passed.'
