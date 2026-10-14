#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/manage-fixture.XXXXXX")"
readonly TEST_CONFIG="${TEST_TEMP_ROOT}/enabled.env"
readonly TEST_DISABLED_CONFIG="${TEST_TEMP_ROOT}/disabled.env"
readonly DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker.log"
readonly DISABLED_DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker-disabled.log"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/manage-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${TEST_CONFIG}"
sed \
  -e 's|^AI_SERVICE_IMAGE=.*|AI_SERVICE_IMAGE=|' \
  -e 's|^AI_ADMIN_IMAGE=.*|AI_ADMIN_IMAGE=|' \
  "${TEST_CONFIG}" >"${TEST_DISABLED_CONFIG}"
: >"${DOCKER_CALL_LOG}"
: >"${DISABLED_DOCKER_CALL_LOG}"

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_CONFIG}"
export DOCKER_FIXTURE_LOG="${DOCKER_CALL_LOG}"

readonly CONFIG_CHECKSUM_BEFORE="$(sha256sum "${TEST_CONFIG}" | awk '{print $1}')"
readonly STATUS_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" status 2>&1)"
readonly LOGS_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" logs 2>&1)"
readonly FOLLOW_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" logs-follow 2>&1)"
readonly RESTART_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" restart 2>&1)"
readonly STOP_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" stop 2>&1)"
readonly START_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" start 2>&1)"
readonly HEALTH_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" health 2>&1)"
readonly CONFIG_CHECKSUM_AFTER="$(sha256sum "${TEST_CONFIG}" | awk '{print $1}')"

for expected_status in \
  '[OK] Chatwoot Rails running' \
  '[OK] Chatwoot Sidekiq running' \
  '[OK] PostgreSQL running' \
  '[OK] Redis running' \
  '[OK] AI Service running' \
  '[OK] AI Admin running'; do
  if ! grep -Fq -- "${expected_status}" <<<"${STATUS_OUTPUT}"; then
    printf 'Missing management status output: %s\n' "${expected_status}" >&2
    exit 1
  fi
done

for expected_health in \
  '[OK] PostgreSQL healthy' \
  '[OK] Redis healthy' \
  '[OK] Chatwoot Rails healthy' \
  '[OK] AI Service healthy' \
  '[OK] AI Admin healthy'; do
  if ! grep -Fq -- "${expected_health}" <<<"${HEALTH_OUTPUT}"; then
    printf 'Missing management health output: %s\n' "${expected_health}" >&2
    exit 1
  fi
done

if ! grep -Fq -- 'fixture logs for' <<<"${LOGS_OUTPUT}" ||
  ! grep -Fq -- 'Following Chatwoot and AI Service logs.' <<<"${FOLLOW_OUTPUT}"; then
  printf '%s\n' 'Log and real-time log commands did not execute.' >&2
  exit 1
fi

for expected_action in \
  '[OK] Chatwoot services restarted' \
  '[OK] AI Service restarted' \
  '[OK] Chatwoot services stopped' \
  '[OK] AI Service stopped' \
  '[OK] Chatwoot services started' \
  '[OK] AI Service started'; do
  if ! grep -Fq -- "${expected_action}" \
    <<<"${RESTART_OUTPUT}"$'\n'"${STOP_OUTPUT}"$'\n'"${START_OUTPUT}"; then
    printf 'Missing management action output: %s\n' "${expected_action}" >&2
    exit 1
  fi
done

for compose_file in \
  "${TEST_ROOT}/stacks/chatwoot/compose.yaml" \
  "${TEST_ROOT}/stacks/ai-service/compose.yaml"; do
  if ! grep -Fq -- "--file ${compose_file}" "${DOCKER_CALL_LOG}"; then
    printf 'Management command did not locate Compose file: %s\n' "${compose_file}" >&2
    exit 1
  fi
done

for compose_command in ' logs ' ' restart ' ' stop ' ' start ' ' ps '; do
  if ! grep -Fq -- "${compose_command}" "${DOCKER_CALL_LOG}"; then
    printf 'Missing Docker Compose management command: %s\n' "${compose_command}" >&2
    exit 1
  fi
done

if grep -Eq ' (pull|up|run) ' "${DOCKER_CALL_LOG}"; then
  printf '%s\n' 'Management commands invoked a deployment operation.' >&2
  exit 1
fi

if [[ "${CONFIG_CHECKSUM_BEFORE}" != "${CONFIG_CHECKSUM_AFTER}" ]]; then
  printf '%s\n' 'Management commands modified the user configuration.' >&2
  exit 1
fi

export DOCKER_FIXTURE_MISSING_SERVICE="rails"
FAILURE_OUTPUT=""
if FAILURE_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" status 2>&1)"; then
  printf '%s\n' 'Service status unexpectedly succeeded with Chatwoot Rails stopped.' >&2
  exit 1
fi
unset DOCKER_FIXTURE_MISSING_SERVICE

if ! grep -Fq -- '[ERROR] Chatwoot Rails not running' <<<"${FAILURE_OUTPUT}"; then
  printf '%s\n' 'Stopped services do not use the unified error output.' >&2
  exit 1
fi

export CONFIG_FILE="${TEST_DISABLED_CONFIG}"
export DOCKER_FIXTURE_LOG="${DISABLED_DOCKER_CALL_LOG}"
readonly DISABLED_OUTPUT="$(bash "${TEST_ROOT}/manage.sh" status 2>&1)"

if ! grep -Fq -- 'AI Service 未启用，跳过。' <<<"${DISABLED_OUTPUT}"; then
  printf '%s\n' 'Disabled AI Service was not reported as skipped.' >&2
  exit 1
fi

if grep -Fq -- 'ai-customer-platform-ai-test' "${DISABLED_DOCKER_CALL_LOG}"; then
  printf '%s\n' 'Disabled AI Service was unexpectedly managed.' >&2
  exit 1
fi

printf '%s\n' 'Unified service management fixture test passed.'
