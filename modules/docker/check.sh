#!/usr/bin/env bash
set -Eeuo pipefail

DOCKER_SERVICE_NAME="${DOCKER_SERVICE_NAME:-}"

docker_initialize_config() {
  DOCKER_SERVICE_NAME="${DOCKER_SERVICE_NAME:-docker}"
}

docker_engine_is_available() {
  command_exists docker
}

docker_compose_is_available() {
  docker_engine_is_available && docker compose version >/dev/null 2>&1
}

docker_service_is_active() {
  docker_initialize_config

  if command_exists systemctl; then
    systemctl is-active --quiet "${DOCKER_SERVICE_NAME}"
    return $?
  fi

  if command_exists service; then
    service "${DOCKER_SERVICE_NAME}" status >/dev/null 2>&1
    return $?
  fi

  docker info >/dev/null 2>&1
}

docker_current_user_can_access_daemon() {
  docker_engine_is_available && docker info >/dev/null 2>&1
}

docker_execute() {
  if docker_current_user_can_access_daemon || current_user_is_root; then
    docker "$@"
    return $?
  fi

  run_as_root docker "$@"
}

docker_installation_privileges_are_available() {
  current_user_is_root || command_exists sudo
}

docker_check_current_user_permissions() {
  local user_name
  user_name="$(id -un 2>/dev/null || printf 'unknown')"

  if ! docker_engine_is_available; then
    if current_user_is_root; then
      log_ok "Docker permissions: running as root."
      return 0
    fi

    if command_exists sudo; then
      log_warn "Docker permissions: ${user_name} will use sudo during Docker installation."
      return 0
    fi

    log_error "Docker permissions: Docker is missing and ${user_name} cannot obtain root privileges."
    return 1
  fi

  if docker_current_user_can_access_daemon; then
    log_ok "Docker permissions: ${user_name} can access the Docker daemon."
    return 0
  fi

  if current_user_is_root; then
    log_warn "Docker permissions: running as root, but the daemon is not currently accessible."
    return 0
  fi

  if command_exists sudo; then
    log_warn "Docker permissions: ${user_name} cannot access the daemon directly; privileged Docker commands will use sudo."
    return 0
  fi

  log_error "Docker permissions: ${user_name} cannot access the Docker daemon and sudo is unavailable."
  return 1
}

docker_check() {
  local failure_count=0
  local docker_version
  local compose_version

  docker_initialize_config
  log_info "[docker] Checking the Docker environment."

  if docker_engine_is_available; then
    docker_version="$(docker --version 2>/dev/null || true)"
    log_ok "Docker CLI detected: ${docker_version:-version unavailable}."
  else
    log_warn "Docker CLI is not installed; the install step will add it."
  fi

  if docker_compose_is_available; then
    compose_version="$(docker compose version 2>/dev/null || true)"
    log_ok "Docker Compose plugin detected: ${compose_version:-version unavailable}."
  else
    log_warn "Docker Compose plugin is not available; the install step will add it when safe."
  fi

  if docker_engine_is_available && docker_service_is_active; then
    log_ok "Docker service is active."
  elif docker_engine_is_available; then
    log_warn "Docker service is not active; the install step will attempt to start it."
  else
    log_warn "Docker service status is unavailable until Docker is installed."
  fi

  docker_check_current_user_permissions || failure_count=$((failure_count + 1))

  if ((failure_count > 0)); then
    log_error "[docker] Environment check failed: ${failure_count} check(s) did not pass."
    return 1
  fi

  log_ok "[docker] Environment check completed."
}
