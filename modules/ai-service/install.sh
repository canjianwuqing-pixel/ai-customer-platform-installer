#!/usr/bin/env bash
set -Eeuo pipefail

ai_service_install() {
  ai_service_initialize_config
  log_info "[ai-service] Validating the Docker Compose model without printing resolved secrets."

  if ! ai_service_compose config --quiet; then
    log_error "AI Service Compose configuration validation failed."
    return 1
  fi
  log_ok "[ai-service] Docker Compose configuration is valid."

  if [[ "${AI_RUNTIME_BUILD:-false}" == "true" ]]; then
    log_info "[ai-service] Building the configured first-party runtime image."
    if ! ai_service_compose build ai-service; then
      log_error "Failed to build AI Runtime or pull the administration image."
      return 1
    fi
    if [[ "${ADMIN_ENABLED:-false}" != true ]]; then
      ai_service_compose pull ai-admin || return 1
    fi
  else
    log_info "[ai-service] Pulling fixed-version FastAPI and administration images."
    local -a pull_services=(ai-service ai-admin)
    [[ "${ADMIN_ENABLED:-false}" != true ]] || pull_services=(ai-service)
    if ! ai_service_compose pull "${pull_services[@]}"; then
      log_error "Failed to pull one or more AI Service images."
      return 1
    fi
  fi

  log_info "[ai-service] Starting the FastAPI service and administration backend."
  local -a start_services=(ai-service ai-admin)
  [[ "${ADMIN_ENABLED:-false}" != true ]] || start_services=(ai-service)
  if ! ai_service_compose up -d "${start_services[@]}"; then
    log_error "Failed to start the AI Service stack."
    return 1
  fi

  log_ok "[ai-service] AI Service stack deployment completed."
}
