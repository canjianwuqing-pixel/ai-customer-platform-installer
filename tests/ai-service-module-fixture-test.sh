#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly COMPOSE_FILE="${TEST_ROOT}/stacks/ai-service/compose.yaml"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/ai-service-fixture.XXXXXX")"
readonly DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker.log"
: >"${DOCKER_CALL_LOG}"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/ai-service-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export INSTALLER_ROOT="${TEST_ROOT}"
export CONFIG_FILE="${TEST_ROOT}/tests/fixtures/chatwoot.test.env"
export DOCKER_FIXTURE_LOG="${DOCKER_CALL_LOG}"
export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"

source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/modules/docker/check.sh"
source "${TEST_ROOT}/modules/ai-service/check.sh"
source "${TEST_ROOT}/modules/ai-service/configure.sh"
source "${TEST_ROOT}/modules/ai-service/install.sh"
source "${TEST_ROOT}/modules/ai-service/verify.sh"

config_load_optional "${CONFIG_FILE}"
ai_service_check
ai_service_configure
ai_service_install
ai_service_verify

assert_compose_call() {
  local expected_fragment="$1"

  if ! grep -Fq -- "${expected_fragment}" "${DOCKER_CALL_LOG}"; then
    printf 'Missing expected AI Service Compose call: %s\n' "${expected_fragment}" >&2
    exit 1
  fi
}

assert_compose_call 'config --quiet'
assert_compose_call 'pull'
assert_compose_call 'up -d ai-service ai-admin'
assert_compose_call 'ps --status running --services'

previous_line=0
for expected_fragment in \
  'config --quiet' \
  'pull' \
  'up -d ai-service ai-admin' \
  'ps --status running --services'; do
  current_line="$(grep -nF -- "${expected_fragment}" "${DOCKER_CALL_LOG}" | head -n 1 | cut -d: -f1)"
  if [[ -z "${current_line}" ]] || ((current_line <= previous_line)); then
    printf 'AI Service Compose call is out of order: %s\n' "${expected_fragment}" >&2
    exit 1
  fi
  previous_line="${current_line}"
done

for service_name in ai-service ai-admin; do
  if ! grep -Fq -- "  ${service_name}:" "${COMPOSE_FILE}"; then
    printf 'Missing required AI service: %s\n' "${service_name}" >&2
    exit 1
  fi
done

for volume_name in ai_service_data ai_admin_data; do
  if ! grep -Fq -- "  ${volume_name}:" "${COMPOSE_FILE}"; then
    printf 'Missing independent AI data volume: %s\n' "${volume_name}" >&2
    exit 1
  fi
done

if grep -Fq ':latest' "${COMPOSE_FILE}"; then
  printf '%s\n' 'AI Service Compose images must not use latest tags.' >&2
  exit 1
fi

for secret_value in \
  "${OPENAI_API_KEY}" \
  "${AI_SERVICE_INTERNAL_TOKEN}" \
  "${AI_ADMIN_PASSWORD}" \
  "${AI_ADMIN_SESSION_SECRET}"; do
  if grep -Fq -- "${secret_value}" "${COMPOSE_FILE}" ||
    grep -Fq -- "${secret_value}" "${DOCKER_CALL_LOG}"; then
    printf '%s\n' 'A fixture secret was written to Compose source or command logs.' >&2
    exit 1
  fi
done

if (
  AI_SERVICE_IMAGE='registry.example.test/ai-service:latest'
  ai_service_validate_config >/dev/null 2>&1
); then
  printf '%s\n' 'The AI Service configuration accepted a latest image tag.' >&2
  exit 1
fi

if (
  CHATWOOT_WEBHOOK_URL='https://chatwoot.example.test/webhooks/ai'
  CHATWOOT_WEBHOOK_SECRET=''
  CHATWOOT_INBOX_ID='1'
  ai_service_validate_config >/dev/null 2>&1
); then
  printf '%s\n' 'The AI Service configuration accepted an incomplete Chatwoot reservation.' >&2
  exit 1
fi

printf '%s\n' 'AI Service fixture test passed.'
