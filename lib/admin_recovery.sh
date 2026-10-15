#!/usr/bin/env bash
set +x
set -Eeuo pipefail

manage_reset_admin_password() {
  local mode="${1:-}" project service key
  command_exists python3 || { log_error 'Administrator recovery requires host Python 3.'; return 1; }
  command_exists docker || { log_error 'Administrator recovery requires local Docker.'; return 1; }
  config_load_installer_configuration "${CONFIG_FILE:-${INSTALLER_ROOT}/.env}"
  project="${AI_SERVICE_COMPOSE_PROJECT:-}"
  service="${BACKUP_AI_ADMIN_SERVICE:-}"
  [[ "${project}" =~ ^[a-z0-9][a-z0-9_-]*$ && "${service}" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || {
    log_error 'Administrator recovery requires a valid configured Compose project and Admin service.'; return 1;
  }
  # Config is read only. The recovery subprocess does not need the platform's
  # exported credentials, and never receives the replacement through env.
  for key in "${!CONFIG_CONTEXT_KEYS[@]}"; do export -n "${key}"; done
  local -a options=(--project "${project}" --service "${service}")
  [[ "${mode}" != --manual ]] || options+=(--manual)
  python3 "${INSTALLER_ROOT}/platform_admin/recovery.py" "${options[@]}"
}
