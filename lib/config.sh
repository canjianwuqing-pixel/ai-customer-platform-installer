#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_LOADED="false"
CONFIG_FILE="${CONFIG_FILE:-}"

config_load_optional() {
  local config_file="$1"

  CONFIG_FILE="${config_file}"

  if [[ ! -f "${CONFIG_FILE}" ]]; then
    log_warn "Configuration file not found: ${CONFIG_FILE}."
    return 0
  fi

  config_load_file "${CONFIG_FILE}"
  CONFIG_LOADED="true"
  log_info "Loaded configuration from ${CONFIG_FILE}."
}

config_load_file() {
  local config_file="$1"
  local line
  local key
  local value

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"

    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue

    if [[ ! "${line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      die "Invalid configuration line in ${config_file}: ${line}"
    fi

    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value}" == \'*\' && "${value}" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi

    printf -v "${key}" '%s' "${value}"
    export "${key}"
  done <"${config_file}"
}
