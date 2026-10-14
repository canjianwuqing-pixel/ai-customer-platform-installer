#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "${TEST_ROOT}/runtime/tests"
readonly TEST_TEMP="$(mktemp -d "${TEST_ROOT}/runtime/tests/quiesce.XXXXXX")"
cleanup() {
  local resolved="$(readlink -m -- "${TEST_TEMP}")"
  case "${resolved}" in "${TEST_ROOT}"/runtime/tests/quiesce.*) rm -rf -- "${resolved}" ;; esac
}
trap cleanup EXIT
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/modules/backup/backup.sh"
source "${TEST_ROOT}/modules/upgrade/upgrade.sh"
BACKUP_DIR="${TEST_TEMP}/backups"
BACKUP_CHATWOOT_STORAGE_SERVICE=rails
backup_initialize_config() { :; }
config_ai_enabled() { return 1; }
backup_get_container_id() { printf '%s\n' fixture-container; }
backup_suspend_application_services() { printf '%s\n' suspend >>"${TEST_TEMP}/events"; }
backup_resume_application_services() { printf '%s\n' resume >>"${TEST_TEMP}/events"; }
backup_create_payload() { printf '%s\n' snapshot >>"${TEST_TEMP}/events"; }
backup_verify_directory() { [[ "${FAIL_VERIFICATION:-false}" != true ]]; }
backup_apply_retention() { :; }
backup_create true >"${TEST_TEMP}/backup.log"
[[ "$(tr '\n' ' ' <"${TEST_TEMP}/events")" == 'suspend snapshot ' ]]

: >"${TEST_TEMP}/events"
backup_create >>"${TEST_TEMP}/backup.log"
[[ "$(tr '\n' ' ' <"${TEST_TEMP}/events")" == 'suspend snapshot resume ' ]]

: >"${TEST_TEMP}/events"
FAIL_VERIFICATION=true
if backup_create true >>"${TEST_TEMP}/backup.log" 2>&1; then exit 1; fi
grep -Fxq resume "${TEST_TEMP}/events"

# An artifact preparation failure resumes original containers without replaying
# the database. No service/data commands run in this deterministic control test.
upgrade_source_file="${TEST_TEMP}/source.env"
upgrade_set_record_value() { :; }
config_load_installer_configuration() { printf '%s\n' reload-source >>"${TEST_TEMP}/events"; }
backup_restore() { printf '%s\n' unexpected-data-restore >>"${TEST_TEMP}/events"; }
: >"${TEST_TEMP}/events"
if upgrade_fail_before_change "${TEST_TEMP}/record.env" image-build >>"${TEST_TEMP}/backup.log" 2>&1; then exit 1; fi
[[ "$(tr '\n' ' ' <"${TEST_TEMP}/events")" == 'reload-source resume ' ]]
printf '%s\n' 'Upgrade snapshot holds writers, normal backups resume, and pre-migration failures resume without data replay.'
