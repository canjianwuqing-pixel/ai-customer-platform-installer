#!/usr/bin/env bash
set -Eeuo pipefail

docker_verify() {
  local docker_version
  local compose_version
  local daemon_version

  docker_initialize_config
  log_info "[docker] Verifying the Docker installation."

  if ! docker_engine_is_available; then
    log_error "Docker verification failed: the docker command is unavailable."
    return 1
  fi

  docker_version="$(docker --version 2>/dev/null || true)"
  if [[ -z "${docker_version}" ]]; then
    log_error "Docker verification failed: unable to read the Docker version."
    return 1
  fi
  log_ok "Docker version: ${docker_version}."

  if ! docker_compose_is_available; then
    log_error "Docker verification failed: the Docker Compose Plugin is unavailable."
    return 1
  fi

  compose_version="$(docker compose version 2>/dev/null || true)"
  log_ok "Docker Compose version: ${compose_version}."

  if ! docker_service_is_active; then
    log_error "Docker verification failed: the ${DOCKER_SERVICE_NAME} service is not active."
    return 1
  fi
  log_ok "Docker service status: active."

  if ! daemon_version="$(docker_execute version --format '{{.Server.Version}}')"; then
    log_error "Docker verification failed: the Docker daemon is not accessible."
    return 1
  fi
  log_ok "Docker daemon version: ${daemon_version:-available}."

  if docker_current_user_can_access_daemon; then
    log_ok "Docker daemon access: available to the current user."
  else
    log_warn "Docker daemon access: available only through elevated privileges in the current session."
  fi

  log_ok "[docker] Docker verification completed successfully."
}
