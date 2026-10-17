#!/usr/bin/env bash
set -Eeuo pipefail

admin_install_worker() {
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  admin_check || return 1
  admin_configure || return 1
  local candidate unit_file="/etc/systemd/system/${ADMIN_MAINTENANCE_UNIT}"
  if run_as_root test -e "${unit_file}" || run_as_root test -L "${unit_file}"; then
    if run_as_root test -L "${unit_file}" ||
      ! run_as_root grep -Fq '# Managed by AI Customer Platform Installer unified Admin.' "${unit_file}"; then
      log_error "Refusing to replace an unmanaged or linked maintenance unit."; return 1
    fi
  fi
  candidate="$(mktemp)"
  if ! sed -e "s|{{INSTALLER_ROOT}}|${INSTALLER_ROOT}|g" -e "s|{{CONFIG_FILE}}|${CONFIG_FILE}|g" \
    "${INSTALLER_ROOT}/templates/admin/maintenance.service.template" >"${candidate}"; then
    rm -f -- "${candidate}"; return 1
  fi
  if run_as_root test -f "${unit_file}" &&
    run_as_root systemctl is-active --quiet "${ADMIN_MAINTENANCE_UNIT}" &&
    ! run_as_root cmp -s -- "${candidate}" "${unit_file}"; then
    rm -f -- "${candidate}"
    log_error '[admin] Active maintenance unit paths changed. Finish maintenance jobs and stop the configured unit before re-running; no active worker was interrupted.'
    return 1
  fi
  # The executor is privileged but accepts only fixed maintenance operations.
  if ! run_as_root install -d -m 0700 "${ADMIN_MAINTENANCE_STATE_DIR}" "${ADMIN_MAINTENANCE_SOCKET_DIR}" ||
    ! run_as_root install -m 0644 "${candidate}" "${unit_file}"; then
    rm -f -- "${candidate}"; return 1
  fi
  rm -f -- "${candidate}"
  run_as_root systemctl daemon-reload || return 1
  # Do not restart a worker that may be executing a backup or upgrade.
  run_as_root systemctl enable --now "${ADMIN_MAINTENANCE_UNIT}" || return 1
  # An old process keeps the old Python allow-list even though files changed.
  # A separate fixed one-shot waits for any in-flight upgrade/restore to finish;
  # it never interrupts the very maintenance job that installed this version.
  local worker_version="${ADMIN_MAINTENANCE_STATE_DIR}/worker.version"
  local expected_version; expected_version="$(sha256sum "${INSTALLER_ROOT}/platform_admin/maintenance_worker.py" 2>/dev/null | cut -d ' ' -f1)" || expected_version=""
  if command_exists systemd-run && [[ -n "${expected_version}" ]] &&
    run_as_root test -f "${ADMIN_MAINTENANCE_STATE_DIR}/jobs.sqlite3" &&
    ! run_as_root grep -Fxq "${expected_version}" "${worker_version}" 2>/dev/null &&
    ! run_as_root systemctl is-active --quiet "${ADMIN_MAINTENANCE_UNIT%.service}-refresh.service"; then
    run_as_root systemd-run --collect --unit="${ADMIN_MAINTENANCE_UNIT%.service}-refresh" \
      --property=Type=exec --property=StandardOutput=null --property=StandardError=null \
      /usr/bin/python3 "${INSTALLER_ROOT}/platform_admin/worker_refresh.py" \
      "${ADMIN_MAINTENANCE_STATE_DIR}/jobs.sqlite3" "${ADMIN_MAINTENANCE_UNIT}" >/dev/null || {
        log_error 'Could not schedule the idle-only maintenance executor refresh.'; return 1;
      }
  fi
}

admin_install() {
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  admin_install_worker || return 1
  ai_service_compose config --quiet || return 1
  ai_service_compose build ai-admin || return 1
  ai_service_compose up -d ai-admin || return 1
  log_ok "[admin] The single Admin service and restricted maintenance executor were deployed."
}
