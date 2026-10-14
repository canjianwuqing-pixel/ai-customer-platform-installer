#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/domains-fixture.XXXXXX")"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/domains-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export INSTALLER_ROOT="${TEST_ROOT}"
export PATH="${TEST_ROOT}/tests/fixtures/services/bin:${PATH}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/lib/records.sh"
source "${TEST_ROOT}/lib/services.sh"
source "${TEST_ROOT}/lib/domains.sh"

readonly EMPTY_SERVICES="${TEST_TEMP_ROOT}/empty-services.yaml"
readonly EMPTY_DOMAINS="${TEST_TEMP_ROOT}/empty-domains.yaml"
readonly SERVICES_CONFIG="${TEST_TEMP_ROOT}/services.yaml"
readonly DOMAINS_CONFIG="${TEST_TEMP_ROOT}/domains.yaml"
readonly DEFAULT_DOMAINS="${TEST_TEMP_ROOT}/default-domains.yaml"
readonly OVERRIDE_DOMAINS="${TEST_TEMP_ROOT}/override-domains.yaml"
readonly INVALID_BINDING="${TEST_TEMP_ROOT}/invalid-binding.yaml"
readonly DISABLED_BINDING="${TEST_TEMP_ROOT}/disabled-binding.yaml"

printf '%s\n' 'version: 1' 'services: []' >"${EMPTY_SERVICES}"
printf '%s\n' 'version: 1' 'domains: []' >"${EMPTY_DOMAINS}"

services_load "${EMPTY_SERVICES}" "" >/dev/null
domains_load "${EMPTY_DOMAINS}" "" >/dev/null
if [[ "${DOMAINS_CONFIG_LOADED}" != "true" ]] ||
  ((${#DOMAIN_BINDING_DOMAINS[@]} != 0)) ||
  [[ -n "$(domains_list)" ]]; then
  printf '%s\n' 'Empty domain configuration did not load as an empty collection.' >&2
  exit 1
fi

services_load "${TEST_ROOT}/config/services/services.example.yaml" "" >/dev/null
domains_load "${TEST_ROOT}/config/domains/domains.example.yaml" "" >/dev/null
if [[ "$(domains_get_service_id service.example.test)" != "example-service" ]]; then
  printf '%s\n' 'The repository domain configuration example is not parseable.' >&2
  exit 1
fi

cat >"${SERVICES_CONFIG}" <<'YAML'
version: 1
services:
  - id: primary-service
    name: Primary Service
    enabled: true
    ai_config: ai-one
    prompt_config: prompt-one
    knowledge_config: knowledge-one
    channel_config: channels-one
  - id: secondary-service
    name: Secondary Service
    enabled: true
    ai_config: ai-two
    prompt_config: prompt-two
    knowledge_config: knowledge-two
    channel_config: channels-two
  - id: disabled-service
    name: Disabled Service
    enabled: false
    ai_config: ""
    prompt_config: ""
    knowledge_config: ""
    channel_config: ""
YAML

cat >"${DOMAINS_CONFIG}" <<'YAML'
version: 1
domains:
  - domain: primary.example.test
    service_id: primary-service
    enabled: true
    default_entry: true
  - domain: secondary.example.test
    service_id: secondary-service
    enabled: true
    default_entry: false
YAML
chmod 600 "${DOMAINS_CONFIG}"

services_load "${SERVICES_CONFIG}" "" >/dev/null
domains_load "${EMPTY_DOMAINS}" "${DOMAINS_CONFIG}" >/dev/null
if ((${#DOMAIN_BINDING_DOMAINS[@]} != 2)) ||
  [[ "$(domains_get_service_id primary.example.test)" != "primary-service" ]] ||
  [[ "$(domains_resolve_service_id secondary.example.test)" != "secondary-service" ]]; then
  printf '%s\n' 'Multi-domain parsing or service resolution failed.' >&2
  exit 1
fi

cat >"${DEFAULT_DOMAINS}" <<'YAML'
version: 1
domains:
  - domain: override.example.test
    service_id: primary-service
    enabled: true
    default_entry: true
YAML
cat >"${OVERRIDE_DOMAINS}" <<'YAML'
version: 1
domains:
  - domain: override.example.test
    service_id: secondary-service
YAML
chmod 600 "${OVERRIDE_DOMAINS}"

domains_load "${DEFAULT_DOMAINS}" "${OVERRIDE_DOMAINS}" >/dev/null
if [[ "$(domains_get_service_id override.example.test)" != "secondary-service" ]]; then
  printf '%s\n' 'User domain configuration did not override the default binding.' >&2
  exit 1
fi

cat >"${INVALID_BINDING}" <<'YAML'
version: 1
domains:
  - domain: missing.example.test
    service_id: missing-service
    enabled: true
    default_entry: false
YAML
chmod 600 "${INVALID_BINDING}"
if domains_load "${EMPTY_DOMAINS}" "${INVALID_BINDING}" >/dev/null 2>&1; then
  printf '%s\n' 'A domain binding to an unknown service was accepted.' >&2
  exit 1
fi

cat >"${DISABLED_BINDING}" <<'YAML'
version: 1
domains:
  - domain: disabled.example.test
    service_id: disabled-service
    enabled: true
    default_entry: false
YAML
chmod 600 "${DISABLED_BINDING}"
if domains_load "${EMPTY_DOMAINS}" "${DISABLED_BINDING}" >/dev/null 2>&1; then
  printf '%s\n' 'An enabled domain binding to a disabled service was accepted.' >&2
  exit 1
fi

printf '%s\n' 'Domain parsing, multi-binding, priority, invalid binding, and empty fixture tests passed.'
