#!/usr/bin/env bash
set -Eeuo pipefail

admin_install() {
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  admin_check
  admin_configure
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
  # The executor is privileged but accepts only fixed maintenance operations.
  if ! run_as_root install -d -m 0700 "${ADMIN_MAINTENANCE_STATE_DIR}" "${ADMIN_MAINTENANCE_SOCKET_DIR}" ||
    ! run_as_root install -m 0644 "${candidate}" "${unit_file}"; then
    rm -f -- "${candidate}"; return 1
  fi
  rm -f -- "${candidate}"
  run_as_root systemctl daemon-reload
  # Do not restart a worker that may be executing a backup or upgrade.
  run_as_root systemctl enable --now "${ADMIN_MAINTENANCE_UNIT}"
  ai_service_compose config --quiet
  ai_service_compose build ai-admin
  ai_service_compose up -d ai-admin
  log_ok "[admin] The single Admin service and restricted maintenance executor were deployed."
}
