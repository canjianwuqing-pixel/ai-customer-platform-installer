#!/usr/bin/env bash
set -Eeuo pipefail

SETUP_INPUT_VALUE=""

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

setup_validate_api_key() {
  local value="$1"
  validation_has_minimum_length "${value}" 20 &&
    [[ "${value}" != *[[:space:]]* ]]
}

setup_validate_model_name() {
  local value="$1"
  [[ "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*$ ]]
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

  if [[ -z "${AI_SERVICE_IMAGE:-}" ]]; then
    setup_prompt_value \
      "请输入 AI Service 固定版本镜像：" \
      "registry.example.com/ai-service:1.0.0" \
      validation_is_pinned_container_image
    AI_SERVICE_IMAGE="${SETUP_INPUT_VALUE}"
    export AI_SERVICE_IMAGE
    changed_keys+=(AI_SERVICE_IMAGE)
  fi

  if [[ -z "${AI_ADMIN_IMAGE:-}" ]]; then
    setup_prompt_value \
      "请输入 AI 管理后台固定版本镜像：" \
      "registry.example.com/ai-admin:1.0.0" \
      validation_is_pinned_container_image
    AI_ADMIN_IMAGE="${SETUP_INPUT_VALUE}"
    export AI_ADMIN_IMAGE
    changed_keys+=(AI_ADMIN_IMAGE)
  fi

  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    setup_prompt_value "请输入 OpenAI API Key：" "" setup_validate_api_key true
    OPENAI_API_KEY="${SETUP_INPUT_VALUE}"
    export OPENAI_API_KEY
    changed_keys+=(OPENAI_API_KEY)
  fi

  if [[ -z "${AI_MODEL:-}" ]]; then
    setup_prompt_value "请输入 AI 模型名称：" "your-model-name" setup_validate_model_name
    AI_MODEL="${SETUP_INPUT_VALUE}"
    export AI_MODEL
    changed_keys+=(AI_MODEL)
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

  config_load_optional "${CONFIG_FILE}"
  if [[ "${CONFIG_LOADED}" != "true" ]]; then
    log_error "The installation configuration could not be loaded."
    return 1
  fi

  setup_collect_configuration
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

  pending_file="$(setup_admin_password_pending_file)"
  if [[ -L "${pending_file}" || (-e "${pending_file}" && ! -f "${pending_file}") ]]; then
    log_error "Administrator password display marker must be a regular file."
    return 1
  fi
  if [[ -f "${pending_file}" ]]; then
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
    '================================'
}
