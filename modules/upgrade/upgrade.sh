#!/usr/bin/env bash
set -Eeuo pipefail

upgrade_prepare_record_root() {
  local current_gid
  local current_uid

  current_uid="$(id -u)"
  current_gid="$(id -g)"

  if [[ ! -d "${UPGRADE_RECORD_DIR}" ]]; then
    if ! mkdir -p -- "${UPGRADE_RECORD_DIR}" 2>/dev/null; then
      run_as_root mkdir -p -- "${UPGRADE_RECORD_DIR}" || return 1
      run_as_root chown "${current_uid}:${current_gid}" "${UPGRADE_RECORD_DIR}" || return 1
    fi
  fi

  if [[ -L "${UPGRADE_RECORD_DIR}" ]] || [[ ! -d "${UPGRADE_RECORD_DIR}" ]] ||
    [[ ! -w "${UPGRADE_RECORD_DIR}" ]]; then
    log_error "UPGRADE_RECORD_DIR must be a writable real directory."
    return 1
  fi

  chmod 0700 "${UPGRADE_RECORD_DIR}" || return 1
}

upgrade_is_valid_record_id() {
  local record_id="$1"
  [[ "${record_id}" =~ ^[0-9]{8}T[0-9]{6}Z(-[0-9]{2})?$ ]]
}

upgrade_create_record_id() {
  local base_id
  local candidate_id
  local suffix=0

  base_id="$(date -u '+%Y%m%dT%H%M%SZ')"
  candidate_id="${base_id}"
  while [[ -e "${UPGRADE_RECORD_DIR}/${candidate_id}" ]]; do
    suffix=$((suffix + 1))
    printf -v candidate_id '%s-%02d' "${base_id}" "${suffix}"
  done
  printf '%s\n' "${candidate_id}"
}

upgrade_get_record_directory() {
  local record_id="$1"
  local resolved_root
  local resolved_record

  if ! upgrade_is_valid_record_id "${record_id}"; then
    log_error "Invalid upgrade record ID format."
    return 1
  fi

  resolved_root="$(readlink -m -- "${UPGRADE_RECORD_DIR}")"
  resolved_record="$(readlink -m -- "${UPGRADE_RECORD_DIR}/${record_id}")"
  if [[ "$(dirname -- "${resolved_record}")" != "${resolved_root}" ]]; then
    log_error "Upgrade record ID resolves outside UPGRADE_RECORD_DIR."
    return 1
  fi
  printf '%s\n' "${resolved_record}"
}

upgrade_set_record_value() {
  local record_file="$1"
  local key_name="$2"
  local value="$3"
  local temporary_file="${record_file}.update.$$"
  local line
  local found="false"

  if [[ ! "${key_name}" =~ ^[A-Z][A-Z0-9_]*$ ]] ||
    [[ "${value}" == *$'\n'* ]] || [[ "${value}" == *$'\r'* ]]; then
    log_error "Refusing to write an invalid upgrade record value."
    return 1
  fi

  : >"${temporary_file}" || return 1
  chmod 0600 "${temporary_file}" || return 1
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${key_name}="* ]]; then
      printf '%s=%s\n' "${key_name}" "${value}" >>"${temporary_file}" || return 1
      found="true"
    else
      printf '%s\n' "${line}" >>"${temporary_file}" || return 1
    fi
  done <"${record_file}"

  if [[ "${found}" != "true" ]]; then
    printf '%s=%s\n' "${key_name}" "${value}" >>"${temporary_file}" || return 1
  fi
  mv -f -- "${temporary_file}" "${record_file}" || return 1
}

upgrade_create_record() {
  local record_id="$1"
  local record_directory="${UPGRADE_RECORD_DIR}/${record_id}"

  mkdir -- "${record_directory}" || return 1
  chmod 0700 "${record_directory}" || return 1
  {
    printf 'UPGRADE_RECORD_FORMAT_VERSION=%s\n' "${UPGRADE_RECORD_FORMAT_VERSION}"
    printf 'UPGRADE_ID=%s\n' "${record_id}"
    printf 'STARTED_AT=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'STATUS=preparing\n'
    printf 'BACKUP_ID=\n'
    printf 'FAILED_STEP=\n'
    printf 'CHATWOOT_FROM=%s\n' "${CHATWOOT_VERSION}"
    printf 'CHATWOOT_TO=%s\n' "${UPGRADE_CHATWOOT_VERSION}"
    printf 'AI_SERVICE_FROM=%s\n' "${AI_SERVICE_IMAGE}"
    printf 'AI_SERVICE_TO=%s\n' "${UPGRADE_AI_SERVICE_IMAGE}"
    printf 'AI_ADMIN_FROM=%s\n' "${AI_ADMIN_IMAGE}"
    printf 'AI_ADMIN_TO=%s\n' "${UPGRADE_AI_ADMIN_IMAGE}"
  } >"${record_directory}/record.env" || return 1
  chmod 0600 "${record_directory}/record.env" || return 1
  printf '%s\n' "${record_directory}"
}

