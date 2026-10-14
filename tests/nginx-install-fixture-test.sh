#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/nginx-install-fixture.XXXXXX")"
readonly INSTALL_CALL_LOG="${TEST_TEMP_ROOT}/install.log"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/nginx-install-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

INSTALLER_ROOT="${TEST_ROOT}"
export INSTALLER_ROOT
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/modules/nginx/install.sh"

NGINX_SITE_NAME="ai-customer-platform-chatwoot"
NGINX_FIXTURE_INSTALLED="false"

nginx_is_installed() {
  [[ "${NGINX_FIXTURE_INSTALLED}" == "true" ]]
}

nginx() {
  if [[ "${1:-}" == "-v" ]]; then
    printf '%s\n' 'nginx version: nginx/1.24.0-install-fixture' >&2
    return 0
  fi
  return 1
}

current_user_is_root() {
  return 1
}

command_exists() {
  case "$1" in
    apt-get | sudo) return 0 ;;
    *) command -v "$1" >/dev/null 2>&1 ;;
  esac
}

run_as_root() {
  printf '%s\n' "$*" >>"${INSTALL_CALL_LOG}"
  if [[ "$*" == *'apt-get install -y --no-remove nginx'* ]]; then
    NGINX_FIXTURE_INSTALLED="true"
  fi
  return 0
}

nginx_install

update_line="$(grep -nFx 'apt-get update' "${INSTALL_CALL_LOG}" | cut -d: -f1)"
install_line="$(grep -nF 'apt-get install -y --no-remove nginx' "${INSTALL_CALL_LOG}" | cut -d: -f1)"
if [[ -z "${update_line}" || -z "${install_line}" ]] || ((update_line >= install_line)); then
  printf '%s\n' 'Nginx package installation commands were missing or out of order.' >&2
  exit 1
fi

if [[ "${NGINX_FIXTURE_INSTALLED}" != "true" ]]; then
  printf '%s\n' 'Nginx installation fixture did not reach the installed state.' >&2
  exit 1
fi

printf '%s\n' 'Nginx installation fixture test passed.'
