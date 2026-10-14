#!/usr/bin/env bash
set -Eeuo pipefail

backup_confirm_restore() {
  local backup_id="$1"
  local confirmed="$2"
  local response

  if [[ "${confirmed}" == "true" ]]; then
    log_warn "[restore] Confirmation supplied non-interactively for backup ${backup_id}."
    return 0
  fi

  if [[ ! -t 0 ]]; then
    log_error "Restore requires interactive confirmation or the explicit --yes option."
    return 1
  fi

  printf 'Restore backup %s and overwrite current platform data? Type RESTORE to continue: ' "${backup_id}" >&2
  read -r response
  if [[ "${response}" != "RESTORE" ]]; then
    log_warn "[restore] Restore cancelled; no services or files were changed."
    return 1
  fi
}

backup_manifest_value() {
  local backup_directory="$1"
  local key_name="$2"
  read_key_value_file "${backup_directory}/manifest.env" "${key_name}"
}

backup_verify_restore_compatibility() {
  local backup_directory="$1"

  if [[ "$(backup_manifest_value "${backup_directory}" CHATWOOT_COMPOSE_PROJECT)" != "${CHATWOOT_COMPOSE_PROJECT}" ]] ||
    [[ "$(backup_manifest_value "${backup_directory}" AI_SERVICE_COMPOSE_PROJECT)" != "${AI_SERVICE_COMPOSE_PROJECT}" ]] ||
    [[ "$(backup_manifest_value "${backup_directory}" DATABASE_NAME)" != "${DATABASE_NAME}" ]] ||
    [[ "$(backup_manifest_value "${backup_directory}" DATABASE_USER)" != "${DATABASE_USER}" ]] ||
    [[ "$(backup_manifest_value "${backup_directory}" CHATWOOT_DATABASE_SERVICE)" != "${BACKUP_CHATWOOT_DATABASE_SERVICE}" ]] ||
    [[ "$(backup_manifest_value "${backup_directory}" CHATWOOT_STORAGE_SERVICE)" != "${BACKUP_CHATWOOT_STORAGE_SERVICE}" ]] ||
    [[ "$(backup_manifest_value "${backup_directory}" AI_DATA_SERVICE)" != "${BACKUP_AI_DATA_SERVICE}" ]] ||
    [[ "$(backup_manifest_value "${backup_directory}" AI_ADMIN_SERVICE)" != "${BACKUP_AI_ADMIN_SERVICE}" ]]; then
    log_error "Backup project, database, or service metadata does not match the current deployment."
    return 1
  fi
}

backup_wait_for_restore_database() {
  local deadline=$((SECONDS + CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS))

  until chatwoot_compose exec -T "${BACKUP_CHATWOOT_DATABASE_SERVICE}" \
    pg_isready --username "${DATABASE_USER}" --dbname "${DATABASE_NAME}" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
      log_error "PostgreSQL did not become ready for restore within the configured timeout."
      return 1
    fi
    sleep "${CHATWOOT_HEALTHCHECK_INTERVAL_SECONDS}"
  done
}

backup_restore_chatwoot_database() {
  local dump_path="$1"

  log_info "[restore] Recreating and restoring the Chatwoot PostgreSQL database."
  chatwoot_compose up -d "${BACKUP_CHATWOOT_DATABASE_SERVICE}" || return 1
  backup_wait_for_restore_database || return 1

  chatwoot_compose exec -T "${BACKUP_CHATWOOT_DATABASE_SERVICE}" \
    dropdb --if-exists --force --username "${DATABASE_USER}" "${DATABASE_NAME}" || return 1
  chatwoot_compose exec -T "${BACKUP_CHATWOOT_DATABASE_SERVICE}" \
    createdb --template template0 --username "${DATABASE_USER}" "${DATABASE_NAME}" || return 1

  if ! chatwoot_compose exec -T "${BACKUP_CHATWOOT_DATABASE_SERVICE}" \
    pg_restore \
    --exit-on-error \
    --no-owner \
    --no-privileges \
    --username "${DATABASE_USER}" \
    --dbname "${DATABASE_NAME}" <"${dump_path}"; then
    log_error "Chatwoot PostgreSQL restore failed; application services remain stopped."
    return 1
  fi
}

