#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "${TEST_ROOT}/runtime/tests"
readonly TEST_TEMP="$(mktemp -d "${TEST_ROOT}/runtime/tests/admin-module.XXXXXX")"
cleanup() {
  local result=$?
  if ((result != 0)); then
    printf 'Failed Admin fixture retained at %s\n' "${TEST_TEMP}" >&2
    return 0
  fi
  local resolved="$(cd -- "${TEST_TEMP}" && pwd -P)"
  case "${resolved}" in
    "${TEST_ROOT}"/runtime/tests/admin-module.*) rm -rf -- "${resolved}" ;;
    *) printf '%s\n' 'Unsafe fixture cleanup path.' >&2 ;;
  esac
}
trap cleanup EXIT
trap 'printf "Admin module fixture failed at line %s.\n" "${LINENO}" >&2' ERR
export INSTALLER_ROOT="${TEST_ROOT}"
export CONFIG_FILE="${TEST_TEMP}/.env"
export INSTALLER_ADMIN_ONCE_FILE="${TEST_TEMP}/password-once"
export DOCKER_FIXTURE_LOG="${TEST_TEMP}/docker.log"
export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/lib/secrets.sh"
source "${TEST_ROOT}/lib/setup.sh"
source "${TEST_ROOT}/lib/admin.sh"
source "${TEST_ROOT}/modules/docker/check.sh"
source "${TEST_ROOT}/modules/ai-service/check.sh"
source "${TEST_ROOT}/modules/ai-service/configure.sh"
source "${TEST_ROOT}/modules/ai-service/install.sh"
source "${TEST_ROOT}/modules/ai-service/verify.sh"
for module in check configure install verify; do source "${TEST_ROOT}/modules/admin/${module}.sh"; done
source "${TEST_ROOT}/modules/nginx/install.sh"
source "${TEST_ROOT}/modules/nginx/configure.sh"
source "${TEST_ROOT}/modules/domain/check.sh"
source "${TEST_ROOT}/modules/domain/ssl.sh"
source "${TEST_ROOT}/modules/chatwoot/configure.sh"
source "${TEST_ROOT}/modules/backup/backup.sh"
if [[ -n "${ADMIN_TEST_PYTHON:-}" ]]; then
  python3() { "${ADMIN_TEST_PYTHON}" "$@"; }
fi

config_create_from_template "${CONFIG_FILE}" "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" >/dev/null
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
ADMIN_ENABLED=true
AI_SERVICE_IMAGE="" AI_ADMIN_IMAGE=""
ADMIN_MAINTENANCE_STATE_DIR="${TEST_TEMP}/state"
ADMIN_MAINTENANCE_SOCKET_DIR="${TEST_TEMP}/socket"
config_save_keys "${CONFIG_FILE}" ADMIN_ENABLED AI_SERVICE_IMAGE AI_ADMIN_IMAGE ADMIN_MAINTENANCE_STATE_DIR ADMIN_MAINTENANCE_SOCKET_DIR
setup_configure_optional_modules >"${TEST_TEMP}/setup.log"
[[ "${AI_RUNTIME_BUILD}" == true && "${AI_SERVICE_ENABLED}" == true ]]
secrets_ensure >>"${TEST_TEMP}/setup.log"
readonly OLD_PASSWORD="${AI_ADMIN_PASSWORD}"
admin_prepare_credentials >>"${TEST_TEMP}/setup.log"
[[ -z "${AI_ADMIN_PASSWORD}" && -z "${AI_ADMIN_SESSION_SECRET}" ]]
[[ "${ADMIN_PASSWORD_HASH}" =~ ^pbkdf2_sha256:600000: ]]
if grep -Fq -- "${OLD_PASSWORD}" "${CONFIG_FILE}" || grep -Fq -- "${OLD_PASSWORD}" "${TEST_TEMP}/setup.log"; then
  printf '%s\n' 'Legacy plaintext password was not safely migrated.' >&2; exit 1
fi
readonly FIRST_HASH="${ADMIN_PASSWORD_HASH}"
readonly FIRST_SECRET="${ADMIN_SESSION_SECRET}"
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
secrets_ensure >>"${TEST_TEMP}/setup.log"
admin_prepare_credentials >>"${TEST_TEMP}/setup.log"
[[ "${ADMIN_PASSWORD_HASH}" == "${FIRST_HASH}" && "${ADMIN_SESSION_SECRET}" == "${FIRST_SECRET}" ]]

