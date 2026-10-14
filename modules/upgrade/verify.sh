#!/usr/bin/env bash
set -Eeuo pipefail

upgrade_verify() {
  local record_directory="$1"
  local expected_chatwoot
  local expected_ai_service
  local expected_ai_admin

  expected_chatwoot="$(read_key_value_file "${record_directory}/record.env" CHATWOOT_TO)"
  expected_ai_service="$(read_key_value_file "${record_directory}/record.env" AI_SERVICE_TO)"
  expected_ai_admin="$(read_key_value_file "${record_directory}/record.env" AI_ADMIN_TO)"

  if [[ "${CHATWOOT_VERSION}" != "${expected_chatwoot}" ]] ||
    [[ "${AI_SERVICE_IMAGE}" != "${expected_ai_service}" ]] ||
    [[ "${AI_ADMIN_IMAGE}" != "${expected_ai_admin}" ]]; then
    log_error "Active image versions do not match the recorded upgrade targets."
    return 1
  fi

  if ! chatwoot_compose config --quiet || ! ai_service_compose config --quiet; then
    log_error "Active Compose configuration failed validation after upgrade."
    return 1
  fi

  chatwoot_verify
  ai_service_verify
  nginx_verify
  log_ok "[upgrade] Upgraded platform health checks passed."
}

upgrade_verify_rolled_back_versions() {
  local record_directory="$1"
  local expected_chatwoot
  local expected_ai_service
  local expected_ai_admin

  expected_chatwoot="$(read_key_value_file "${record_directory}/record.env" CHATWOOT_FROM)"
  expected_ai_service="$(read_key_value_file "${record_directory}/record.env" AI_SERVICE_FROM)"
  expected_ai_admin="$(read_key_value_file "${record_directory}/record.env" AI_ADMIN_FROM)"

  if [[ "${CHATWOOT_VERSION}" != "${expected_chatwoot}" ]] ||
    [[ "${AI_SERVICE_IMAGE}" != "${expected_ai_service}" ]] ||
    [[ "${AI_ADMIN_IMAGE}" != "${expected_ai_admin}" ]]; then
    log_error "Restored versions do not match the recorded pre-upgrade versions."
    return 1
  fi

  log_ok "[rollback] Pre-upgrade versions were restored."
}
