#!/usr/bin/env bash
set -Eeuo pipefail

admin_verify() {
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  run_as_root systemctl is-active --quiet "${ADMIN_MAINTENANCE_UNIT}" || {
    log_error "[admin] Maintenance executor is not active."; return 1;
  }
  ai_service_wait_for_http "Unified Admin" "${AI_ADMIN_HTTP_CHECK_URL}"
  log_ok "[admin] Unified Admin health check passed."
}
