#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "${TEST_ROOT}/runtime/tests"
readonly TEST_TEMP="$(mktemp -d "${TEST_ROOT}/runtime/tests/lifecycle.XXXXXX")"
readonly TEST_PROJECT="${TEST_TEMP}/project"
cleanup() {
  local result=$? resolved
  if ((result)); then printf 'Lifecycle fixture retained at %s\n' "${TEST_TEMP}" >&2; return; fi
  resolved="$(readlink -m -- "${TEST_TEMP}")"
  case "${resolved}" in "${TEST_ROOT}"/runtime/tests/lifecycle.*) rm -rf -- "${resolved}" ;; esac
}
trap cleanup EXIT
trap 'printf "Lifecycle fixture failed at line %s.\n" "${LINENO}" >&2' ERR
mkdir -p -- "${TEST_PROJECT}/config/services" "${TEST_PROJECT}/config/domains"
cp -- "${TEST_ROOT}"/{install,manage,backup,restore,upgrade,rollback}.sh "${TEST_ROOT}/.env.example" "${TEST_PROJECT}/"
cp -R -- "${TEST_ROOT}"/{lib,modules,stacks,templates} "${TEST_PROJECT}/"
cp -- "${TEST_ROOT}/config/"{default,images}.env "${TEST_PROJECT}/config/"
cp -- "${TEST_ROOT}/config/services/services.yaml" "${TEST_PROJECT}/config/services/"
cp -- "${TEST_ROOT}/config/domains/domains.yaml" "${TEST_PROJECT}/config/domains/"
export INSTALLER_ROOT="${TEST_PROJECT}"
export PATH="${TEST_ROOT}/tests/fixtures/admin-installer/bin:${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export ADMIN_INSTALLER_FIXTURE_ROOT="${TEST_TEMP}"
export NGINX_CONFIG_ROOT="${TEST_TEMP}/nginx" CERTBOT_CONFIG_DIR="${TEST_TEMP}/certbot"
export NGINX_FIXTURE_LOG="${TEST_TEMP}/nginx.log" DOCKER_FIXTURE_LOG="${TEST_TEMP}/docker.log"
export DOCKER_FIXTURE_AI_DEPLOYED=false
source "${TEST_PROJECT}/lib/common.sh"
source "${TEST_PROJECT}/lib/config.sh"
source "${TEST_PROJECT}/lib/secrets.sh"
source "${TEST_PROJECT}/lib/setup.sh"
source "${TEST_PROJECT}/lib/lifecycle.sh"
export CONFIG_FILE="${TEST_PROJECT}/.env"
cp -- "${TEST_ROOT}/.env.example" "${CONFIG_FILE}"
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
DOMAIN=platform.example.test EMAIL=admin@example.test ADMIN_ENABLED=true
ADMIN_MAINTENANCE_UNIT=ai-customer-platform-maintenance-fixture.service
ADMIN_MAINTENANCE_STATE_DIR="${TEST_TEMP}/state" ADMIN_MAINTENANCE_SOCKET_DIR="${TEST_TEMP}/socket"
config_save_keys "${CONFIG_FILE}" DOMAIN EMAIL ADMIN_ENABLED ADMIN_MAINTENANCE_UNIT ADMIN_MAINTENANCE_STATE_DIR ADMIN_MAINTENANCE_SOCKET_DIR

