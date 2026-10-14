#!/usr/bin/env bash
set -Eeuo pipefail

AI_SERVICE_COMPOSE_FILE="${AI_SERVICE_COMPOSE_FILE:-}"

ai_service_initialize_config() {
  AI_SERVICE_COMPOSE_FILE="${AI_SERVICE_COMPOSE_FILE:-${INSTALLER_ROOT}/stacks/ai-service/compose.yaml}"
}

ai_service_compose() {
  docker_execute compose \
    --project-name "${AI_SERVICE_COMPOSE_PROJECT}" \
    --env-file "${CONFIG_FILE}" \
    --file "${AI_SERVICE_COMPOSE_FILE}" \
    "$@"
}

ai_service_check() {
  ai_service_initialize_config
  log_info "[ai-service] Checking deployment prerequisites."

  if ! docker_engine_is_available; then
    log_error "AI Service deployment requires Docker Engine."
    return 1
  fi

  if ! docker_compose_is_available; then
    log_error "AI Service deployment requires the Docker Compose plugin."
    return 1
  fi

  if ! docker_current_user_can_access_daemon &&
    ! current_user_is_root &&
    ! command_exists sudo; then
    log_error "The current user cannot access the Docker daemon for AI Service deployment."
    return 1
  fi

  if ! file_is_readable "${CONFIG_FILE}"; then
    log_error "AI Service deployment requires a readable .env configuration file."
    return 1
  fi

  if ! file_is_readable "${AI_SERVICE_COMPOSE_FILE}"; then
    log_error "AI Service Compose file is unavailable: ${AI_SERVICE_COMPOSE_FILE}"
    return 1
  fi

  log_ok "[ai-service] Docker, Compose, and deployment files are available."
}
