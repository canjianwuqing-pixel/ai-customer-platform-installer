#!/usr/bin/env bash
set -Eeuo pipefail

# Invoke the real install.sh entry point, configuration loading, module
# registration and completion. Only host/network/Docker commands are stubbed.
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "${TEST_ROOT}/runtime/tests"
readonly TEST_TEMP="$(mktemp -d "${TEST_ROOT}/runtime/tests/admin-installer.XXXXXX")"
readonly TEST_PROJECT="${TEST_TEMP}/project"
cleanup() {
  local result=$? resolved
  if ((result != 0)); then
    printf 'Failed Admin installer fixture retained at %s\n' "${TEST_TEMP}" >&2
    return 0
  fi
  resolved="$(cd -- "${TEST_TEMP}" && pwd -P)"
  case "${resolved}" in
    "${TEST_ROOT}"/runtime/tests/admin-installer.*) rm -rf -- "${resolved}" ;;
    *) printf '%s\n' 'Unsafe fixture cleanup path.' >&2 ;;
  esac
}
trap cleanup EXIT
trap 'printf "Admin installer fixture failed at line %s.\n" "${LINENO}" >&2' ERR

mkdir -p -- "${TEST_PROJECT}/config/services" "${TEST_PROJECT}/config/domains" "${TEST_PROJECT}/platform_admin"
cp -- "${TEST_ROOT}/install.sh" "${TEST_ROOT}/.env.example" "${TEST_PROJECT}/"
cp -R -- "${TEST_ROOT}/lib" "${TEST_ROOT}/modules" "${TEST_ROOT}/stacks" "${TEST_ROOT}/templates" "${TEST_PROJECT}/"
cp -- "${TEST_ROOT}/config/default.env" "${TEST_ROOT}/config/images.env" "${TEST_PROJECT}/config/"
cp -- "${TEST_ROOT}/config/services/services.yaml" "${TEST_PROJECT}/config/services/"
cp -- "${TEST_ROOT}/config/domains/domains.yaml" "${TEST_PROJECT}/config/domains/"
cp -- "${TEST_ROOT}/platform_admin/passwords.py" "${TEST_PROJECT}/platform_admin/"

export INSTALLER_ROOT="${TEST_PROJECT}"
export PATH="${TEST_ROOT}/tests/fixtures/admin-installer/bin:${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export ADMIN_INSTALLER_FIXTURE_ROOT="${TEST_TEMP}"
export CONFIG_FILE="${TEST_PROJECT}/.env"
export INSTALLER_ENV_TEMPLATE="${TEST_TEMP}/template.env"
export DOCKER_FIXTURE_LOG="${TEST_TEMP}/docker.log"
export NGINX_CONFIG_ROOT="${TEST_TEMP}/nginx"
export NGINX_FIXTURE_LOG="${TEST_TEMP}/nginx.log"
export DOMAIN_FIXTURE_LOG="${TEST_TEMP}/domain.log"
export CERTBOT_FIXTURE_LOG="${TEST_TEMP}/certbot.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP}/letsencrypt"
export CERTBOT_WORK_DIR="${TEST_TEMP}/certbot-work"
export CERTBOT_LOGS_DIR="${TEST_TEMP}/certbot-logs"
export SYSTEM_OS_RELEASE_FILE="${TEST_ROOT}/tests/fixtures/os-release.ubuntu-24.04"
export SYSTEM_NETWORK_TIMEOUT_SECONDS=2
# Exercise the default filename, not a test-only override shared by both ends.
unset INSTALLER_ADMIN_ONCE_FILE

source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/secrets.sh"
source "${TEST_ROOT}/lib/admin.sh"
cp -- "${TEST_ROOT}/.env.example" "${INSTALLER_ENV_TEMPLATE}"
config_load_installer_configuration "${INSTALLER_ENV_TEMPLATE}" >/dev/null
ADMIN_ENABLED=true
AI_SERVICE_COMPOSE_PROJECT=ai-customer-platform-ai-test
ADMIN_MAINTENANCE_UNIT=ai-customer-platform-maintenance-fixture.service
ADMIN_MAINTENANCE_STATE_DIR="${TEST_TEMP}/maintenance-state"
ADMIN_MAINTENANCE_SOCKET_DIR="${TEST_TEMP}/maintenance-socket"
config_save_keys "${INSTALLER_ENV_TEMPLATE}" ADMIN_ENABLED AI_SERVICE_COMPOSE_PROJECT ADMIN_MAINTENANCE_UNIT ADMIN_MAINTENANCE_STATE_DIR ADMIN_MAINTENANCE_SOCKET_DIR
# Loading the template above sets CONFIG_FILE, restore the actual target.
export CONFIG_FILE="${TEST_PROJECT}/.env"
readonly ONCE_FILE="$(secrets_admin_once_file)"
[[ ! -e "${CONFIG_FILE}" && ! -e "${ONCE_FILE}" ]]

printf '%s\n' 'chat.example.test' 'admin@example.test' |
  bash "${TEST_PROJECT}/install.sh" >"${TEST_TEMP}/first-output.log" 2>&1