backup_restore_container_path() {
  local container_id="$1"
  local target_path="$2"
  local archive_path="$3"
  local compressed="$4"

  if ! docker_execute run --rm \
    --volumes-from "${container_id}" \
    "${BACKUP_TOOL_IMAGE}" \
    sh -eu -c '
      target_path="$1"
      compressed="$2"
      mkdir -p -- "${target_path}"
      find "${target_path}" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
      if [ "${compressed}" = "true" ]; then
        tar -C "${target_path}" -xzf -
      else
        tar -C "${target_path}" -xf -
      fi
    ' sh "${target_path}" "${compressed}" <"${archive_path}"; then
    log_error "Persistent data restore failed; application services remain stopped."
    return 1
  fi
}

backup_rollback_project_files() {
  local target_path
  local rollback_path
  local -a target_paths=("$@")

  for target_path in "${target_paths[@]}"; do
    rollback_path="${target_path}.pre-restore.$$"
    if [[ -f "${rollback_path}" ]]; then
      mv -f -- "${rollback_path}" "${target_path}" || true
    fi
    rm -f -- "${target_path}.restore.$$" || true
  done

  config_load_file "${CONFIG_FILE}" || true
  log_warn "[restore] Restored the pre-restore project configuration files."
}

backup_restore_project_files() {
  local backup_directory="$1"
  local index
  local source_path
  local target_path
  local staged_path
  local mode
  local -a source_paths=(
    "${backup_directory}/config/platform.env"
    "${backup_directory}/compose/chatwoot.compose.yaml"
    "${backup_directory}/compose/ai-service.compose.yaml"
  )
  local -a target_paths=(
    "${CONFIG_FILE}"
    "${CHATWOOT_COMPOSE_FILE}"
    "${AI_SERVICE_COMPOSE_FILE}"
  )

  log_info "[restore] Staging the protected environment and Compose configuration files."
  if [[ -f "${backup_directory}/compose/admin.compose.yaml" ]]; then
    source_paths+=("${backup_directory}/compose/admin.compose.yaml")
    target_paths+=("${INSTALLER_ROOT}/stacks/admin/compose.yaml")
  fi
  if [[ -f "${backup_directory}/compose/runtime.compose.yaml" ]]; then
    source_paths+=("${backup_directory}/compose/runtime.compose.yaml")
    target_paths+=("${INSTALLER_ROOT}/stacks/ai-service/compose.runtime.yaml")
  fi
  for ((index = 0; index < ${#target_paths[@]}; index++)); do
    source_path="${source_paths[index]}"
    target_path="${target_paths[index]}"
    staged_path="${target_path}.restore.$$"
    mode="0644"
    [[ "${target_path}" == "${CONFIG_FILE}" ]] && mode="0600"

    if [[ ! -f "${target_path}" ]] || [[ ! -w "${target_path}" ]]; then
      log_error "Active project configuration is not writable: ${target_path}"
      backup_rollback_project_files "${target_paths[@]}"
      return 1
    fi

    cp -p -- "${target_path}" "${target_path}.pre-restore.$$" || {
      backup_rollback_project_files "${target_paths[@]}"
      return 1
    }
    cp -- "${source_path}" "${staged_path}" || {
      backup_rollback_project_files "${target_paths[@]}"
      return 1
    }
    chmod "${mode}" "${staged_path}" || {
      backup_rollback_project_files "${target_paths[@]}"
      return 1
    }
  done

  for target_path in "${target_paths[@]}"; do
    mv -f -- "${target_path}.restore.$$" "${target_path}" || {
      backup_rollback_project_files "${target_paths[@]}"
      return 1
    }
  done

  config_load_file "${CONFIG_FILE}"
  backup_validate_config || {
    backup_rollback_project_files "${target_paths[@]}"
    return 1
  }

  if ! chatwoot_compose config --quiet || ! ai_service_compose config --quiet; then
    log_error "Restored Compose configuration did not validate."
    backup_rollback_project_files "${target_paths[@]}"
    return 1
  fi

  for target_path in "${target_paths[@]}"; do
    rm -f -- "${target_path}.pre-restore.$$"
  done
  log_ok "[restore] Environment and Compose configuration files were restored safely."
}

backup_restore_nginx_configuration() {
  local backup_directory="$1"

  log_info "[restore] Validating and restoring the managed Nginx site."
  nginx_initialize_config
  nginx_apply_candidate "${backup_directory}/nginx/site.conf"
}

backup_start_restored_services() {
  local -a chatwoot_services=()
  local -a ai_services=()

  read -r -a chatwoot_services <<<"${BACKUP_CHATWOOT_REQUIRED_SERVICES}"
  read -r -a ai_services <<<"${BACKUP_AI_REQUIRED_SERVICES}"

  log_info "[restore] Starting restored platform services."
  chatwoot_compose up -d "${chatwoot_services[@]}"
  ai_service_compose up -d "${ai_services[@]}"
}

backup_verify_restored_services() {
  local service_name
  local chatwoot_running
  local ai_running
  local -a chatwoot_services=()
  local -a ai_services=()

  read -r -a chatwoot_services <<<"${BACKUP_CHATWOOT_REQUIRED_SERVICES}"
  read -r -a ai_services <<<"${BACKUP_AI_REQUIRED_SERVICES}"
  chatwoot_running="$(chatwoot_compose ps --status running --services)" || return 1
  ai_running="$(ai_service_compose ps --status running --services)" || return 1

  for service_name in "${chatwoot_services[@]}"; do
    if ! grep -Fqx -- "${service_name}" <<<"${chatwoot_running}"; then
      log_error "Restored Chatwoot service is not running: ${service_name}"
      return 1
    fi
  done

  for service_name in "${ai_services[@]}"; do
    if ! grep -Fqx -- "${service_name}" <<<"${ai_running}"; then
      log_error "Restored AI service is not running: ${service_name}"
      return 1
    fi
  done

  backup_wait_for_restore_database
  chatwoot_verify_http
  ai_service_wait_for_http "FastAPI service" "${AI_SERVICE_HTTP_CHECK_URL}"
  ai_service_wait_for_http "AI administration backend" "${AI_ADMIN_HTTP_CHECK_URL}"
  nginx_verify
  log_ok "[restore] Restored platform health checks passed."
}

backup_restore() {
  local backup_id="$1"
  local confirmed="$2"
  local backup_directory
  local archive_suffix
  local compressed
  local storage_container_id
  local ai_data_container_id
  local ai_admin_container_id

  backup_initialize_config
  backup_directory="$(backup_get_version_directory "${backup_id}")" || return 1
  backup_verify_directory "${backup_directory}" "${backup_id}"
  backup_verify_restore_compatibility "${backup_directory}"
  backup_confirm_restore "${backup_id}" "${confirmed}"

  archive_suffix="$(backup_manifest_value "${backup_directory}" ARCHIVE_SUFFIX)"
  compressed="$(backup_manifest_value "${backup_directory}" COMPRESSED)"
  storage_container_id="$(backup_get_container_id chatwoot_compose "${BACKUP_CHATWOOT_STORAGE_SERVICE}")"
  ai_data_container_id="$(backup_get_container_id ai_service_compose "${BACKUP_AI_DATA_SERVICE}")"
  ai_admin_container_id="$(backup_get_container_id ai_service_compose "${BACKUP_AI_ADMIN_SERVICE}")"

  log_warn "[restore] Beginning destructive restore of backup ${backup_id}."
  backup_suspend_application_services
  backup_restore_chatwoot_database "${backup_directory}/data/chatwoot-postgres.dump"
  backup_restore_container_path \
    "${storage_container_id}" "${BACKUP_CHATWOOT_STORAGE_PATH}" \
    "${backup_directory}/data/chatwoot-storage.${archive_suffix}" "${compressed}"
  backup_restore_container_path \
    "${ai_data_container_id}" "${AI_SERVICE_DATA_PATH}" \
    "${backup_directory}/data/ai-service-data.${archive_suffix}" "${compressed}"
  backup_restore_container_path \
    "${ai_admin_container_id}" "${AI_ADMIN_DATA_PATH}" \
    "${backup_directory}/data/ai-admin-data.${archive_suffix}" "${compressed}"
  backup_restore_project_files "${backup_directory}"
  backup_restore_nginx_configuration "${backup_directory}"
  backup_start_restored_services
  backup_verify_restored_services

  log_ok "[restore] Backup ${backup_id} was restored successfully."
}
