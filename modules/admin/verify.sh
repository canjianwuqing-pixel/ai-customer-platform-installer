#!/usr/bin/env bash
set -Eeuo pipefail

admin_verify() {
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  run_as_root systemctl is-active --quiet "${ADMIN_MAINTENANCE_UNIT}" || {
    log_error "[admin] Maintenance executor is not active."; return 1;
  }
  ai_service_wait_for_http "Unified Admin" "${AI_ADMIN_HTTP_CHECK_URL}" || return 1
  local probe deadline=$((SECONDS + AI_SERVICE_STARTUP_TIMEOUT_SECONDS))
  probe="$(<"${INSTALLER_ROOT}/modules/admin/readiness.py")"
  until ai_service_compose exec -T ai-admin python -c "${probe}" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
      log_error '[admin] Readiness failed: verify Management API interfaces, authentication and maintenance connectivity.'; return 1
    fi
    sleep "${AI_SERVICE_HEALTHCHECK_INTERVAL_SECONDS}"
  done
  log_ok "[admin] Unified Admin health check passed."
}
