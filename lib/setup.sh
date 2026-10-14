#!/usr/bin/env bash
set -Eeuo pipefail

SETUP_INPUT_VALUE=""
AI_SERVICE_ENABLED="false"

setup_template_file() {
  printf '%s\n' "${INSTALLER_ENV_TEMPLATE:-${INSTALLER_ROOT}/.env.example}"
}

setup_admin_password_pending_file() {
  printf '%s\n' "${INSTALLER_ADMIN_PASSWORD_PENDING_FILE:-${INSTALLER_ROOT}/.installer-admin-password-pending}"
}

setup_prompt_value() {
  local prompt_text="$1"
  local example_text="$2"
  local validator_function="$3"
  local sensitive="${4:-false}"
  local value

  while true; do
    printf '%s\n' "${prompt_text}" >&2
    if [[ -n "${example_text}" ]]; then
      printf '例如：%s\n' "${example_text}" >&2
    fi
    printf '> ' >&2

    if [[ "${sensitive}" == "true" && -t 0 ]]; then
      if ! IFS= read -r -s value; then
        printf '\n' >&2
        log_error "Installation input ended before all required values were provided."
        return 1
      fi
      printf '\n' >&2
    elif ! IFS= read -r value; then
      log_error "Installation input ended before all required values were provided."
      return 1
    fi

    if "${validator_function}" "${value}"; then
      SETUP_INPUT_VALUE="${value}"
      return 0
    fi

    log_warn "输入格式无效，请重新输入。"
  done
}

