#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/secrets-fixture.XXXXXX")"
readonly CHMOD_CALL_LOG="${TEST_TEMP_ROOT}/chmod.log"
: >"${CHMOD_CALL_LOG}"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/secrets-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export INSTALLER_ROOT="${TEST_ROOT}"
export SECRETS_CHMOD_LOG="${CHMOD_CALL_LOG}"
export PATH="${TEST_ROOT}/tests/fixtures/secrets/bin:${PATH}"

source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/secrets.sh"

assert_hex_length() {
  local key="$1"
  local expected_length="$2"
  local value="${!key}"
  local pattern="^[0-9a-f]{${expected_length}}$"

  if [[ ! "${value}" =~ ${pattern} ]]; then
    printf 'Generated %s does not have the expected secure format.\n' "${key}" >&2
    exit 1
  fi
}

readonly EMPTY_CONFIG="${TEST_TEMP_ROOT}/empty.env"
readonly FIRST_LOG="${TEST_TEMP_ROOT}/first.log"
readonly SECOND_LOG="${TEST_TEMP_ROOT}/second.log"

printf '%s\n' \
  'DOMAIN=chat.example.test' \
  'DATABASE_PASSWORD=' \
  'REDIS_PASSWORD=' \
  'SECRET_KEY_BASE=' \
  'AI_SERVICE_INTERNAL_TOKEN=' \
  'AI_ADMIN_SESSION_SECRET=' \
  'SERVICE_MANAGEMENT_TOKEN=' \
  'AI_ADMIN_PASSWORD=' >"${EMPTY_CONFIG}"

config_load_optional "${EMPTY_CONFIG}"
secrets_ensure >"${FIRST_LOG}" 2>&1

assert_hex_length DATABASE_PASSWORD 64
assert_hex_length REDIS_PASSWORD 64
assert_hex_length SECRET_KEY_BASE 128
assert_hex_length AI_SERVICE_INTERNAL_TOKEN 64
assert_hex_length SERVICE_MANAGEMENT_TOKEN 64
if [[ -n "${AI_ADMIN_PASSWORD}" || -n "${AI_ADMIN_SESSION_SECRET}" ]]; then
  printf '%s\n' 'Generic secret generation must not create legacy administrator credentials.' >&2
  exit 1
fi

if ! grep -Fq -- "600 ${EMPTY_CONFIG}" "${CHMOD_CALL_LOG}"; then
  printf '%s\n' 'Secret generation did not request 0600 permissions for .env.' >&2
  exit 1
fi

if [[ "${OSTYPE:-}" != msys* && "${OSTYPE:-}" != cygwin* &&
  "$(stat -c '%a' "${EMPTY_CONFIG}")" != "600" ]]; then
  printf '%s\n' 'Generated .env permissions are not 0600.' >&2
  exit 1
fi

for key in "${INSTALLER_SECRET_KEYS[@]}"; do
  if grep -Fq -- "${!key}" "${FIRST_LOG}"; then
    printf 'Generated %s was exposed in installer output.\n' "${key}" >&2
    exit 1
  fi
done

readonly FIRST_CHECKSUM="$(sha256sum "${EMPTY_CONFIG}" | awk '{print $1}')"
config_load_optional "${EMPTY_CONFIG}" >/dev/null
secrets_ensure >"${SECOND_LOG}" 2>&1
readonly SECOND_CHECKSUM="$(sha256sum "${EMPTY_CONFIG}" | awk '{print $1}')"

if [[ "${FIRST_CHECKSUM}" != "${SECOND_CHECKSUM}" ]]; then
  printf '%s\n' 'Repeated secret generation changed an already populated configuration.' >&2
  exit 1
fi

readonly EXISTING_CONFIG="${TEST_TEMP_ROOT}/existing.env"
readonly EXISTING_LOG="${TEST_TEMP_ROOT}/existing.log"
readonly EXISTING_DATABASE_PASSWORD='existing-database-password'
readonly EXISTING_REDIS_PASSWORD='existing-redis-password'
readonly EXISTING_SECRET_KEY_BASE='existing-secret-key-base-value'
readonly EXISTING_INTERNAL_TOKEN='existing-internal-token'
readonly EXISTING_SESSION_SECRET='existing-session-secret'
readonly EXISTING_ADMIN_PASSWORD='existing-admin-password'
readonly EXISTING_MANAGEMENT_TOKEN='existing-management-token'

printf '%s\n' \
  "DATABASE_PASSWORD=${EXISTING_DATABASE_PASSWORD}" \
  "REDIS_PASSWORD=${EXISTING_REDIS_PASSWORD}" \
  "SECRET_KEY_BASE=${EXISTING_SECRET_KEY_BASE}" \
  "AI_SERVICE_INTERNAL_TOKEN=${EXISTING_INTERNAL_TOKEN}" \
  "AI_ADMIN_SESSION_SECRET=${EXISTING_SESSION_SECRET}" \
  "SERVICE_MANAGEMENT_TOKEN=${EXISTING_MANAGEMENT_TOKEN}" \
  "AI_ADMIN_PASSWORD=${EXISTING_ADMIN_PASSWORD}" >"${EXISTING_CONFIG}"
readonly EXISTING_CHECKSUM="$(sha256sum "${EXISTING_CONFIG}" | awk '{print $1}')"

config_load_optional "${EXISTING_CONFIG}" >/dev/null
secrets_ensure >"${EXISTING_LOG}" 2>&1

if [[ "${DATABASE_PASSWORD}" != "${EXISTING_DATABASE_PASSWORD}" ||
  "${REDIS_PASSWORD}" != "${EXISTING_REDIS_PASSWORD}" ||
  "${SECRET_KEY_BASE}" != "${EXISTING_SECRET_KEY_BASE}" ||
  "${AI_SERVICE_INTERNAL_TOKEN}" != "${EXISTING_INTERNAL_TOKEN}" ||
  "${AI_ADMIN_SESSION_SECRET}" != "${EXISTING_SESSION_SECRET}" ||
  "${SERVICE_MANAGEMENT_TOKEN}" != "${EXISTING_MANAGEMENT_TOKEN}" ||
  "${AI_ADMIN_PASSWORD}" != "${EXISTING_ADMIN_PASSWORD}" ]]; then
  printf '%s\n' 'An existing secret was overwritten.' >&2
  exit 1
fi

if [[ "$(sha256sum "${EXISTING_CONFIG}" | awk '{print $1}')" != "${EXISTING_CHECKSUM}" ]]; then
  printf '%s\n' 'An existing configuration file was unexpectedly rewritten.' >&2
  exit 1
fi

if ! grep -Fq -- "600 ${EXISTING_CONFIG}" "${CHMOD_CALL_LOG}"; then
  printf '%s\n' 'Existing .env permissions were not restricted to 0600.' >&2
  exit 1
fi

for existing_value in \
  "${EXISTING_DATABASE_PASSWORD}" \
  "${EXISTING_REDIS_PASSWORD}" \
  "${EXISTING_SECRET_KEY_BASE}" \
  "${EXISTING_INTERNAL_TOKEN}" \
  "${EXISTING_SESSION_SECRET}" \
  "${EXISTING_MANAGEMENT_TOKEN}" \
  "${EXISTING_ADMIN_PASSWORD}"; do
  if grep -Fq -- "${existing_value}" "${EXISTING_LOG}"; then
    printf '%s\n' 'An existing secret was exposed in installer output.' >&2
    exit 1
  fi
done

printf '%s\n' 'Secrets fixture test passed.'