[[ ! -e "${ONCE_FILE}" ]]
[[ "$(find "${TEST_PROJECT}" -maxdepth 1 -name '.installer-admin-password-*' | wc -l)" == 0 ]]
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
readonly INITIAL_HASH="${ADMIN_PASSWORD_HASH}"
readonly INITIAL_SECRET="${ADMIN_SESSION_SECRET}"
readonly INITIAL_PASSWORD="$(awk '/^初始密码（仅显示一次）：$/ {getline; print; exit}' "${TEST_TEMP}/first-output.log")"
[[ "${INITIAL_PASSWORD}" =~ ^[a-f0-9]{32}$ && "${ADMIN_USERNAME}" == admin ]]
[[ "$(grep -Fxc -- "${INITIAL_PASSWORD}" "${TEST_TEMP}/first-output.log")" == 1 ]]
printf '%s\n%s' "${INITIAL_PASSWORD}" "${INITIAL_HASH}" | python3 "${TEST_PROJECT}/platform_admin/passwords.py" --verify
[[ -z "${AI_ADMIN_PASSWORD:-}" ]]
if grep -Fq -- "${INITIAL_PASSWORD}" "${CONFIG_FILE}"; then exit 1; fi
for log in "${TEST_TEMP}"/docker.log "${TEST_TEMP}"/nginx.log "${TEST_TEMP}"/domain.log "${TEST_TEMP}"/certbot.log; do
  if grep -Fq -- "${INITIAL_PASSWORD}" "${log}"; then exit 1; fi
done
for key in ADMIN_PASSWORD_HASH ADMIN_SESSION_SECRET ADMIN_MAINTENANCE_TOKEN SERVICE_MANAGEMENT_TOKEN; do
  if grep -Fq -- "${!key}" "${TEST_TEMP}/first-output.log"; then exit 1; fi
done

readonly CHECKSUM="$(sha256sum "${CONFIG_FILE}" | awk '{print $1}')"
SYSTEM_OS_RELEASE_FILE="${TEST_ROOT}/tests/fixtures/os-release.ubuntu-22.04" \
  bash "${TEST_PROJECT}/install.sh" </dev/null >"${TEST_TEMP}/repeat-output.log" 2>&1
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
[[ "${ADMIN_PASSWORD_HASH}" == "${INITIAL_HASH}" && "${ADMIN_SESSION_SECRET}" == "${INITIAL_SECRET}" ]]
[[ ! -e "${ONCE_FILE}" && "$(sha256sum "${CONFIG_FILE}" | awk '{print $1}')" == "${CHECKSUM}" ]]
if grep -Fq -- "${INITIAL_PASSWORD}" "${TEST_TEMP}/repeat-output.log"; then exit 1; fi

# A failed installation retains the nonce/hash; resuming the real workflow
# must display that same password once, not silently replace credentials.
readonly RETRY_PROJECT="${TEST_TEMP}/retry-project"
mkdir -p -- "${RETRY_PROJECT}"
cp -R -- "${TEST_PROJECT}/install.sh" "${TEST_PROJECT}/.env.example" \
  "${TEST_PROJECT}/lib" "${TEST_PROJECT}/modules" "${TEST_PROJECT}/stacks" \
  "${TEST_PROJECT}/templates" "${TEST_PROJECT}/config" "${TEST_PROJECT}/platform_admin" "${RETRY_PROJECT}/"
export INSTALLER_ROOT="${RETRY_PROJECT}"
export CONFIG_FILE="${RETRY_PROJECT}/.env"
readonly RETRY_ONCE_FILE="$(secrets_admin_once_file)"
if printf '%s\n' 'chat.example.test' 'admin@example.test' | DOCKER_FIXTURE_FAIL_MATCH='build ai-admin' \
  bash "${RETRY_PROJECT}/install.sh" >"${TEST_TEMP}/failed-output.log" 2>&1; then
  printf '%s\n' 'The installer failure fixture unexpectedly succeeded.' >&2; exit 1
fi
[[ -f "${RETRY_ONCE_FILE}" && "$(<"${RETRY_ONCE_FILE}")" =~ ^[a-f0-9]{64}$ ]]
[[ "$(find "${RETRY_PROJECT}" -maxdepth 1 -name '.installer-admin-password-*' | wc -l)" == 1 ]]
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
readonly RETRY_HASH="${ADMIN_PASSWORD_HASH}"
readonly RETRY_PASSWORD="$(admin_initial_password)"
if grep -Fq -- "${RETRY_PASSWORD}" "${TEST_TEMP}/failed-output.log"; then exit 1; fi
bash "${RETRY_PROJECT}/install.sh" </dev/null >"${TEST_TEMP}/resumed-output.log" 2>&1
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
[[ "${ADMIN_PASSWORD_HASH}" == "${RETRY_HASH}" && ! -e "${RETRY_ONCE_FILE}" ]]
[[ "$(grep -Fxc -- "${RETRY_PASSWORD}" "${TEST_TEMP}/resumed-output.log")" == 1 ]]
if grep -Fq -- "${RETRY_PASSWORD}" "${CONFIG_FILE}"; then exit 1; fi
printf '%s\n' 'Real installer flow fixtures passed: fresh password, one-time display, deletion, stable rerun and failure resume.'
