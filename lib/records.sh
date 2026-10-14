#!/usr/bin/env bash
set -Eeuo pipefail

RECORDS_DECODED_VALUE=""

records_decode_scalar() {
  local value="$1"
  local first_character=""
  local last_character=""

  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  RECORDS_DECODED_VALUE=""

  if [[ -z "${value}" ]]; then
    return 0
  fi

  first_character="${value:0:1}"
  last_character="${value: -1}"
  if [[ "${first_character}" == '"' || "${first_character}" == "'" ]]; then
    if ((${#value} < 2)) || [[ "${last_character}" != "${first_character}" ]]; then
      return 1
    fi
    value="${value:1:${#value}-2}"
  elif [[ "${last_character}" == '"' || "${last_character}" == "'" ]]; then
    return 1
  fi

  case "${value}" in
    '|' | '>' | '|-' | '>-' | '|+' | '>+') return 1 ;;
  esac

  [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] || return 1
  RECORDS_DECODED_VALUE="${value}"
}

records_check_user_file_permissions() {
  local config_file="$1"
  local description="$2"
  local mode
  local permission_bits

  if [[ -L "${config_file}" ]]; then
    log_error "${description} must not be a symbolic link: ${config_file}."
    return 1
  fi

  if ! mode="$(stat -c '%a' "${config_file}" 2>/dev/null)"; then
    log_error "Unable to verify ${description} permissions: ${config_file}."
    return 1
  fi
  mode="${mode: -3}"
  if [[ ! "${mode}" =~ ^[0-7]{3}$ ]]; then
    log_error "Unable to interpret ${description} permissions: ${config_file}."
    return 1
  fi

  permission_bits=$((8#${mode}))
  if ((permission_bits & 077)); then
    log_error "${description} permissions must be 0600 or stricter: ${config_file}."
    return 1
  fi
}
