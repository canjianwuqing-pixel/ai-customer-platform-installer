#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/upgrade-fixture.XXXXXX")"
readonly TEST_ENV="${TEST_TEMP_ROOT}/upgrade.test.env"
readonly DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker.log"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/upgrade-fixture.*) rm -rf -- "${resolved_temp}" ;;
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

"${TEST_ROOT}/upgrade.sh" --yes

if ! grep -Fxq 'CHATWOOT_VERSION=v4.19.0' "${TEST_ENV}" ||
  ! grep -Fxq 'AI_SERVICE_IMAGE=registry.example.test/ai-service:1.1.0' "${TEST_ENV}" ||
  ! grep -Fxq 'AI_ADMIN_IMAGE=registry.example.test/ai-admin:1.1.0' "${TEST_ENV}"; then
  printf '%s\n' 'Upgrade did not atomically activate all configured target versions.' >&2
  exit 1
fi

record_id="$(find "${TEST_TEMP_ROOT}/upgrades" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | head -n 1)"
record_file="${TEST_TEMP_ROOT}/upgrades/${record_id}/record.env"
for expected_record in \
  'STATUS=completed' \
  'CHATWOOT_FROM=v4.18.0' \
  'CHATWOOT_TO=v4.19.0' \
  'AI_SERVICE_FROM=registry.example.test/ai-service:1.0.0' \
  'AI_SERVICE_TO=registry.example.test/ai-service:1.1.0'; do
  if ! grep -Fxq -- "${expected_record}" "${record_file}"; then
    printf 'Missing upgrade record value: %s\n' "${expected_record}" >&2
    exit 1
  fi
done

backup_id="$(sed -n 's/^BACKUP_ID=//p' "${record_file}")"
if [[ -z "${backup_id}" ]] || [[ ! -d "${TEST_TEMP_ROOT}/backups/${backup_id}" ]]; then
  printf '%s\n' 'Upgrade did not retain its verified pre-upgrade backup.' >&2
  exit 1
fi

for expected_call in \
  'pull rails sidekiq' \
  'pull ai-service ai-admin' \
  'run --rm --no-deps rails bundle exec rails db:chatwoot_prepare' \
  'up -d --no-deps --force-recreate rails sidekiq' \
  'up -d --no-deps --force-recreate ai-service ai-admin'; do
  if ! grep -Fq -- "${expected_call}" "${DOCKER_CALL_LOG}"; then
    printf 'Missing expected upgrade flow call: %s\n' "${expected_call}" >&2
    exit 1
  fi
done

for secret_value in \
  fixture_openai_api_key_not_real_123456789 \
  fixture_internal_token_not_real_1234567890 \
  fixture_admin_password_not_real; do
  if grep -Fq -- "${secret_value}" "${DOCKER_CALL_LOG}"; then
    printf '%s\n' 'A fixture secret was exposed in upgrade command logs.' >&2
    exit 1
  fi
done

printf '%s\n' 'Upgrade module fixture test passed.'
