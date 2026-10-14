#!/usr/bin/env bash
set -Eeuo pipefail

admin_check() {
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  command_exists python3 || { log_error "Unified Admin requires python3 on the host."; return 1; }
  command_exists systemctl || { log_error "Unified Admin maintenance requires systemd."; return 1; }
  docker_engine_is_available && docker_compose_is_available || return 1
  local version
  version="$(docker compose version --short)" || return 1
  version="${version#v}"
  if [[ ! "${version}" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]] ||
    (( BASH_REMATCH[1] < 2 || (BASH_REMATCH[1] == 2 && (BASH_REMATCH[2] < 24 || (BASH_REMATCH[2] == 24 && BASH_REMATCH[3] < 4))) )); then
    log_error 'Unified Admin requires Docker Compose 2.24.4 or newer; existing Docker was not replaced.'; return 1
  fi
  log_ok "[admin] Host executor and Docker prerequisites are available."
}