setup_collect_configuration() {
  local restore_xtrace="false"
  local -a changed_keys=()

  if [[ "$-" == *x* ]]; then
    restore_xtrace="true"
    set +x
  fi

  if [[ -z "${DOMAIN:-}" ]]; then
    setup_prompt_value "请输入网站域名：" "chat.example.com" validation_is_hostname
    DOMAIN="${SETUP_INPUT_VALUE}"
    export DOMAIN
    changed_keys+=(DOMAIN)
  fi

  if [[ -z "${EMAIL:-}" ]]; then
    setup_prompt_value "请输入管理员邮箱：" "admin@example.com" validation_is_email
    EMAIL="${SETUP_INPUT_VALUE}"
    export EMAIL
    changed_keys+=(EMAIL)
  fi

  if [[ -z "${LETSENCRYPT_EMAIL:-}" ]]; then
    LETSENCRYPT_EMAIL="${EMAIL}"
    export LETSENCRYPT_EMAIL
    changed_keys+=(LETSENCRYPT_EMAIL)
  fi

  if [[ -z "${AI_ADMIN_USERNAME:-}" ]]; then
    AI_ADMIN_USERNAME="admin"
    export AI_ADMIN_USERNAME
    changed_keys+=(AI_ADMIN_USERNAME)
  fi

  if ((${#changed_keys[@]} > 0)); then
    config_save_keys "${CONFIG_FILE}" "${changed_keys[@]}"
    log_ok "Saved installation wizard configuration without logging sensitive values."
  fi

  if [[ "${restore_xtrace}" == "true" ]]; then
    set -x
  fi
}

setup_prepare_configuration() {
  local template_file

  CONFIG_FILE="${CONFIG_FILE:-${INSTALLER_ROOT}/.env}"
  template_file="$(setup_template_file)"

  if [[ ! -e "${CONFIG_FILE}" && ! -L "${CONFIG_FILE}" ]]; then
    config_create_from_template "${CONFIG_FILE}" "${template_file}"
  fi

  config_load_installer_configuration "${CONFIG_FILE}"
  if [[ "${CONFIG_LOADED}" != "true" ]]; then
    log_error "The installation configuration could not be loaded."
    return 1
  fi

  config_materialize_missing_defaults \
    "${CONFIG_FILE}" \
    "$(config_images_file)" \
    "$(config_default_file)"

  setup_collect_configuration
}

setup_configure_optional_modules() {
  AI_SERVICE_ENABLED="false"

  validation_is_boolean "${ADMIN_ENABLED:-false}" || { log_error "ADMIN_ENABLED must be true or false."; return 1; }
  if [[ "${ADMIN_ENABLED:-false}" == true ]]; then
    if [[ -z "${AI_SERVICE_IMAGE:-}" ]]; then
      AI_SERVICE_IMAGE="${ADMIN_RUNTIME_IMAGE}"
      AI_RUNTIME_BUILD=true
    fi
    AI_ADMIN_IMAGE="${AI_ADMIN_IMAGE:-${ADMIN_DEFAULT_IMAGE}}"
    if [[ -z "${ADMIN_API_URL:-}" ]]; then
      ADMIN_API_URL="http://ai-service:${AI_SERVICE_CONTAINER_PORT}"
    fi
    export AI_SERVICE_IMAGE AI_RUNTIME_BUILD AI_ADMIN_IMAGE ADMIN_API_URL
    config_save_keys "${CONFIG_FILE}" AI_SERVICE_IMAGE AI_RUNTIME_BUILD AI_ADMIN_IMAGE ADMIN_API_URL
  fi

  if [[ -z "${AI_SERVICE_IMAGE:-}" ]]; then
    if [[ -n "${AI_ADMIN_IMAGE:-}" ]]; then
      log_warn "AI_ADMIN_IMAGE is set without AI_SERVICE_IMAGE; the optional AI Service module will be skipped."
    else
      log_info "AI Service image is not configured; the optional module will be skipped."
    fi
    export AI_SERVICE_ENABLED
    return 0
  fi

  if [[ -z "${AI_ADMIN_IMAGE:-}" ]]; then
    log_error "AI_ADMIN_IMAGE is required when AI_SERVICE_IMAGE enables the optional AI Service module."
    return 1
  fi

  AI_SERVICE_ENABLED="true"
  export AI_SERVICE_ENABLED
  log_info "AI Service images are configured; the optional module is enabled."
}

setup_mark_admin_password_pending() {
  local pending_file
  local temporary_file

  if [[ "${AI_ADMIN_PASSWORD_GENERATED:-false}" != "true" ]]; then
    return 0
  fi

  pending_file="$(setup_admin_password_pending_file)"
  if [[ -L "${pending_file}" || (-e "${pending_file}" && ! -f "${pending_file}") ]]; then
    log_error "Administrator password display marker must be a regular file."
    return 1
  fi
  if [[ ! -d "$(dirname -- "${pending_file}")" || ! -w "$(dirname -- "${pending_file}")" ]]; then
    log_error "Administrator password display marker directory is not writable."
    return 1
  fi

  temporary_file="$(mktemp "${pending_file}.setup.XXXXXX")" || {
    log_error "Unable to create the administrator password display marker."
    return 1
  }
  if ! chmod 600 "${temporary_file}" || ! mv -f -- "${temporary_file}" "${pending_file}"; then
    rm -f -- "${temporary_file}"
    log_error "Unable to record the pending one-time administrator password display."
    return 1
  fi
}

setup_print_completion() {
  local pending_file
  local password_line="初始密码：已设置（仅在首次安装成功时显示）"

  if [[ "${ADMIN_ENABLED:-false}" == true ]]; then
    printf '\nAI Customer Platform 安装完成\n网站地址：\nhttps://%s\n' "${DOMAIN}"
    admin_print_completion
    printf '\n维护命令：\n./manage.sh status\n./manage.sh logs\n./manage.sh restart\n'
    return 0
  fi

  pending_file="$(setup_admin_password_pending_file)"
  if [[ -L "${pending_file}" || (-e "${pending_file}" && ! -f "${pending_file}") ]]; then
    log_error "Administrator password display marker must be a regular file."
    return 1
  fi
  if [[ "${AI_SERVICE_ENABLED:-false}" == "true" && -f "${pending_file}" ]]; then
    if [[ -z "${AI_ADMIN_PASSWORD:-}" ]]; then
      log_error "Administrator password is unavailable for the one-time completion display."
      return 1
    fi
    if ! rm -f -- "${pending_file}"; then
      log_error "Unable to consume the one-time administrator password display marker."
      return 1
    fi
    password_line="初始密码：${AI_ADMIN_PASSWORD}"
  fi

  if [[ "${AI_SERVICE_ENABLED:-false}" != "true" ]]; then
    printf '%s\n' \
      '================================' \
      '' \
      'AI Customer Platform 安装完成' \
      '' \
      '网站地址：' \
      "https://${DOMAIN}" \
      '' \
      'AI Service 未配置，可后续启用。' \
      '' \
      '维护命令：' \
      '' \
      '状态：' \
      './manage.sh status' \
      '' \
      '日志：' \
      './manage.sh logs' \
      '' \
      '重启：' \
      './manage.sh restart' \
      '' \
      '================================'
    return 0
  fi

  printf '%s\n' \
    '================================' \
    '' \
    'AI Customer Platform 安装完成' \
    '' \
    '网站地址：' \
    "https://${DOMAIN}" \
    '' \
    'AI 管理后台：' \
    "https://${DOMAIN}/ai/admin" \
    '' \
    '管理员账号：' \
    "${AI_ADMIN_USERNAME}" \
    '' \
    "${password_line}" \
    '' \
    '请首次登录后修改密码。' \
    '' \
    '维护命令：' \
    '' \
    '状态：' \
    './manage.sh status' \
    '' \
    '日志：' \
    './manage.sh logs' \
    '' \
    '重启：' \
    './manage.sh restart' \
    '' \
    '================================'
}
