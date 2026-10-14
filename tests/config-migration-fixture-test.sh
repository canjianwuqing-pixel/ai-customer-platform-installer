#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "${TEST_ROOT}/runtime/tests"
readonly TEST_TEMP="$(mktemp -d "${TEST_ROOT}/runtime/tests/config-migration.XXXXXX")"
cleanup() {
  local result=$? resolved="$(readlink -m -- "${TEST_TEMP}")"
  if ((result)); then printf 'Migration fixture retained at %s\n' "${TEST_TEMP}" >&2; return; fi
  case "${resolved}" in "${TEST_ROOT}"/runtime/tests/config-migration.*) rm -rf -- "${resolved}" ;; esac
}
trap cleanup EXIT
export INSTALLER_ROOT="${TEST_ROOT}" CONFIG_FILE="${TEST_TEMP}/user.env"
export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/secrets.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/lib/admin.sh"
source "${TEST_ROOT}/lib/lifecycle.sh"
source "${TEST_ROOT}/modules/docker/check.sh"
source "${TEST_ROOT}/modules/upgrade/check.sh"
cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${CONFIG_FILE}"
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
INSTALLER_CONFIG_VERSION=99
config_save_keys "${CONFIG_FILE}" INSTALLER_CONFIG_VERSION
before="$(sha256sum "${CONFIG_FILE}")"
if config_prepare install >"${TEST_TEMP}/unsupported.log" 2>&1; then exit 1; fi
[[ "$(sha256sum "${CONFIG_FILE}")" == "${before}" ]]

INSTALLER_CONFIG_VERSION=1 ADMIN_ENABLED=true ADMIN_PASSWORD=admin
config_save_keys "${CONFIG_FILE}" INSTALLER_CONFIG_VERSION ADMIN_ENABLED ADMIN_PASSWORD
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
admin_prepare_credentials >"${TEST_TEMP}/admin.log"
[[ "${ADMIN_PASSWORD}" == admin && -n "${AI_ADMIN_PASSWORD}" ]]
readonly SESSION_SECRET="${ADMIN_SESSION_SECRET}" MAINTENANCE_TOKEN="${ADMIN_MAINTENANCE_TOKEN}"
ADMIN_PASSWORD=123456
ADMIN_PASSWORD_HASH=pbkdf2_sha256:600000:00000000000000000000000000000000:0000000000000000000000000000000000000000000000000000000000000000
config_save_keys "${CONFIG_FILE}" ADMIN_PASSWORD ADMIN_PASSWORD_HASH
admin_prepare_credentials >>"${TEST_TEMP}/admin.log"
[[ "${ADMIN_PASSWORD}" == 123456 && "${ADMIN_SESSION_SECRET}" == "${SESSION_SECRET}" && "${ADMIN_MAINTENANCE_TOKEN}" == "${MAINTENANCE_TOKEN}" ]]

export DOCKER_FIXTURE_CHATWOOT_DEPLOYED=true
DATABASE_PASSWORD=""
config_save_keys "${CONFIG_FILE}" DATABASE_PASSWORD
before="$(sha256sum "${CONFIG_FILE}")"
if config_prepare install >"${TEST_TEMP}/missing.log" 2>&1; then exit 1; fi
[[ "$(sha256sum "${CONFIG_FILE}")" == "${before}" ]]

DATABASE_PASSWORD=incorrect-fixture-password
export DOCKER_FIXTURE_DATABASE_PASSWORD=fixture_database_password
upgrade_source_file="${TEST_TEMP}/source.env"
cp -- "${CONFIG_FILE}" "${upgrade_source_file}"
if upgrade_capture_chatwoot_source >"${TEST_TEMP}/mismatch.log" 2>&1; then exit 1; fi
for value in "${SESSION_SECRET}" "${MAINTENANCE_TOKEN}" incorrect-fixture-password; do
  if grep -Fq -- "${value}" "${TEST_TEMP}"/*.log; then exit 1; fi
done
printf '%s\n' 'Migration version rejection, explicit passwords, legacy hash compatibility, retained secrets and deployed-credential mismatch fixtures passed.'
