#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/backup-fixture.XXXXXX")"
readonly TEST_ENV="${TEST_TEMP_ROOT}/backup.test.env"
readonly DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker.log"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/backup-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${TEST_ENV}"
printf 'BACKUP_DIR=%s\nBACKUP_RETENTION=1\n' "${TEST_TEMP_ROOT}/backups" >>"${TEST_ENV}"
printf 'ADMIN_MAINTENANCE_STATE_DIR=%s\n' "${TEST_TEMP_ROOT}/maintenance" >>"${TEST_ENV}"
mkdir -p -- "${TEST_TEMP_ROOT}/maintenance"
chmod 0700 "${TEST_TEMP_ROOT}/maintenance"
printf '%s\n' '{"version":1,"primary":"primary.example.test","domains":[{"domain":"primary.example.test","fullchain":"/fixture/cert.pem","private_key":"/fixture/key.pem"}]}' >"${TEST_TEMP_ROOT}/maintenance/website-domains.json"

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_ENV}"
export DOCKER_FIXTURE_LOG="${DOCKER_CALL_LOG}"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${TEST_TEMP_ROOT}/nginx.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"

mkdir -p -- "${NGINX_CONFIG_ROOT}/sites-available" "${NGINX_CONFIG_ROOT}/sites-enabled"
mkdir -p -- "${TEST_TEMP_ROOT}/backups"
chmod 0700 "${TEST_TEMP_ROOT}/backups"
printf '%s\n' \
  '# Managed by AI Customer Platform Installer.' \
  '# Managed by AI Customer Platform Installer domain module.' >\
  "${NGINX_CONFIG_ROOT}/sites-available/ai-customer-platform-chatwoot"
: >"${DOCKER_CALL_LOG}"

"${TEST_ROOT}/backup.sh"
printf 'BACKUP_COMPRESS=false\n' >>"${TEST_ENV}"
"${TEST_ROOT}/backup.sh"

mapfile -t backup_ids < <(
  find "${TEST_TEMP_ROOT}/backups" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
)
if ((${#backup_ids[@]} != 1)); then
  printf 'Retention expected one backup, found %s.\n' "${#backup_ids[@]}" >&2
  exit 1
fi

backup_id="${backup_ids[0]}"
backup_path="${TEST_TEMP_ROOT}/backups/${backup_id}"
for artifact in \
  manifest.env \
  SHA256SUMS \
  data/chatwoot-postgres.dump \
  data/chatwoot-storage.tar \
  data/ai-service-data.tar \
  data/ai-admin-data.tar \
  config/platform.env \
  config/ssl-record.env \
  config/website-domains.json \
  compose/chatwoot.compose.yaml \
  compose/ai-service.compose.yaml \
  nginx/site.conf; do
  if [[ ! -s "${backup_path}/${artifact}" ]]; then
    printf 'Missing backup artifact: %s\n' "${artifact}" >&2
    exit 1
  fi
done
cmp -- "${TEST_TEMP_ROOT}/maintenance/website-domains.json" "${backup_path}/config/website-domains.json"
grep -Fq 'config/website-domains.json' "${backup_path}/SHA256SUMS"

if ! grep -Fq 'AI_DATA_COVERAGE=database,knowledge-base,application-data' \
  "${backup_path}/manifest.env"; then
  printf '%s\n' 'AI database and knowledge-base coverage was not recorded.' >&2
  exit 1
fi

if ! grep -Fq 'COMPRESSED=false' "${backup_path}/manifest.env"; then
  printf '%s\n' 'BACKUP_COMPRESS=false was not recorded in the retained version.' >&2
  exit 1
fi

list_output="$("${TEST_ROOT}/restore.sh" --list)"
if ! grep -Fq -- "${backup_id}" <<<"${list_output}"; then
  printf '%s\n' 'Backup list did not include the retained version.' >&2
  exit 1
fi

for secret_value in \
  fixture_openai_api_key_not_real_123456789 \
  fixture_internal_token_not_real_1234567890 \
  fixture_admin_password_not_real; do
  if grep -Fq -- "${secret_value}" "${DOCKER_CALL_LOG}"; then
    printf '%s\n' 'A fixture secret was exposed in backup command logs.' >&2
    exit 1
  fi
done

for expected_call in \
  'stop rails sidekiq' \
  'stop ai-service ai-admin' \
  'pg_dump' \
  'start rails sidekiq' \
  'start ai-service ai-admin'; do
  if ! grep -Fq -- "${expected_call}" "${DOCKER_CALL_LOG}"; then
    printf 'Missing expected backup flow call: %s\n' "${expected_call}" >&2
    exit 1
  fi
done

printf '%s\n' 'Backup module fixture test passed.'
