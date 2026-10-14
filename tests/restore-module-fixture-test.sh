#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/restore-fixture.XXXXXX")"
readonly TEST_ENV="${TEST_TEMP_ROOT}/restore.test.env"
readonly DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker.log"
readonly NGINX_SITE="${TEST_TEMP_ROOT}/nginx/sites-available/ai-customer-platform-chatwoot"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/restore-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${TEST_ENV}"
printf 'BACKUP_DIR=%s\n' "${TEST_TEMP_ROOT}/backups" >>"${TEST_ENV}"

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_ENV}"
export DOCKER_FIXTURE_LOG="${DOCKER_CALL_LOG}"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${TEST_TEMP_ROOT}/nginx.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"

mkdir -p -- "$(dirname -- "${NGINX_SITE}")" "${NGINX_CONFIG_ROOT}/sites-enabled"
mkdir -p -- "${TEST_TEMP_ROOT}/backups"
chmod 0700 "${TEST_TEMP_ROOT}/backups"
printf '%s\n' \
  '# Managed by AI Customer Platform Installer.' \
  '# Managed by AI Customer Platform Installer domain module.' \
  '# fixture-original' >"${NGINX_SITE}"
: >"${DOCKER_CALL_LOG}"

"${TEST_ROOT}/backup.sh"
backup_id="$(find "${TEST_TEMP_ROOT}/backups" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | head -n 1)"

: >"${DOCKER_CALL_LOG}"
if "${TEST_ROOT}/restore.sh" "${backup_id}" </dev/null >/dev/null 2>&1; then
  printf '%s\n' 'Non-interactive restore succeeded without explicit confirmation.' >&2
  exit 1
fi
if grep -Fq ' stop ' "${DOCKER_CALL_LOG}"; then
  printf '%s\n' 'Restore changed service state before confirmation.' >&2
  exit 1
fi

printf '%s\n' \
  '# Managed by AI Customer Platform Installer.' \
  '# Managed by AI Customer Platform Installer domain module.' \
  '# fixture-mutated' >"${NGINX_SITE}"
: >"${DOCKER_CALL_LOG}"
"${TEST_ROOT}/restore.sh" --yes "${backup_id}"

if ! grep -Fq '# fixture-original' "${NGINX_SITE}" || grep -Fq '# fixture-mutated' "${NGINX_SITE}"; then
  printf '%s\n' 'The managed Nginx configuration was not restored.' >&2
  exit 1
fi

for expected_call in \
  'stop rails sidekiq' \
  'stop ai-service ai-admin' \
  'dropdb --if-exists --force' \
  'createdb --template template0' \
  'pg_restore --exit-on-error' \
  'up -d postgres redis rails sidekiq' \
  'up -d ai-service ai-admin' \
  'ps --status running --services'; do
  if ! grep -Fq -- "${expected_call}" "${DOCKER_CALL_LOG}"; then
    printf 'Missing expected restore flow call: %s\n' "${expected_call}" >&2
    exit 1
  fi
done

restore_volume_calls="$(grep -Fc -- '--volumes-from fixture-container-' "${DOCKER_CALL_LOG}")"
if ((restore_volume_calls < 3)); then
  printf '%s\n' 'Restore did not replay all persistent data archives.' >&2
  exit 1
fi

printf '%s\n' 'Restore module and data recovery flow fixture test passed.'
