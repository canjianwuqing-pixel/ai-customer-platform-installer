#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/config-layer-fixture.XXXXXX")"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/config-layer-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export INSTALLER_ROOT="${TEST_ROOT}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"

readonly TEST_DEFAULTS="${TEST_TEMP_ROOT}/default.env"
readonly TEST_IMAGES="${TEST_TEMP_ROOT}/images.env"
readonly TEST_USER_DEFAULT="${TEST_TEMP_ROOT}/user-default.env"
readonly TEST_USER_OVERRIDE="${TEST_TEMP_ROOT}/user-override.env"

printf '%s\n' \
  'AI_ADMIN_USERNAME=admin' \
  'AI_LOG_LEVEL=INFO' >"${TEST_DEFAULTS}"
printf '%s\n' \
  'CHATWOOT_VERSION=v4.18.0' \
  'AI_SERVICE_IMAGE=registry.example.test/ai-service:1.0.0' \
  'AI_ADMIN_IMAGE=registry.example.test/ai-admin:1.0.0' >"${TEST_IMAGES}"
printf '%s\n' \
  'DOMAIN=chat.example.test' >"${TEST_USER_DEFAULT}"
printf '%s\n' \
  'DOMAIN=custom.example.test' \
  'AI_SERVICE_IMAGE=registry.example.test/custom-service:2.0.0' \
  'AI_ADMIN_IMAGE=registry.example.test/custom-admin:2.0.0' >"${TEST_USER_OVERRIDE}"

export INSTALLER_DEFAULT_CONFIG_FILE="${TEST_DEFAULTS}"
export INSTALLER_IMAGES_CONFIG_FILE="${TEST_IMAGES}"

config_load_installer_configuration "${TEST_USER_DEFAULT}" >/dev/null
if [[ "${AI_SERVICE_IMAGE}" != 'registry.example.test/ai-service:1.0.0' ]] ||
  [[ "${AI_ADMIN_IMAGE}" != 'registry.example.test/ai-admin:1.0.0' ]]; then
  printf '%s\n' 'Pinned image defaults were not loaded from the image configuration layer.' >&2
  exit 1
fi

config_load_installer_configuration "${TEST_USER_OVERRIDE}" >/dev/null
if [[ "${AI_SERVICE_IMAGE}" != 'registry.example.test/custom-service:2.0.0' ]] ||
  [[ "${AI_ADMIN_IMAGE}" != 'registry.example.test/custom-admin:2.0.0' ]]; then
  printf '%s\n' 'User image configuration did not override the image defaults.' >&2
  exit 1
fi

if grep -Eiq '^[A-Za-z_][A-Za-z0-9_]*=.*:latest([[:space:]]|$)' \
  "${TEST_ROOT}/config/images.env"; then
  printf '%s\n' 'The repository image defaults contain a forbidden latest tag.' >&2
  exit 1
fi

if ! grep -Fxq 'AI_SERVICE_IMAGE=' "${TEST_ROOT}/config/images.env"; then
  printf '%s\n' 'AI Service must remain disabled in the standard image defaults.' >&2
  exit 1
fi

printf '%s\n' 'Layered default and user override fixture test passed.'
