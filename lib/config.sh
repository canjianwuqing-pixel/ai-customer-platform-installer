#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_LOADED="false"
CONFIG_FILE="${CONFIG_FILE:-}"

config_load_optional() {
  local config_file="$1"

  CONFIG_FILE="${config_file}"
  CONFIG_LOADED="false"

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
  local line_number=0
  local key
  local value

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_number=$((line_number + 1))
    line="${line%$'\r'}"

    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue

    if [[ ! "${line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      die "Invalid configuration syntax at line ${line_number} in ${config_file}."
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

config_create_from_template() {
  local config_file="$1"
  local template_file="$2"
  local temporary_file

  if [[ -e "${config_file}" || -L "${config_file}" ]]; then
    log_error "Refusing to replace an existing configuration path: ${config_file}."
    return 1
  fi

  if [[ ! -f "${template_file}" || ! -r "${template_file}" ]]; then
    log_error "Configuration template is not readable: ${template_file}."
    return 1
  fi

  if [[ ! -d "$(dirname -- "${config_file}")" || ! -w "$(dirname -- "${config_file}")" ]]; then
    log_error "Configuration directory is not writable: $(dirname -- "${config_file}")."
    return 1
  fi

  temporary_file="$(mktemp "${config_file}.setup.XXXXXX")" || {
    log_error "Unable to create a secure temporary configuration file."
    return 1
  }

  if ! chmod 600 "${temporary_file}" || ! cp -- "${template_file}" "${temporary_file}"; then
    rm -f -- "${temporary_file}"
    log_error "Unable to prepare the installation configuration."
    return 1
  fi

  if ! mv -- "${temporary_file}" "${config_file}" || ! chmod 600 "${config_file}"; then
    rm -f -- "${temporary_file}"
    log_error "Unable to create the protected .env configuration."
    return 1
  fi

  log_ok "Created protected installation configuration: ${config_file}."
}

config_save_keys() {
  local config_file="$1"
  shift
  local restore_xtrace="false"
  local temporary_file
  local line
  local normalized_line
  local key
  local value
  local -a requested_keys=("$@")
  local -A requested_values=()
  local -A written_keys=()

  if [[ "$-" == *x* ]]; then
    restore_xtrace="true"
    set +x
  fi

  if [[ -L "${config_file}" || ! -f "${config_file}" || ! -r "${config_file}" || ! -w "${config_file}" ]]; then
    log_error "Configuration file must be a readable and writable regular file: ${config_file}."
    return 1
  fi

  for key in "${requested_keys[@]}"; do
    if [[ ! "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      log_error "Invalid configuration key requested for save."
      return 1
    fi
    value="${!key-}"
    if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
      log_error "Configuration values cannot contain line breaks."
      return 1
    fi
    requested_values["${key}"]="${value}"
  done

  temporary_file="$(mktemp "${config_file}.save.XXXXXX")" || {
    log_error "Unable to create a secure temporary configuration file."
    return 1
  }
  if ! chmod 600 "${temporary_file}"; then
    rm -f -- "${temporary_file}"
    log_error "Unable to restrict temporary configuration file permissions."
    return 1
  fi

  while IFS= read -r line || [[ -n "${line}" ]]; do
    normalized_line="${line%$'\r'}"
    if [[ "${normalized_line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      key="${BASH_REMATCH[1]}"
      if [[ -n "${requested_values[${key}]+requested}" ]]; then
        if [[ -n "${written_keys[${key}]+written}" ]]; then
          rm -f -- "${temporary_file}"
          log_error "Configuration contains duplicate field: ${key}."
          return 1
        fi
        printf '%s=%s\n' "${key}" "${requested_values[${key}]}" >>"${temporary_file}"
        written_keys["${key}"]=true
        continue
      fi
    fi
    printf '%s\n' "${line}" >>"${temporary_file}"
  done <"${config_file}"

  for key in "${requested_keys[@]}"; do
    if [[ -z "${written_keys[${key}]+written}" ]]; then
      printf '%s=%s\n' "${key}" "${requested_values[${key}]}" >>"${temporary_file}"
    fi
  done

  if ! mv -f -- "${temporary_file}" "${config_file}" || ! chmod 600 "${config_file}"; then
    rm -f -- "${temporary_file}"
    log_error "Unable to save the installation configuration."
    return 1
  fi

  if [[ "${restore_xtrace}" == "true" ]]; then
    set -x
  fi
}
