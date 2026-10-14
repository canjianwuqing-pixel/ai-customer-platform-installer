#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/rollback-fixture.XXXXXX")"
readonly TEST_ENV="${TEST_TEMP_ROOT}/rollback.test.env"
readonly DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker.log"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/rollback-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${TEST_ENV}"
printf 'BACKUP_DIR=%s\nUPGRADE_RECORD_DIR=%s\n' \
  "${TEST_TEMP_ROOT}/backups" "${TEST_TEMP_ROOT}/upgrades" >>"${TEST_ENV}"

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_ENV}"
export DOCKER_FIXTURE_LOG="${DOCKER_CALL_LOG}"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${TEST_TEMP_ROOT}/nginx.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"

site_path="${NGINX_CONFIG_ROOT}/sites-available/ai-customer-platform-chatwoot"
enabled_path="${NGINX_CONFIG_ROOT}/sites-enabled/ai-customer-platform-chatwoot"
mkdir -p -- "$(dirname -- "${site_path}")" "$(dirname -- "${enabled_path}")"
printf '%s\n' \
  '# Managed by AI Customer Platform Installer.' \
  '# Managed by AI Customer Platform Installer domain module.' >"${site_path}"
cp -- "${site_path}" "${enabled_path}"
: >"${DOCKER_CALL_LOG}"

"${TEST_ROOT}/upgrade.sh" --yes >/dev/null
record_id="$(find "${TEST_TEMP_ROOT}/upgrades" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | head -n 1)"
: >"${DOCKER_CALL_LOG}"
"${TEST_ROOT}/rollback.sh" --yes

if ! grep -Fxq 'CHATWOOT_VERSION=v4.18.0' "${TEST_ENV}" ||
  ! grep -Fxq 'AI_SERVICE_IMAGE=registry.example.test/ai-service:1.0.0' "${TEST_ENV}" ||
  ! grep -Fxq 'AI_ADMIN_IMAGE=registry.example.test/ai-admin:1.0.0' "${TEST_ENV}"; then
  printf '%s\n' 'Rollback did not restore the pre-upgrade version configuration.' >&2
  exit 1
fi

record_file="${TEST_TEMP_ROOT}/upgrades/${record_id}/record.env"
if ! grep -Fxq 'STATUS=rolled_back' "${record_file}" ||
  ! grep -Eq '^ROLLED_BACK_AT=[0-9]{4}-' "${record_file}"; then
  printf '%s\n' 'Rollback status was not recorded.' >&2
  exit 1
fi

for expected_call in \
  'dropdb --if-exists --force' \
  'pg_restore --exit-on-error' \
  'up -d postgres redis rails sidekiq' \
  'up -d ai-service ai-admin'; do
  if ! grep -Fq -- "${expected_call}" "${DOCKER_CALL_LOG}"; then
    printf 'Missing expected rollback flow call: %s\n' "${expected_call}" >&2
    exit 1
  fi
done

printf '%s\n' 'Rollback module fixture test passed.'
