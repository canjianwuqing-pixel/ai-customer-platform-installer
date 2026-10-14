#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_FORMAT_VERSION="1"

backup_initialize_config() {
  chatwoot_initialize_config
  ai_service_initialize_config
  domain_initialize_config
}

backup_service_list_contains() {
  local service_list="$1"
  local expected_service="$2"
  local service_name
  local -a service_names=()

  read -r -a service_names <<<"${service_list}"
  for service_name in "${service_names[@]}"; do
    [[ "${service_name}" == "${expected_service}" ]] && return 0
  done
  return 1
}

backup_validate_config() {
  local service_list

  validation_require_keys \
    BACKUP_DIR \
    BACKUP_RETENTION \
    BACKUP_COMPRESS \
    BACKUP_TOOL_IMAGE \
    BACKUP_CHATWOOT_DATABASE_SERVICE \
    BACKUP_CHATWOOT_STORAGE_SERVICE \
    BACKUP_CHATWOOT_APP_SERVICES \
    BACKUP_CHATWOOT_REQUIRED_SERVICES \
    BACKUP_AI_DATA_SERVICE \
    BACKUP_AI_ADMIN_SERVICE \
    BACKUP_AI_APP_SERVICES \
    BACKUP_AI_REQUIRED_SERVICES \
    BACKUP_CHATWOOT_STORAGE_PATH \
    CHATWOOT_VERSION \
    POSTGRES_VERSION \
    REDIS_VERSION \
    AI_SERVICE_DATA_PATH \
    AI_ADMIN_DATA_PATH
  if config_ai_enabled; then validation_require_keys AI_SERVICE_IMAGE AI_ADMIN_IMAGE; fi

  if ! validation_is_safe_absolute_path "${BACKUP_DIR}"; then
    log_error "BACKUP_DIR must be a safe absolute path other than the filesystem root."
    return 1
  fi

  if ! validation_is_positive_integer "${BACKUP_RETENTION}"; then
    log_error "BACKUP_RETENTION must be a positive integer."
    return 1
  fi

  if ! validation_is_boolean "${BACKUP_COMPRESS}"; then
    log_error "BACKUP_COMPRESS must be true or false."
    return 1
  fi

  if ! validation_is_pinned_container_image "${BACKUP_TOOL_IMAGE}"; then
    log_error "BACKUP_TOOL_IMAGE must use a fixed tag or sha256 digest."
    return 1
  fi

  for service_list in \
    "${BACKUP_CHATWOOT_DATABASE_SERVICE}" \
    "${BACKUP_CHATWOOT_STORAGE_SERVICE}" \
    "${BACKUP_CHATWOOT_APP_SERVICES}" \
    "${BACKUP_CHATWOOT_REQUIRED_SERVICES}" \
    "${BACKUP_AI_DATA_SERVICE}" \
    "${BACKUP_AI_ADMIN_SERVICE}" \
    "${BACKUP_AI_APP_SERVICES}" \
    "${BACKUP_AI_REQUIRED_SERVICES}"; do
    if ! validation_is_safe_service_list "${service_list}"; then
      log_error "Backup service names must use valid Docker Compose service-name characters."
      return 1
    fi
  done

  if ! validation_is_safe_container_path "${BACKUP_CHATWOOT_STORAGE_PATH}" ||
    ! validation_is_safe_container_path "${AI_SERVICE_DATA_PATH}" ||
    ! validation_is_safe_container_path "${AI_ADMIN_DATA_PATH}"; then
    log_error "Backup source paths must be safe absolute container paths."
    return 1
  fi

  if backup_service_list_contains \
    "${BACKUP_CHATWOOT_APP_SERVICES}" "${BACKUP_CHATWOOT_DATABASE_SERVICE}"; then
    log_error "The PostgreSQL service must remain outside BACKUP_CHATWOOT_APP_SERVICES."
    return 1
  fi

  if ! backup_service_list_contains \
    "${BACKUP_CHATWOOT_APP_SERVICES}" "${BACKUP_CHATWOOT_STORAGE_SERVICE}" ||
    ! backup_service_list_contains \
      "${BACKUP_CHATWOOT_REQUIRED_SERVICES}" "${BACKUP_CHATWOOT_DATABASE_SERVICE}" ||
    ! backup_service_list_contains \
      "${BACKUP_CHATWOOT_REQUIRED_SERVICES}" "${BACKUP_CHATWOOT_STORAGE_SERVICE}"; then
    log_error "Chatwoot backup services must be included in their configured app and required service lists."
    return 1
  fi

  if [[ "${BACKUP_AI_DATA_SERVICE}" == "${BACKUP_AI_ADMIN_SERVICE}" ]] ||
    ! backup_service_list_contains \
      "${BACKUP_AI_APP_SERVICES}" "${BACKUP_AI_DATA_SERVICE}" ||
    ! backup_service_list_contains \
      "${BACKUP_AI_APP_SERVICES}" "${BACKUP_AI_ADMIN_SERVICE}" ||
    ! backup_service_list_contains \
      "${BACKUP_AI_REQUIRED_SERVICES}" "${BACKUP_AI_DATA_SERVICE}" ||
    ! backup_service_list_contains \
      "${BACKUP_AI_REQUIRED_SERVICES}" "${BACKUP_AI_ADMIN_SERVICE}"; then
    log_error "AI data and administration services must be distinct and included in the app and required service lists."
    return 1
  fi
}

backup_check_command_dependencies() {
  local command_name

  for command_name in date find readlink sha256sum sort stat tar xargs; do
    if ! command_exists "${command_name}"; then
      log_error "Backup and restore require the '${command_name}' command."
      return 1
    fi
  done
}

backup_check() {
  backup_initialize_config
  log_info "[backup] Checking backup and restore prerequisites."

  backup_validate_config
  backup_check_command_dependencies

  if ! docker_engine_is_available || ! docker_compose_is_available; then
    log_error "Backup and restore require Docker Engine and the Docker Compose plugin."
    return 1
  fi

  if ! docker_current_user_can_access_daemon &&
    ! current_user_is_root &&
    ! command_exists sudo; then
    log_error "The current user cannot access the Docker daemon for backup operations."
    return 1
  fi

  for required_file in "${CONFIG_FILE}" "${CHATWOOT_COMPOSE_FILE}" "${AI_SERVICE_COMPOSE_FILE}"; do
    if ! file_is_readable "${required_file}"; then
      log_error "Required backup source file is unavailable: ${required_file}"
      return 1
    fi
  done

  if ! run_as_root test -r "${NGINX_SITE_CONFIG_PATH}"; then
    log_error "Managed Nginx site configuration is unavailable: ${NGINX_SITE_CONFIG_PATH}"
    return 1
  fi

  log_ok "[backup] Backup and restore prerequisites are valid."
}