config_prepare install >"${TEST_TEMP}/fresh.log" 2>&1
[[ "${ADMIN_PASSWORD}" == admin && "${ADMIN_API_URL}" == http://ai-service:8000 && ${#ADMIN_SESSION_SECRET} -ge 32 ]]
readonly FRESH_SUM="$(sha256sum "${CONFIG_FILE}" | cut -d ' ' -f1)"
config_prepare install >"${TEST_TEMP}/repeat.log" 2>&1
[[ "$(sha256sum "${CONFIG_FILE}" | cut -d ' ' -f1)" == "${FRESH_SUM}" ]]
[[ -z "$(find "${TEST_PROJECT}" -name '.installer-admin-password-*' -print -quit)" ]]

# Old Chatwoot-only configuration has none of the new Admin settings/tokens.
sed -E '/^(AI_|OPENAI_|SERVICE_MANAGEMENT_TOKEN|UPGRADE_)/d' "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" >"${CONFIG_FILE}"
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
ADMIN_ENABLED=true
ADMIN_MAINTENANCE_UNIT=ai-customer-platform-maintenance-fixture.service
ADMIN_MAINTENANCE_STATE_DIR="${TEST_TEMP}/state" ADMIN_MAINTENANCE_SOCKET_DIR="${TEST_TEMP}/socket"
BACKUP_DIR="${TEST_TEMP}/backups" UPGRADE_RECORD_DIR="${TEST_TEMP}/upgrades"
AI_SERVICE_COMPOSE_PROJECT=ai-customer-platform-ai-test
AI_SERVICE_STARTUP_TIMEOUT_SECONDS=2 AI_SERVICE_HEALTHCHECK_INTERVAL_SECONDS=1
config_save_keys "${CONFIG_FILE}" ADMIN_ENABLED ADMIN_MAINTENANCE_UNIT ADMIN_MAINTENANCE_STATE_DIR ADMIN_MAINTENANCE_SOCKET_DIR BACKUP_DIR UPGRADE_RECORD_DIR AI_SERVICE_COMPOSE_PROJECT AI_SERVICE_STARTUP_TIMEOUT_SECONDS AI_SERVICE_HEALTHCHECK_INTERVAL_SECONDS
readonly CORE_DATABASE="${DATABASE_PASSWORD}" CORE_REDIS="${REDIS_PASSWORD}" CORE_SECRET="${SECRET_KEY_BASE}"
site="${NGINX_CONFIG_ROOT}/sites-available/ai-customer-platform-chatwoot"
mkdir -p -- "$(dirname -- "${site}")" "${NGINX_CONFIG_ROOT}/sites-enabled"
printf '%s\n' '# Managed by AI Customer Platform Installer.' '# Managed by AI Customer Platform Installer domain module.' >"${site}"
cp -- "${site}" "${NGINX_CONFIG_ROOT}/sites-enabled/ai-customer-platform-chatwoot"
"${ADMIN_TEST_PYTHON:-python3}" "${TEST_ROOT}/tests/recovery_persistence_check.py" prepare "${TEST_TEMP}/existing-admin-data"
bash "${TEST_PROJECT}/upgrade.sh" --yes >"${TEST_TEMP}/upgrade.log" 2>&1
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
"${ADMIN_TEST_PYTHON:-python3}" "${TEST_ROOT}/tests/recovery_persistence_check.py" verify "${TEST_TEMP}/existing-admin-data"
[[ "${ADMIN_ENABLED}" == true && "${AI_RUNTIME_BUILD}" == true && -n "${AI_SERVICE_IMAGE}" && ${#SERVICE_MANAGEMENT_TOKEN} -ge 32 ]]
[[ "${DATABASE_PASSWORD}" == "${CORE_DATABASE}" && "${REDIS_PASSWORD}" == "${CORE_REDIS}" && "${SECRET_KEY_BASE}" == "${CORE_SECRET}" ]]
grep -Fq 'build ai-service' "${DOCKER_FIXTURE_LOG}"
grep -Fq 'build ai-admin' "${DOCKER_FIXTURE_LOG}"
if grep -Fq 'pull ai-service ai-admin' "${DOCKER_FIXTURE_LOG}"; then exit 1; fi
record="$(find "${UPGRADE_RECORD_DIR}" -name record.env | head -1)"
backup_id="$(read_key_value_file "${record}" BACKUP_ID)"
grep -Fxq 'AI_ENABLED=false' "${BACKUP_DIR}/${backup_id}/manifest.env"
[[ ! -e "${BACKUP_DIR}/${backup_id}/data/ai-admin-data.tar.gz" ]]

# Restore must discard every new feature flag, rather than overlaying old data.
bash "${TEST_PROJECT}/restore.sh" --yes "${backup_id}" >"${TEST_TEMP}/restore.log" 2>&1
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
[[ "${ADMIN_ENABLED}" == false && "${AI_RUNTIME_BUILD}" == false && -z "${AI_SERVICE_IMAGE}" ]]
[[ "${DATABASE_PASSWORD}" == "${CORE_DATABASE}" && "${REDIS_PASSWORD}" == "${CORE_REDIS}" && "${SECRET_KEY_BASE}" == "${CORE_SECRET}" ]]

# Failed migration changes neither the file nor running services.
ADMIN_ENABLED=true SERVICE_MANAGEMENT_TOKEN=short
config_save_keys "${CONFIG_FILE}" ADMIN_ENABLED SERVICE_MANAGEMENT_TOKEN
readonly BAD_SUM="$(sha256sum "${CONFIG_FILE}" | cut -d ' ' -f1)"
: >"${DOCKER_FIXTURE_LOG}"
if bash "${TEST_PROJECT}/upgrade.sh" --yes >"${TEST_TEMP}/failed.log" 2>&1; then exit 1; fi
[[ "$(sha256sum "${CONFIG_FILE}" | cut -d ' ' -f1)" == "${BAD_SUM}" ]]
if grep -Eq ' (stop|up|build|pull) ' "${DOCKER_FIXTURE_LOG}"; then exit 1; fi
DATABASE_PASSWORD=""
config_save_keys "${CONFIG_FILE}" DATABASE_PASSWORD
readonly MISSING_SUM="$(sha256sum "${CONFIG_FILE}" | cut -d ' ' -f1)"
if config_prepare restore >"${TEST_TEMP}/missing.log" 2>&1; then exit 1; fi
[[ "$(sha256sum "${CONFIG_FILE}" | cut -d ' ' -f1)" == "${MISSING_SUM}" ]]

# A valid backup can repair damaged current credentials; no current value may
# leak into the replacement context during the restore.
bash "${TEST_PROJECT}/restore.sh" --yes "${backup_id}" >"${TEST_TEMP}/damaged-restore.log" 2>&1
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
[[ "${DATABASE_PASSWORD}" == "${CORE_DATABASE}" && "${ADMIN_ENABLED}" == false ]]

# A newly enabled Admin that never becomes ready rolls back to Chatwoot-only.
ADMIN_ENABLED=true
config_save_keys "${CONFIG_FILE}" ADMIN_ENABLED
if CURL_FIXTURE_FAIL_READY=true bash "${TEST_PROJECT}/upgrade.sh" --yes >"${TEST_TEMP}/health-failure.log" 2>&1; then exit 1; fi
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
[[ "${ADMIN_ENABLED}" == false && -z "${AI_SERVICE_IMAGE}" && "${DATABASE_PASSWORD}" == "${CORE_DATABASE}" ]]
grep -Rq '^STATUS=rolled_back_after_failure$' "${UPGRADE_RECORD_DIR}"

# An AI-free platform can also upgrade Chatwoot without enabling any AI stack.
UPGRADE_CHATWOOT_VERSION=v4.19.0
config_save_keys "${CONFIG_FILE}" UPGRADE_CHATWOOT_VERSION
bash "${TEST_PROJECT}/upgrade.sh" --yes >"${TEST_TEMP}/chatwoot-only-upgrade.log" 2>&1
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
[[ "${CHATWOOT_VERSION}" == v4.19.0 && "${ADMIN_ENABLED}" == false && -z "${AI_SERVICE_IMAGE}" ]]
for value in "${CORE_DATABASE}" "${CORE_REDIS}" "${CORE_SECRET}"; do
  if grep -Fq -- "${value}" "${TEST_TEMP}"/*.log; then exit 1; fi
done
printf '%s\n' 'Lifecycle fixtures passed: new/repeat install, optional Admin upgrades, damaged-config restore, failed migration/health rollback and preserved secrets.'