upgrade_confirm() {
  local confirmed="$1"
  local response

  log_info "[upgrade] Chatwoot target: ${CHATWOOT_VERSION} -> ${UPGRADE_CHATWOOT_VERSION}."
  log_info "[upgrade] AI Service and administration images will use their configured fixed targets."

  if [[ "${confirmed}" == "true" ]]; then
    log_warn "[upgrade] Explicit non-interactive confirmation supplied."
    return 0
  fi

  if [[ ! -t 0 ]]; then
    log_error "Upgrade requires interactive confirmation or the explicit --yes option."
    return 1
  fi

  printf 'Create a backup and upgrade the platform? Type UPGRADE to continue: ' >&2
  read -r response
  if [[ "${response}" != "UPGRADE" ]]; then
    log_warn "[upgrade] Upgrade cancelled; no backup, service, or configuration changes were made."
    return 1
  fi
}

upgrade_pull_target_images() {
  local -a chatwoot_services=()
  local -a ai_services=()

  read -r -a chatwoot_services <<<"${UPGRADE_CHATWOOT_APP_SERVICES}"
  read -r -a ai_services <<<"${UPGRADE_AI_APP_SERVICES}"

  log_info "[upgrade] Pulling configured fixed-version target images."
  CHATWOOT_VERSION="${UPGRADE_CHATWOOT_VERSION}" \
    chatwoot_compose pull "${chatwoot_services[@]}" || return 1
  AI_SERVICE_IMAGE="${UPGRADE_AI_SERVICE_IMAGE}" \
    AI_ADMIN_IMAGE="${UPGRADE_AI_ADMIN_IMAGE}" \
    ai_service_compose pull "${ai_services[@]}" || return 1
}

upgrade_write_target_config() {
  local temporary_file="${CONFIG_FILE}.upgrade.$$"
  local line
  local key_name
  local chatwoot_count=0
  local ai_service_count=0
  local ai_admin_count=0

  : >"${temporary_file}" || return 1
  chmod 0600 "${temporary_file}" || return 1

  while IFS= read -r line || [[ -n "${line}" ]]; do
    key_name="${line%%=*}"
    case "${key_name}" in
      CHATWOOT_VERSION)
        printf 'CHATWOOT_VERSION=%s\n' "${UPGRADE_CHATWOOT_VERSION}" >>"${temporary_file}"
        chatwoot_count=$((chatwoot_count + 1))
        ;;
      AI_SERVICE_IMAGE)
        printf 'AI_SERVICE_IMAGE=%s\n' "${UPGRADE_AI_SERVICE_IMAGE}" >>"${temporary_file}"
        ai_service_count=$((ai_service_count + 1))
        ;;
      AI_ADMIN_IMAGE)
        printf 'AI_ADMIN_IMAGE=%s\n' "${UPGRADE_AI_ADMIN_IMAGE}" >>"${temporary_file}"
        ai_admin_count=$((ai_admin_count + 1))
        ;;
      *) printf '%s\n' "${line}" >>"${temporary_file}" ;;
    esac
  done <"${CONFIG_FILE}"

  if ((chatwoot_count != 1 || ai_service_count != 1 || ai_admin_count != 1)); then
    rm -f -- "${temporary_file}"
    log_error "Active version keys must each occur exactly once in the .env file."
    return 1
  fi

  mv -f -- "${temporary_file}" "${CONFIG_FILE}" || return 1
  config_load_file "${CONFIG_FILE}"
  chatwoot_validate_config
  ai_service_validate_config
  chatwoot_compose config --quiet
  ai_service_compose config --quiet
  log_ok "[upgrade] Active Compose configuration now references the target versions."
}

upgrade_stop_application_services() {
  local -a chatwoot_services=()
  local -a ai_services=()

  read -r -a chatwoot_services <<<"${UPGRADE_CHATWOOT_APP_SERVICES}"
  read -r -a ai_services <<<"${UPGRADE_AI_APP_SERVICES}"
  log_info "[upgrade] Stopping application services before database migration."
  chatwoot_compose stop "${chatwoot_services[@]}" || return 1
  ai_service_compose stop "${ai_services[@]}" || return 1
}

