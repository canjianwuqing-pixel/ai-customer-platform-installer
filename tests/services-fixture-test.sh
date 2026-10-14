#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/services-fixture.XXXXXX")"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/services-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export INSTALLER_ROOT="${TEST_ROOT}"
export PATH="${TEST_ROOT}/tests/fixtures/services/bin:${PATH}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/lib/services.sh"

readonly EMPTY_CONFIG="${TEST_TEMP_ROOT}/empty.yaml"
readonly SINGLE_CONFIG="${TEST_TEMP_ROOT}/single.yaml"
readonly DEFAULT_CONFIG="${TEST_TEMP_ROOT}/defaults.yaml"
readonly OVERRIDE_CONFIG="${TEST_TEMP_ROOT}/override.yaml"
readonly INVALID_CONFIG="${TEST_TEMP_ROOT}/invalid.yaml"
readonly SENSITIVE_CONFIG="${TEST_TEMP_ROOT}/sensitive.yaml"
readonly UNSAFE_CONFIG="${TEST_TEMP_ROOT}/unsafe.yaml"

printf '%s\n' \
  'version: 1' \
  'services: []' >"${EMPTY_CONFIG}"

services_load "${EMPTY_CONFIG}" "" >/dev/null
if [[ "${SERVICES_CONFIG_LOADED}" != "true" ]] ||
  ((${#SERVICE_INSTANCE_IDS[@]} != 0)) ||
  [[ -n "$(services_list_ids)" ]]; then
  printf '%s\n' 'Empty service configuration did not load as an empty collection.' >&2
  exit 1
fi

services_load "${TEST_ROOT}/config/services/services.example.yaml" "" >/dev/null
if [[ "$(services_get_value example-service service.enabled)" != "false" ]]; then
  printf '%s\n' 'The repository service configuration example is not parseable.' >&2
  exit 1
fi

cat >"${SINGLE_CONFIG}" <<'YAML'
version: 1
services:
  - service:
      id: general-service
      name: General Service
      enabled: true
    ai:
      model: generic-model
      prompt: "Respond using the configured context."
    knowledge:
      source: primary-knowledge
    channels:
      enabled: [web, api]
    integration:
      provider: generic-provider
      config:
        endpoint: https://integration.example.test/events
        credential_ref: GENERIC_PROVIDER_CREDENTIAL
YAML
chmod 600 "${SINGLE_CONFIG}"

services_load "${EMPTY_CONFIG}" "${SINGLE_CONFIG}" >/dev/null
services_get_instance general-service
if [[ "${SERVICE_INSTANCE_CONFIG[service.name]}" != "General Service" ]] ||
  [[ "${SERVICE_INSTANCE_CONFIG[ai.model]}" != "generic-model" ]] ||
  [[ "${SERVICE_INSTANCE_CONFIG[channels.enabled]}" != "[web, api]" ]] ||
  [[ "${SERVICE_INSTANCE_CONFIG[integration.config.credential_ref]}" != \
    "GENERIC_PROVIDER_CREDENTIAL" ]]; then
  printf '%s\n' 'Parsed service instance values do not match the fixture.' >&2
  exit 1
fi

if [[ "$(services_get_value general-service ai.prompt)" != \
  'Respond using the configured context.' ]]; then
  printf '%s\n' 'Unable to retrieve a service instance field by id.' >&2
  exit 1
fi

cat >"${DEFAULT_CONFIG}" <<'YAML'
version: 1
services:
  - service:
      id: shared-service
      name: Shared Service
      enabled: true
    ai:
      model: default-model
      prompt: default-prompt
    knowledge:
      source: default-knowledge
    channels:
      enabled: [web]
    integration:
      provider: ""
      config: {}
YAML

cat >"${OVERRIDE_CONFIG}" <<'YAML'
version: 1
services:
  - service:
      id: shared-service
    ai:
      model: user-model
  - service:
      id: second-service
      name: Second Service
      enabled: false
    ai:
      model: ""
      prompt: ""
    knowledge:
      source: second-knowledge
    channels:
      enabled: []
    integration:
      provider: ""
      config: {}
YAML
chmod 600 "${OVERRIDE_CONFIG}"

services_load "${DEFAULT_CONFIG}" "${OVERRIDE_CONFIG}" >/dev/null
if ((${#SERVICE_INSTANCE_IDS[@]} != 2)) ||
  [[ "$(services_get_value shared-service ai.model)" != "user-model" ]] ||
  [[ "$(services_get_value shared-service ai.prompt)" != "default-prompt" ]] ||
  [[ "$(services_get_value second-service service.enabled)" != "false" ]]; then
  printf '%s\n' 'Multi-instance loading or user-over-default priority failed.' >&2
  exit 1
fi

cat >"${INVALID_CONFIG}" <<'YAML'
version: 1
services:
  - service:
      id: incomplete-service
      name: Incomplete Service
      enabled: maybe
YAML
chmod 600 "${INVALID_CONFIG}"
if services_load "${EMPTY_CONFIG}" "${INVALID_CONFIG}" >/dev/null 2>&1; then
  printf '%s\n' 'Invalid service configuration was accepted.' >&2
  exit 1
fi

cat >"${SENSITIVE_CONFIG}" <<'YAML'
version: 1
services:
  - service:
      id: unsafe-service
      name: Unsafe Service
      enabled: false
    ai:
      model: ""
      prompt: ""
    knowledge:
      source: ""
    channels:
      enabled: []
    integration:
      provider: generic-provider
      config:
        api_key: ""
YAML
chmod 600 "${SENSITIVE_CONFIG}"
if services_load "${EMPTY_CONFIG}" "${SENSITIVE_CONFIG}" >/dev/null 2>&1; then
  printf '%s\n' 'Plaintext secret fields were accepted in service configuration.' >&2
  exit 1
fi

cp -- "${SINGLE_CONFIG}" "${UNSAFE_CONFIG}"
chmod 644 "${UNSAFE_CONFIG}"
export SERVICES_FIXTURE_UNSAFE_FILE="${UNSAFE_CONFIG}"
if services_load "${EMPTY_CONFIG}" "${UNSAFE_CONFIG}" >/dev/null 2>&1; then
  printf '%s\n' 'Unsafe user service configuration permissions were accepted.' >&2
  exit 1
fi

printf '%s\n' 'Service instance parsing, priority, multi-instance, empty, and invalid fixture tests passed.'
