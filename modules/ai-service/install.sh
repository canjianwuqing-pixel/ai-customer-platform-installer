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

  log_info "[ai-service] Pulling fixed-version FastAPI and administration images."
  if ! ai_service_compose pull; then
    log_error "Failed to pull one or more AI Service images."
    return 1
  fi

  log_info "[ai-service] Starting the FastAPI service and administration backend."
  if ! ai_service_compose up -d ai-service ai-admin; then
    log_error "Failed to start the AI Service stack."
    return 1
  fi

  log_ok "[ai-service] AI Service stack deployment completed."
}