upgrade_run_database_migration() {
  log_info "[upgrade] Running the Chatwoot database preparation and migration task."
  if ! chatwoot_compose run --rm --no-deps \
    "${UPGRADE_CHATWOOT_MIGRATION_SERVICE}" \
    bundle exec rails db:chatwoot_prepare; then
    log_error "Chatwoot database migration failed."
    return 1
  fi
}

upgrade_restart_application_services() {
  local -a chatwoot_services=()
  local -a ai_services=()

  read -r -a chatwoot_services <<<"${UPGRADE_CHATWOOT_APP_SERVICES}"
  read -r -a ai_services <<<"${UPGRADE_AI_APP_SERVICES}"
  log_info "[upgrade] Recreating application services with the target images."
  chatwoot_compose up -d --no-deps --force-recreate "${chatwoot_services[@]}" || return 1
  ai_service_compose up -d --no-deps --force-recreate "${ai_services[@]}" || return 1
}

upgrade_fail_before_change() {
  local record_file="$1"
  local failed_step="$2"

  upgrade_set_record_value "${record_file}" FAILED_STEP "${failed_step}" || true
  upgrade_set_record_value "${record_file}" STATUS failed_before_change || true
  upgrade_set_record_value "${record_file}" COMPLETED_AT "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" || true
  log_error "Upgrade stopped before the active configuration or data was changed."
  return 1
}

upgrade_fail_and_restore() {
  local record_directory="$1"
  local backup_id="$2"
  local failed_step="$3"
  local record_file="${record_directory}/record.env"

  upgrade_set_record_value "${record_file}" FAILED_STEP "${failed_step}" || true
  upgrade_set_record_value "${record_file}" STATUS automatic_rollback_in_progress || true
  log_warn "[upgrade] ${failed_step} failed; restoring the verified pre-upgrade backup."

  if backup_restore "${backup_id}" true &&
    upgrade_verify_rolled_back_versions "${record_directory}"; then
    upgrade_set_record_value "${record_file}" STATUS rolled_back_after_failure || true
    upgrade_set_record_value "${record_file}" COMPLETED_AT "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" || true
    log_ok "[upgrade] Automatic rollback restored the pre-upgrade platform."
  else
    upgrade_set_record_value "${record_file}" STATUS automatic_rollback_failed || true
    upgrade_set_record_value "${record_file}" COMPLETED_AT "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" || true
    log_error "Automatic rollback failed; services may remain stopped and require operator recovery."
  fi
  return 1
}

upgrade_execute() {
  local confirmed="$1"
  local record_id
  local record_directory
  local record_file
  local backup_id

  upgrade_check
  upgrade_confirm "${confirmed}"
  upgrade_prepare_record_root
  record_id="$(upgrade_create_record_id)"
  record_directory="$(upgrade_create_record "${record_id}")"
  record_file="${record_directory}/record.env"
  log_info "[upgrade] Created upgrade record ${record_id}."

  if ! backup_create; then
    upgrade_fail_before_change "${record_file}" backup
    return 1
  fi
  backup_id="${BACKUP_LAST_CREATED_ID}"
  if [[ -z "${backup_id}" ]]; then
    upgrade_fail_before_change "${record_file}" backup-record
    return 1
  fi
  upgrade_set_record_value "${record_file}" BACKUP_ID "${backup_id}"
  upgrade_set_record_value "${record_file}" STATUS backed_up

  if ! upgrade_pull_target_images; then
    upgrade_fail_before_change "${record_file}" image-pull
    return 1
  fi

  if ! upgrade_write_target_config; then
    upgrade_fail_and_restore "${record_directory}" "${backup_id}" config-update
    return 1
  fi
  upgrade_set_record_value "${record_file}" STATUS configuration_updated

  if ! upgrade_stop_application_services; then
    upgrade_fail_and_restore "${record_directory}" "${backup_id}" service-stop
    return 1
  fi
  if ! upgrade_run_database_migration; then
    upgrade_fail_and_restore "${record_directory}" "${backup_id}" database-migration
    return 1
  fi
  if ! upgrade_restart_application_services; then
    upgrade_fail_and_restore "${record_directory}" "${backup_id}" service-restart
    return 1
  fi
  if ! upgrade_verify "${record_directory}"; then
    upgrade_fail_and_restore "${record_directory}" "${backup_id}" health-check
    return 1
  fi

  upgrade_set_record_value "${record_file}" STATUS completed
  upgrade_set_record_value "${record_file}" COMPLETED_AT "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log_ok "[upgrade] Upgrade completed successfully: ${record_id}."
}
