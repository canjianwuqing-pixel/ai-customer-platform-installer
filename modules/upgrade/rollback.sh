#!/usr/bin/env bash
set -Eeuo pipefail

upgrade_list_records() {
  local record_id
  local record_file
  local status
  local started_at

  upgrade_initialize_config
  if [[ -z "${UPGRADE_RECORD_DIR:-}" ]] ||
    ! validation_is_safe_absolute_path "${UPGRADE_RECORD_DIR}"; then
    log_error "UPGRADE_RECORD_DIR is not configured with a safe absolute path."
    return 1
  fi

  if [[ ! -d "${UPGRADE_RECORD_DIR}" ]]; then
    log_info "[upgrade] No upgrade record directory exists yet."
    return 0
  fi

  log_info "[upgrade] Available upgrade records:"
  while IFS= read -r record_id; do
    upgrade_is_valid_record_id "${record_id}" || continue
    record_file="${UPGRADE_RECORD_DIR}/${record_id}/record.env"
    [[ -f "${record_file}" ]] || continue
    status="$(read_key_value_file "${record_file}" STATUS || true)"
    started_at="$(read_key_value_file "${record_file}" STARTED_AT || true)"
    printf '%s\t%s\t%s\n' "${record_id}" "${status:-unknown}" "${started_at:-unknown}"
  done < <(find "${UPGRADE_RECORD_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r)
}

upgrade_find_latest_completed_record() {
  local record_id
  local record_file

  [[ -d "${UPGRADE_RECORD_DIR}" ]] || return 1
  while IFS= read -r record_id; do
    upgrade_is_valid_record_id "${record_id}" || continue
    record_file="${UPGRADE_RECORD_DIR}/${record_id}/record.env"
    [[ -f "${record_file}" ]] || continue
    if [[ "$(read_key_value_file "${record_file}" STATUS || true)" == "completed" ]]; then
      printf '%s\n' "${record_id}"
      return 0
    fi
  done < <(find "${UPGRADE_RECORD_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r)
  return 1
}

upgrade_verify_record() {
  local record_directory="$1"
  local expected_id="$2"
  local record_file="${record_directory}/record.env"
  local format_version
  local manifest_id
  local backup_id
  local status

  if [[ ! -d "${record_directory}" ]] || [[ -L "${record_directory}" ]] ||
    ! file_is_readable "${record_file}"; then
    log_error "Upgrade record is missing or unsafe."
    return 1
  fi

  format_version="$(read_key_value_file "${record_file}" UPGRADE_RECORD_FORMAT_VERSION)"
  manifest_id="$(read_key_value_file "${record_file}" UPGRADE_ID)"
  backup_id="$(read_key_value_file "${record_file}" BACKUP_ID)"
  status="$(read_key_value_file "${record_file}" STATUS)"

  if [[ "${format_version}" != "${UPGRADE_RECORD_FORMAT_VERSION}" ]] ||
    [[ "${manifest_id}" != "${expected_id}" ]] ||
    [[ "${status}" != "completed" ]] ||
    ! backup_is_valid_version_id "${backup_id}"; then
    log_error "Only a valid, completed upgrade record can be rolled back."
    return 1
  fi

  if [[ "$(read_key_value_file "${record_file}" CHATWOOT_TO)" != "${CHATWOOT_VERSION}" ]] ||
    [[ "$(read_key_value_file "${record_file}" AI_SERVICE_TO)" != "${AI_SERVICE_IMAGE}" ]] ||
    [[ "$(read_key_value_file "${record_file}" AI_ADMIN_TO)" != "${AI_ADMIN_IMAGE}" ]]; then
    log_error "The active versions do not match this upgrade record's target versions."
    return 1
  fi

  backup_verify_version "${backup_id}"
}

upgrade_confirm_rollback() {
  local record_id="$1"
  local confirmed="$2"
  local response

  if [[ "${confirmed}" == "true" ]]; then
    log_warn "[rollback] Explicit non-interactive confirmation supplied for ${record_id}."
    return 0
  fi

  if [[ ! -t 0 ]]; then
    log_error "Rollback requires interactive confirmation or the explicit --yes option."
    return 1
  fi

  printf 'Restore the pre-upgrade backup for %s? Type ROLLBACK to continue: ' "${record_id}" >&2
  read -r response
  if [[ "${response}" != "ROLLBACK" ]]; then
    log_warn "[rollback] Rollback cancelled; no services or files were changed."
    return 1
  fi
}

upgrade_rollback() {
  local record_id="$1"
  local confirmed="$2"
  local record_directory
  local record_file
  local backup_id

  upgrade_check_rollback_prerequisites
  upgrade_prepare_record_root
  record_directory="$(upgrade_get_record_directory "${record_id}")" || return 1
  upgrade_verify_record "${record_directory}" "${record_id}"
  upgrade_confirm_rollback "${record_id}" "${confirmed}"

  record_file="${record_directory}/record.env"
  backup_id="$(read_key_value_file "${record_file}" BACKUP_ID)"
  upgrade_set_record_value "${record_file}" STATUS manual_rollback_in_progress

  if ! backup_restore "${backup_id}" true ||
    ! upgrade_verify_rolled_back_versions "${record_directory}"; then
    upgrade_set_record_value "${record_file}" STATUS manual_rollback_failed || true
    upgrade_set_record_value "${record_file}" ROLLED_BACK_AT "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" || true
    log_error "Manual rollback failed; operator recovery may be required."
    return 1
  fi

  upgrade_set_record_value "${record_file}" STATUS rolled_back
  upgrade_set_record_value "${record_file}" ROLLED_BACK_AT "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log_ok "[rollback] Upgrade ${record_id} was rolled back successfully."
}
