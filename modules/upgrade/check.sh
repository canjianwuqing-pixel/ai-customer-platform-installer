#!/usr/bin/env bash
set -Eeuo pipefail

UPGRADE_RECORD_FORMAT_VERSION="1"

upgrade_initialize_config() {
  chatwoot_initialize_config
  ai_service_initialize_config
  backup_initialize_config
}

upgrade_service_list_contains() {
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

upgrade_validate_config() {
  validation_require_keys \
    UPGRADE_RECORD_DIR \
    UPGRADE_CHATWOOT_VERSION \
    UPGRADE_AI_SERVICE_IMAGE \
    UPGRADE_AI_ADMIN_IMAGE \
    UPGRADE_CHATWOOT_MIGRATION_SERVICE \
    UPGRADE_CHATWOOT_APP_SERVICES \
    UPGRADE_AI_APP_SERVICES

  if ! validation_is_safe_absolute_path "${UPGRADE_RECORD_DIR}"; then
    log_error "UPGRADE_RECORD_DIR must be a safe absolute path other than the filesystem root."
    return 1
  fi

  if ! validation_is_exact_semver_tag "${UPGRADE_CHATWOOT_VERSION}"; then
    log_error "UPGRADE_CHATWOOT_VERSION must be an exact version tag; moving tags are forbidden."
    return 1
  fi

  if ! validation_is_pinned_container_image "${UPGRADE_AI_SERVICE_IMAGE}" ||
    ! validation_is_pinned_container_image "${UPGRADE_AI_ADMIN_IMAGE}"; then
    log_error "Upgrade AI images must use fixed tags or sha256 digests; latest is forbidden."
    return 1
  fi

  if ! validation_is_compose_service_name "${UPGRADE_CHATWOOT_MIGRATION_SERVICE}" ||
    ! validation_is_safe_service_list "${UPGRADE_CHATWOOT_APP_SERVICES}" ||
    ! validation_is_safe_service_list "${UPGRADE_AI_APP_SERVICES}"; then
    log_error "Upgrade service names must use valid Docker Compose service-name characters."
    return 1
  fi

  if ! upgrade_service_list_contains \
    "${UPGRADE_CHATWOOT_APP_SERVICES}" "${UPGRADE_CHATWOOT_MIGRATION_SERVICE}"; then
    log_error "The Chatwoot migration service must be included in UPGRADE_CHATWOOT_APP_SERVICES."
    return 1
  fi
}

upgrade_compare_chatwoot_versions() {
  local current_without_prefix="${CHATWOOT_VERSION#v}"
  local target_without_prefix="${UPGRADE_CHATWOOT_VERSION#v}"
  local oldest

  [[ "${CHATWOOT_VERSION}" == "${UPGRADE_CHATWOOT_VERSION}" ]] && return 0
  oldest="$(printf '%s\n%s\n' "${current_without_prefix}" "${target_without_prefix}" | sort -V | head -n 1)"
  if [[ "${oldest}" != "${current_without_prefix}" ]]; then
    log_error "UPGRADE_CHATWOOT_VERSION is older than CHATWOOT_VERSION; use rollback for downgrades."
    return 1
  fi
}

upgrade_validate_target_change() {
  if [[ "${CHATWOOT_VERSION}" == "${UPGRADE_CHATWOOT_VERSION}" ]] &&
    [[ "${AI_SERVICE_IMAGE}" == "${UPGRADE_AI_SERVICE_IMAGE}" ]] &&
    [[ "${AI_ADMIN_IMAGE}" == "${UPGRADE_AI_ADMIN_IMAGE}" ]]; then
    log_error "The configured upgrade targets are identical to the active versions."
    return 1
  fi

  upgrade_compare_chatwoot_versions
}

upgrade_verify_running_service_list() {
  local compose_function="$1"
  local configured_services="$2"
  local stack_label="$3"
  local running_services
  local service_name
  local -a service_names=()

  running_services="$(${compose_function} ps --status running --services)" || {
    log_error "Unable to inspect the ${stack_label} installation state."
    return 1
  }
  read -r -a service_names <<<"${configured_services}"

  for service_name in "${service_names[@]}"; do
    if ! grep -Fqx -- "${service_name}" <<<"${running_services}"; then
      log_error "Required ${stack_label} service is not running: ${service_name}"
      return 1
    fi
  done
}

upgrade_validate_target_compose() {
  log_info "[upgrade] Validating target Compose configuration without printing resolved secrets."

  if ! CHATWOOT_VERSION="${UPGRADE_CHATWOOT_VERSION}" \
    chatwoot_compose config --quiet; then
    log_error "Target Chatwoot Compose configuration is invalid."
    return 1
  fi

  if ! AI_SERVICE_IMAGE="${UPGRADE_AI_SERVICE_IMAGE}" \
    AI_ADMIN_IMAGE="${UPGRADE_AI_ADMIN_IMAGE}" \
    ai_service_compose config --quiet; then
    log_error "Target AI Service Compose configuration is invalid."
    return 1
  fi
}

upgrade_check() {
  upgrade_initialize_config
  log_info "[upgrade] Checking the current installation and configured targets."

  if [[ "${CONFIG_LOADED}" != "true" ]] || ! file_is_readable "${CONFIG_FILE}"; then
    log_error "Upgrade requires a loaded, readable .env configuration file."
    return 1
  fi

  if [[ -L "${CONFIG_FILE}" ]] || [[ ! -w "${CONFIG_FILE}" ]] ||
    [[ ! -w "$(dirname -- "${CONFIG_FILE}")" ]]; then
    log_error "Upgrade requires a writable, non-symbolic-link .env file and parent directory."
    return 1
  fi

  command_exists sort || {
    log_error "Upgrade version validation requires the sort command."
    return 1
  }

  chatwoot_configure
  ai_service_configure
  backup_check
  upgrade_validate_config
  upgrade_validate_target_change
  upgrade_validate_target_compose
  upgrade_verify_running_service_list \
    chatwoot_compose "${BACKUP_CHATWOOT_REQUIRED_SERVICES}" "Chatwoot"
  upgrade_verify_running_service_list \
    ai_service_compose "${BACKUP_AI_REQUIRED_SERVICES}" "AI Service"

  log_ok "[upgrade] Current installation and fixed-version upgrade targets are valid."
}

upgrade_check_rollback_prerequisites() {
  upgrade_initialize_config
  log_info "[rollback] Checking rollback prerequisites."

  chatwoot_configure
  ai_service_configure
  backup_check
  upgrade_validate_config

  log_ok "[rollback] Rollback prerequisites are valid."
}
