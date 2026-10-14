#!/usr/bin/env bash
set -Eeuo pipefail

admin_configure() {
  validation_is_boolean "${ADMIN_ENABLED:-false}" || return 1
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  local password="${ADMIN_PASSWORD:-}"
  validation_require_keys ADMIN_USERNAME ADMIN_SESSION_SECRET ADMIN_MAINTENANCE_TOKEN SERVICE_MANAGEMENT_TOKEN ADMIN_API_URL ADMIN_MAINTENANCE_STATE_DIR ADMIN_MAINTENANCE_SOCKET_DIR ADMIN_MAINTENANCE_UNIT
  [[ "${ADMIN_USERNAME}" == admin ]] || { log_error "ADMIN_USERNAME must be admin."; return 1; }
  if [[ -n "${ADMIN_PASSWORD_HASH:-}" ]]; then
    [[ "${ADMIN_PASSWORD_HASH}" =~ ^pbkdf2_sha256:600000:[a-f0-9]{32}:[a-f0-9]{64}$ ]] || { log_error "Legacy administrator hash is invalid."; return 1; }
  elif [[ "${password}" != admin ]] &&
    (( ${#password} < 6 || ${#password} > 1024 )); then
    log_error "ADMIN_PASSWORD must be admin initially, or contain 6-1024 characters."; return 1
  fi
  [[ ${#ADMIN_SESSION_SECRET} -ge 32 && ${#ADMIN_MAINTENANCE_TOKEN} -ge 32 ]] || return 1
  [[ ${#SERVICE_MANAGEMENT_TOKEN} -ge 32 ]] || { log_error 'SERVICE_MANAGEMENT_TOKEN must contain at least 32 characters.'; return 1; }
  [[ "${AI_ADMIN_BIND_ADDRESS}" == "127.0.0.1" ]] || { log_error "Unified Admin must bind to loopback behind HTTPS Nginx."; return 1; }
  validation_is_http_url "${ADMIN_API_URL}" || return 1
  validation_is_positive_integer "${ADMIN_SESSION_TTL_SECONDS}" || return 1
  ((ADMIN_SESSION_TTL_SECONDS >= 60 && ADMIN_SESSION_TTL_SECONDS <= 86400)) || return 1
  validation_is_pinned_container_image "${ADMIN_PYTHON_IMAGE}" || return 1
  [[ "${ADMIN_MAINTENANCE_UNIT}" =~ ^[a-z0-9][a-z0-9-]*\.service$ ]] || return 1
  local path
  for path in "${INSTALLER_ROOT}" "${CONFIG_FILE}" "${ADMIN_MAINTENANCE_STATE_DIR}" "${ADMIN_MAINTENANCE_SOCKET_DIR}"; do
    validation_is_safe_absolute_path "${path}" || return 1
    [[ ! "${path}" =~ [\"\%\$\`\\\|\&] && "${path}" != *$'\n'* && "${path}" != *$'\r'* ]] || return 1
  done
  for path in "${ADMIN_MAINTENANCE_STATE_DIR}" "${ADMIN_MAINTENANCE_SOCKET_DIR}"; do
    case "$(readlink -m -- "${path}")" in
      /|/etc|/var|/var/lib|/run|/home|/root|/tmp|/usr|/opt|"${INSTALLER_ROOT}"|"$(dirname -- "${CONFIG_FILE}")")
        log_error "Admin maintenance requires dedicated private directories, not a system or project root."; return 1 ;;
    esac
    local component="${path}"
    while [[ "${component}" != / ]]; do
      [[ ! -L "${component}" ]] || { log_error "Admin maintenance paths cannot contain symbolic links."; return 1; }
      component="$(dirname -- "${component}")"
    done
    if [[ -d "${path}" && "$(stat -c %a -- "${path}")" != 700 ]]; then
      log_error "Existing Admin maintenance directories must already be private (0700)."; return 1
    fi
  done
  log_ok "[admin] Unified Admin configuration validated without displaying secrets."
}
