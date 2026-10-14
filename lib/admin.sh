#!/usr/bin/env bash
set -Eeuo pipefail

admin_initial_password() {
  local once_file nonce
  once_file="$(secrets_admin_once_file)" || return 1
  [[ -f "${once_file}" && ! -L "${once_file}" ]] || return 1
  nonce="$(<"${once_file}")"
  [[ "${nonce}" =~ ^[a-f0-9]{64}$ ]] || return 1
  printf '%s' "${ADMIN_SESSION_SECRET}" | python3 "${INSTALLER_ROOT}/platform_admin/passwords.py" --derive "${nonce}"
}

admin_prepare_credentials() {
  [[ "${ADMIN_ENABLED:-false}" == "true" ]] || return 0
  # Never permit bash -x to disclose credentials, including the pipe below.
  local restore_trace=false
  [[ "$-" != *x* ]] || { restore_trace=true; set +x; }
  local initial_password="" once_file
  local -a keys=(ADMIN_USERNAME ADMIN_PASSWORD_HASH ADMIN_SESSION_SECRET ADMIN_MAINTENANCE_TOKEN AI_ADMIN_PASSWORD AI_ADMIN_SESSION_SECRET AI_ADMIN_USERNAME)
  command_exists python3 || { log_error "Unified Admin initialization requires python3."; return 1; }
  ADMIN_USERNAME="${ADMIN_USERNAME:-${AI_ADMIN_USERNAME:-admin}}"
  if [[ -z "${ADMIN_SESSION_SECRET:-}" ]]; then
    if [[ -n "${AI_ADMIN_SESSION_SECRET:-}" ]]; then
      ADMIN_SESSION_SECRET="${AI_ADMIN_SESSION_SECRET}"
    else
      secrets_generate_hex 32; ADMIN_SESSION_SECRET="${SECRETS_GENERATED_VALUE}"
    fi
  fi
  if [[ -z "${ADMIN_PASSWORD_HASH:-}" ]]; then
    once_file="$(secrets_admin_once_file)" || return 1
    if [[ -L "${once_file}" || (-e "${once_file}" && ! -f "${once_file}") ]]; then
      log_error "Pending Admin nonce must be a protected regular file."; return 1
    fi
    if [[ -n "${AI_ADMIN_PASSWORD:-}" ]]; then
      initial_password="${AI_ADMIN_PASSWORD}"
      [[ ! -f "${once_file}" ]] || rm -f -- "${once_file}"
    else
      secrets_ensure_admin_nonce || return 1
      initial_password="$(admin_initial_password)" || return 1
    fi
    ADMIN_PASSWORD_HASH="$(printf '%s' "${initial_password}" | python3 "${INSTALLER_ROOT}/platform_admin/passwords.py")" || {
      log_error "Unable to initialize the administrator password hash (minimum password length: 16)."; return 1;
    }
  fi
  if [[ -z "${ADMIN_MAINTENANCE_TOKEN:-}" ]]; then
    secrets_generate_hex 32; ADMIN_MAINTENANCE_TOKEN="${SECRETS_GENERATED_VALUE}"
  fi
  [[ "${ADMIN_PASSWORD_HASH}" =~ ^pbkdf2_sha256:600000:[a-f0-9]{32}:[a-f0-9]{64}$ ]] || {
    log_error "ADMIN_PASSWORD_HASH must be a valid installer password hash."; return 1;
  }
  [[ ${#ADMIN_SESSION_SECRET} -ge 32 && ${#ADMIN_MAINTENANCE_TOKEN} -ge 32 ]] || {
    log_error "Admin session and maintenance secrets must contain at least 32 characters."; return 1;
  }
  # One account only. Retire the old names after migrating their values.
  AI_ADMIN_PASSWORD="" AI_ADMIN_SESSION_SECRET="" AI_ADMIN_USERNAME=""
  export ADMIN_USERNAME ADMIN_PASSWORD_HASH ADMIN_SESSION_SECRET ADMIN_MAINTENANCE_TOKEN
  export AI_ADMIN_PASSWORD AI_ADMIN_SESSION_SECRET AI_ADMIN_USERNAME
  config_save_keys "${CONFIG_FILE}" "${keys[@]}"
  log_ok "[admin] Single administrator credentials are configured; only the password hash is saved."
  [[ "${restore_trace}" != true ]] || set -x
}

admin_print_completion() {
  local restore_trace=false
  [[ "$-" != *x* ]] || { restore_trace=true; set +x; }
  local once_file
  once_file="$(secrets_admin_once_file)" || return 1
  if [[ -L "${once_file}" || (-e "${once_file}" && ! -f "${once_file}") ]]; then
    log_error "Administrator nonce must be a regular file, not a symbolic link."
    return 1
  fi
  printf '\n统一管理后台：\nhttps://%s/admin\n管理员账号：\n%s\n' "${DOMAIN}" "${ADMIN_USERNAME}"
  if [[ -f "${once_file}" ]]; then
    local initial_password
    initial_password="$(admin_initial_password)" || {
      log_error "Unable to consume the initial password nonce."; return 1;
    }
    printf '%s\n%s' "${initial_password}" "${ADMIN_PASSWORD_HASH}" | python3 "${INSTALLER_ROOT}/platform_admin/passwords.py" --verify || {
      log_error "Initial password nonce does not match the configured hash."; return 1;
    }
    if ! printf '初始密码（仅显示一次）：\n%s\n请首次登录后修改密码。\n' "${initial_password}"; then
      log_error "Initial password could not be displayed; its nonce was retained for retry."
      return 1
    fi
    if ! rm -f -- "${once_file}"; then
      log_error "Unable to remove the displayed initial password nonce."
      return 1
    fi
  else
    printf '%s\n' '密码已设置，后台修改后的密码保存在受保护的数据卷中。'
  fi
  [[ "${restore_trace}" != true ]] || set -x
}