# Stub privileged installation only: never write /etc or operate real systemd.
run_as_root() {
  printf '%s\n' "$*" >>"${TEST_TEMP}/host.log"
  case "$1" in
    systemctl) return 0 ;;
    install)
      if [[ "$2" == -d ]]; then mkdir -p -- "${ADMIN_MAINTENANCE_STATE_DIR}" "${ADMIN_MAINTENANCE_SOCKET_DIR}"; chmod 700 "${ADMIN_MAINTENANCE_STATE_DIR}" "${ADMIN_MAINTENANCE_SOCKET_DIR}"
      else cp -- "$4" "${TEST_TEMP}/worker.service"; fi ;;
    *) return 1 ;;
  esac
}
ai_service_configure >"${TEST_TEMP}/deploy.log"
ai_service_install >>"${TEST_TEMP}/deploy.log"
admin_install >>"${TEST_TEMP}/deploy.log"
admin_verify >>"${TEST_TEMP}/deploy.log"
ai_service_verify >>"${TEST_TEMP}/deploy.log"
grep -Fq 'stacks/admin/compose.yaml' "${DOCKER_FIXTURE_LOG}"
grep -Fq 'build ai-admin' "${DOCKER_FIXTURE_LOG}"
grep -Fq 'up -d ai-admin' "${DOCKER_FIXTURE_LOG}"
grep -Fq 'up -d ai-service' "${DOCKER_FIXTURE_LOG}"
grep -Fq 'modules/admin/worker.sh' "${TEST_TEMP}/worker.service"
if grep -Fq 'docker.sock' "${TEST_ROOT}/stacks/admin/compose.yaml"; then exit 1; fi

nginx_initialize_config
nginx_render_template "${TEST_TEMP}/http.conf"
domain_initialize_config
domain_render_https_template "${TEST_TEMP}/https.conf"
for rendered in http https; do
  grep -Fq 'location = /admin {' "${TEST_TEMP}/${rendered}.conf"
  grep -Fq 'location ^~ /admin/ {' "${TEST_TEMP}/${rendered}.conf"
  grep -Fq "proxy_pass http://${ADMIN_UPSTREAM_HOST}:${AI_ADMIN_HTTP_PORT};" "${TEST_TEMP}/${rendered}.conf"
  if grep -Eq '{{|SERVICE_MANAGEMENT_TOKEN|ADMIN_PASSWORD_HASH' "${TEST_TEMP}/${rendered}.conf"; then exit 1; fi
done
admin_print_completion >"${TEST_TEMP}/completion.log"
if grep -Fq "${OLD_PASSWORD}" "${TEST_TEMP}/completion.log"; then exit 1; fi
[[ ! -e "${INSTALLER_ADMIN_ONCE_FILE}" ]]
admin_print_completion >"${TEST_TEMP}/completion-again.log"
if grep -Fq "${OLD_PASSWORD}" "${TEST_TEMP}/completion-again.log"; then exit 1; fi

# Existing backup implementation must include both new deployment overlays.
chatwoot_initialize_config
NGINX_SITE_CONFIG_PATH="${TEST_TEMP}/https.conf"
backup_copy_protected_file() { cp -- "$1" "$2"; chmod 600 "$2"; }
mkdir -p -- "${TEST_TEMP}/backup-config"
backup_write_configuration_files "${TEST_TEMP}/backup-config"
cmp -- "${TEST_ROOT}/stacks/admin/compose.yaml" "${TEST_TEMP}/backup-config/compose/admin.compose.yaml"
cmp -- "${TEST_ROOT}/stacks/ai-service/compose.runtime.yaml" "${TEST_TEMP}/backup-config/compose/runtime.compose.yaml"

# Fresh install: no legacy plaintext, cryptographic random password, stable hash.
CONFIG_FILE="${TEST_TEMP}/fresh.env"
config_create_from_template "${CONFIG_FILE}" "${TEST_ROOT}/.env.example" >/dev/null
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
ADMIN_ENABLED=true
AI_ADMIN_PASSWORD="" AI_ADMIN_SESSION_SECRET=""
admin_prepare_credentials >"${TEST_TEMP}/fresh.log"
readonly FRESH_HASH="${ADMIN_PASSWORD_HASH}"
[[ "$(<"${INSTALLER_ADMIN_ONCE_FILE}")" =~ ^[a-f0-9]{64}$ ]]
readonly FRESH_PASSWORD="$(admin_initial_password)"
[[ "${FRESH_PASSWORD}" =~ ^[a-f0-9]{32}$ ]]
if grep -Fq -- "${FRESH_PASSWORD}" "${CONFIG_FILE}" || grep -Fq -- "${FRESH_PASSWORD}" "${TEST_TEMP}/fresh.log"; then exit 1; fi
admin_prepare_credentials >>"${TEST_TEMP}/fresh.log"
[[ "${ADMIN_PASSWORD_HASH}" == "${FRESH_HASH}" ]]
[[ "$(admin_initial_password)" == "${FRESH_PASSWORD}" ]]
admin_print_completion >"${TEST_TEMP}/fresh-completion.log"
grep -Fq "${FRESH_PASSWORD}" "${TEST_TEMP}/fresh-completion.log"
[[ ! -e "${INSTALLER_ADMIN_ONCE_FILE}" ]]
admin_print_completion >"${TEST_TEMP}/fresh-again.log"
if grep -Fq "${FRESH_PASSWORD}" "${TEST_TEMP}/fresh-again.log"; then exit 1; fi
if [[ "${OSTYPE:-}" != msys* && "${OSTYPE:-}" != cygwin* && "$(stat -c %a "${CONFIG_FILE}")" != 600 ]]; then exit 1; fi
ADMIN_ENABLED=false
nginx_render_template "${TEST_TEMP}/disabled.conf"
if grep -Fq 'location = /admin' "${TEST_TEMP}/disabled.conf"; then exit 1; fi
printf '%s\n' 'Unified Admin configuration, migration, Compose, Nginx and one-time credential fixtures passed.'
