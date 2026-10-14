#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly COMPOSE_FILE="${TEST_ROOT}/stacks/chatwoot/compose.yaml"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/chatwoot-fixture.XXXXXX")"
readonly DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker.log"
readonly NGINX_CALL_LOG="${TEST_TEMP_ROOT}/nginx.log"
: >"${DOCKER_CALL_LOG}"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/chatwoot-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_TEMP_ROOT}/.env"
cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${CONFIG_FILE}"
export DOCKER_FIXTURE_LOG="${DOCKER_CALL_LOG}"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${NGINX_CALL_LOG}"
export DOMAIN_FIXTURE_LOG="${TEST_TEMP_ROOT}/domain.log"
export CERTBOT_FIXTURE_LOG="${TEST_TEMP_ROOT}/certbot.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"
export CERTBOT_WORK_DIR="${TEST_TEMP_ROOT}/certbot-work"
export CERTBOT_LOGS_DIR="${TEST_TEMP_ROOT}/certbot-logs"
export SYSTEM_OS_RELEASE_FILE="${TEST_ROOT}/tests/fixtures/os-release.ubuntu-24.04"
export SYSTEM_NETWORK_TIMEOUT_SECONDS="2"

"${TEST_ROOT}/install.sh"

assert_compose_call() {
  local expected_fragment="$1"

  if ! grep -Fq -- "${expected_fragment}" "${DOCKER_CALL_LOG}"; then
    printf 'Missing expected Docker Compose call: %s\n' "${expected_fragment}" >&2
    exit 1
  fi
}

assert_compose_call 'config --quiet'
assert_compose_call 'pull'
assert_compose_call 'up -d postgres redis'
assert_compose_call 'exec -T postgres pg_isready'
assert_compose_call 'exec -T redis sh -c'
assert_compose_call 'run --rm rails bundle exec rails db:chatwoot_prepare'
assert_compose_call 'up -d rails sidekiq'
assert_compose_call 'ps --status running --services'

previous_line=0
for expected_fragment in \
  'config --quiet' \
  'pull' \
  'up -d postgres redis' \
  'exec -T postgres pg_isready' \
  'exec -T redis sh -c' \
  'run --rm rails bundle exec rails db:chatwoot_prepare' \
  'up -d rails sidekiq'; do
  current_line="$(grep -nF -- "${expected_fragment}" "${DOCKER_CALL_LOG}" | head -n 1 | cut -d: -f1)"
  if [[ -z "${current_line}" ]] || ((current_line <= previous_line)); then
    printf 'Docker Compose call is out of order: %s\n' "${expected_fragment}" >&2
    exit 1
  fi
  previous_line="${current_line}"
done

postgres_block="$(sed -n '/^  postgres:/,/^  redis:/p' "${COMPOSE_FILE}")"
redis_block="$(sed -n '/^  redis:/,/^  rails:/p' "${COMPOSE_FILE}")"

if grep -Eq '^[[:space:]]+ports:' <<<"${postgres_block}"; then
  printf '%s\n' 'PostgreSQL must not publish host ports.' >&2
  exit 1
fi

if grep -Eq '^[[:space:]]+ports:' <<<"${redis_block}"; then
  printf '%s\n' 'Redis must not publish host ports.' >&2
  exit 1
fi

if grep -Fq ':latest' "${COMPOSE_FILE}"; then
  printf '%s\n' 'Compose images must not use latest tags.' >&2
  exit 1
fi

for volume_name in postgres_data redis_data chatwoot_storage_data; do
  if ! grep -Fq -- "${volume_name}:" "${COMPOSE_FILE}"; then
    printf 'Missing required named volume: %s\n' "${volume_name}" >&2
    exit 1
  fi
done

printf '%s\n' 'Chatwoot Ubuntu fixture test passed.'
