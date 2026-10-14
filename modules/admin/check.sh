#!/usr/bin/env bash
set -Eeuo pipefail

admin_check() {
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  command_exists python3 || { log_error "Unified Admin requires python3 on the host."; return 1; }
  command_exists systemctl || { log_error "Unified Admin maintenance requires systemd."; return 1; }
  docker_engine_is_available && docker_compose_is_available || return 1
  log_ok "[admin] Host executor and Docker prerequisites are available."
}
