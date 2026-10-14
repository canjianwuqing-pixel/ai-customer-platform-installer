#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "${TEST_ROOT}/runtime/tests"
readonly TEST_TEMP="$(mktemp -d "${TEST_ROOT}/runtime/tests/config-context.XXXXXX")"
cleanup() {
  local resolved="$(readlink -m -- "${TEST_TEMP}")"
  case "${resolved}" in "${TEST_ROOT}"/runtime/tests/config-context.*) rm -rf -- "${resolved}" ;; esac
}
trap cleanup EXIT
export INSTALLER_ROOT="${TEST_ROOT}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${TEST_TEMP}/old.env"
ADMIN_ENABLED=true AI_RUNTIME_BUILD=true
export ADMIN_ENABLED AI_RUNTIME_BUILD
config_load_installer_configuration "${TEST_TEMP}/old.env" >/dev/null
[[ "${ADMIN_ENABLED}" == false && "${AI_RUNTIME_BUILD}" == false ]]
CUSTOM_PROVIDER_KEY=fixture-value-not-real
config_save_keys "${CONFIG_FILE}" CUSTOM_PROVIDER_KEY
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
config_load_installer_configuration "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" >/dev/null
[[ -z "${CUSTOM_PROVIDER_KEY+x}" ]]

source "${TEST_ROOT}/modules/nginx/install.sh"
source "${TEST_ROOT}/modules/domain/check.sh"
domain_initialize_config
old_certificate="${CERTBOT_FULLCHAIN_PATH}"
DOMAIN=restored.example.test
config_save_keys "${TEST_TEMP}/old.env" DOMAIN
config_load_installer_configuration "${TEST_TEMP}/old.env" >/dev/null
domain_initialize_config
[[ "${CERTBOT_FULLCHAIN_PATH}" != "${old_certificate}" && "${CERTBOT_FULLCHAIN_PATH}" == */restored.example.test/fullchain.pem ]]

CONFIG_FILE="${TEST_TEMP}/old.env"
ADMIN_PASSWORD='literal $password # no interpolation'
config_save_keys "${CONFIG_FILE}" ADMIN_PASSWORD
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
[[ "${ADMIN_PASSWORD}" == 'literal $password # no interpolation' ]]
config_load_file <(printf '%s\n' 'ADMIN_API_URL=http://example.test # comment')
[[ "${ADMIN_API_URL}" == http://example.test ]]
config_load_file <(printf '%s\n' 'ADMIN_PASSWORD="six spaces" # comment')
[[ "${ADMIN_PASSWORD}" == 'six spaces' ]]
config_load_file <(printf '%s\n' 'ADMIN_PASSWORD=legacy$literal # password suffix')
[[ "${ADMIN_PASSWORD}" == 'legacy$literal # password suffix' ]]
source "${TEST_ROOT}/modules/docker/check.sh"
source "${TEST_ROOT}/modules/admin/check.sh"
export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
ADMIN_ENABLED=true
DOCKER_FIXTURE_COMPOSE_VERSION=2.24.3
export DOCKER_FIXTURE_COMPOSE_VERSION
if admin_check >/dev/null 2>&1; then exit 1; fi
DOCKER_FIXTURE_COMPOSE_VERSION=2.24.4
admin_check >/dev/null
if [[ -n "${TEST_COMPOSE_BINARY:-}" ]]; then
  DATABASE_PASSWORD='legacy$literal # password suffix'
  docker_execute() { shift; "${ADMIN_TEST_PYTHON:-python3}" "${TEST_ROOT}/tests/compose_context_check.py" "${TEST_COMPOSE_BINARY}" "$@"; }
  config_compose "${CHATWOOT_COMPOSE_PROJECT}" --file "${TEST_ROOT}/stacks/chatwoot/compose.yaml"
fi
printf '%s\n' 'Clean context, literal dotenv roundtrip and minimum Compose version fixtures passed.'
