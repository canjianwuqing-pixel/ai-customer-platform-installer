#!/usr/bin/env bash
set -Eeuo pipefail

admin_prepare_credentials() {
  [[ "${ADMIN_ENABLED:-false}" == true ]] || return 0
  local restore_trace=false
  [[ "$-" != *x* ]] || { restore_trace=true; set +x; }
  local -a keys=(ADMIN_USERNAME ADMIN_PASSWORD ADMIN_SESSION_SECRET ADMIN_MAINTENANCE_TOKEN AI_ADMIN_PASSWORD AI_ADMIN_SESSION_SECRET AI_ADMIN_USERNAME)
  ADMIN_USERNAME=admin
  if [[ -n "${ADMIN_PASSWORD_HASH:-}" ]]; then
    # Hashes cannot be decoded. Runtime verifies the old password before
    # migrating it into persistent ADMIN_PASSWORD configuration.
    ADMIN_PASSWORD="${ADMIN_PASSWORD-}"
  elif [[ -z "${CONFIG_USER_KEYS[ADMIN_PASSWORD]+configured}" && -n "${AI_ADMIN_PASSWORD:-}" &&
          ( -z "${ADMIN_PASSWORD:-}" || "${ADMIN_PASSWORD}" == admin ) ]]; then
    ADMIN_PASSWORD="${AI_ADMIN_PASSWORD}"
  else
    ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
  fi
  if [[ -z "${ADMIN_SESSION_SECRET:-}" ]]; then
    if [[ -n "${AI_ADMIN_SESSION_SECRET:-}" ]]; then
      ADMIN_SESSION_SECRET="${AI_ADMIN_SESSION_SECRET}"
    else
      secrets_generate_hex 32; ADMIN_SESSION_SECRET="${SECRETS_GENERATED_VALUE}"
    fi
  fi
  if [[ -z "${ADMIN_MAINTENANCE_TOKEN:-}" ]]; then
    secrets_generate_hex 32; ADMIN_MAINTENANCE_TOKEN="${SECRETS_GENERATED_VALUE}"
  fi
  [[ ${#ADMIN_SESSION_SECRET} -ge 32 && ${#ADMIN_MAINTENANCE_TOKEN} -ge 32 ]] || {
    log_error "Admin session and maintenance secrets must contain at least 32 characters."; return 1;
  }
  # Keep deprecated inputs for recovery if deployment fails. The unified
  # container never consumes them, so they do not create a second account.
  export ADMIN_USERNAME ADMIN_PASSWORD ADMIN_SESSION_SECRET ADMIN_MAINTENANCE_TOKEN
  export AI_ADMIN_PASSWORD AI_ADMIN_SESSION_SECRET AI_ADMIN_USERNAME
  config_save_keys "${CONFIG_FILE}" "${keys[@]}"
  log_ok "[admin] Single administrator configuration saved without logging credentials."
  [[ "${restore_trace}" != true ]] || set -x
}

admin_print_completion() {
  local restore_trace=false
  [[ "$-" != *x* ]] || { restore_trace=true; set +x; }
  printf '\n统一管理后台：\nhttps://%s/admin\n\n管理员账号：\nadmin\n' "${DOMAIN}"
  if [[ -n "${ADMIN_PASSWORD_HASH:-}" ]]; then
    printf '\n%s\n' '密码：保留已有密码；使用原密码登录后自动迁移配置。'
  elif [[ "${ADMIN_PASSWORD:-admin}" == admin ]]; then
    printf '\n初始密码：\nadmin\n'
  else
    printf '\n%s\n' '密码：使用已配置的密码（不显示）。'
  fi
  printf '\n%s\n' '请首次登录后修改密码。' '如已修改过密码，请使用已保存的新密码；重复安装不会重置它。'
  [[ "${restore_trace}" != true ]] || set -x
}
