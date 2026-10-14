#!/usr/bin/env bash
set -Eeuo pipefail

readonly -a INSTALLER_SECRET_KEYS=(
  DATABASE_PASSWORD
  REDIS_PASSWORD
  SECRET_KEY_BASE
  AI_SERVICE_INTERNAL_TOKEN
  SERVICE_MANAGEMENT_TOKEN
)

declare -A INSTALLER_SECRET_KEY_COUNTS=()
declare -A INSTALLER_GENERATED_SECRETS=()
SECRETS_GENERATED_VALUE=""

# One path for nonce creation, derivation and completion. Never create an
# empty display marker: a once file must always contain a 32-byte nonce.
secrets_admin_once_file() {
  local once_file="${INSTALLER_ADMIN_ONCE_FILE:-${INSTALLER_ROOT}/.installer-admin-password-once}"
  if [[ "$(basename -- "${once_file}")" != .installer-admin-password-once ]]; then
    log_error "The administrator nonce file must be named .installer-admin-password-once."
    return 1
  fi
  printf '%s\n' "${once_file}"
}

secrets_ensure_admin_nonce() {
  local once_file
  once_file="$(secrets_admin_once_file)" || return 1
  if [[ -L "${once_file}" || (-e "${once_file}" && ! -f "${once_file}") ]]; then
    log_error "Administrator nonce must be a regular file, not a symbolic link."
    return 1
  fi
  if [[ ! -f "${once_file}" ]]; then
    secrets_generate_hex 32 || return 1
    # noclobber prevents replacing a nonce from an interrupted installation.
    (umask 077; set -o noclobber; printf '%s' "${SECRETS_GENERATED_VALUE}" >"${once_file}") || {
      log_error "Unable to save the administrator nonce."
      return 1
    }
  fi
  chmod 600 "${once_file}" || return 1
  local nonce
  nonce="$(<"${once_file}")"
  if [[ ! "${nonce}" =~ ^[a-f0-9]{64}$ ]]; then
    log_error "Administrator nonce is invalid; existing credentials were left unchanged."
    return 1
  fi
}

secrets_is_managed_key() {
  local candidate="$1"
  local key

  for key in "${INSTALLER_SECRET_KEYS[@]}"; do
    if [[ "${candidate}" == "${key}" ]]; then
      return 0
    fi
  done

  return 1
}

secrets_required_bytes() {
  local key="$1"

  case "${key}" in
    SECRET_KEY_BASE) printf '%s\n' '64' ;;
    DATABASE_PASSWORD | REDIS_PASSWORD | AI_SERVICE_INTERNAL_TOKEN | SERVICE_MANAGEMENT_TOKEN)
      printf '%s\n' '32'
      ;;
    *) return 1 ;;
  esac
}

secrets_generate_hex() {
  local byte_count="$1"
  local expected_length=$((byte_count * 2))
  local generated_value=""
  local validation_pattern="^[0-9a-f]{${expected_length}}$"

  SECRETS_GENERATED_VALUE=""

  if command_exists openssl; then
    generated_value="$(openssl rand -hex "${byte_count}" 2>/dev/null)" || generated_value=""
  fi

  if [[ ! "${generated_value}" =~ ${validation_pattern} ]]; then
    generated_value=""
    if [[ -r /dev/urandom ]] && command_exists od && command_exists tr; then
      generated_value="$(od -An -N "${byte_count}" -tx1 /dev/urandom 2>/dev/null | tr -d '[:space:]')" ||
        generated_value=""
    fi
  fi

  if [[ ! "${generated_value}" =~ ${validation_pattern} ]]; then
    log_error "Unable to generate a cryptographically secure installation secret."
    return 1
  fi

  SECRETS_GENERATED_VALUE="${generated_value}"
}

secrets_validate_config_file() {
  local line
  local normalized_line
  local key

  if [[ "${CONFIG_LOADED:-false}" != "true" ]]; then
    log_error "A populated .env configuration file is required before secrets can be generated."
    return 1
  fi

  if [[ -L "${CONFIG_FILE}" ]]; then
    log_error "Refusing to update a symbolic-link configuration file: ${CONFIG_FILE}."
    return 1
  fi

  if [[ ! -f "${CONFIG_FILE}" || ! -r "${CONFIG_FILE}" || ! -w "${CONFIG_FILE}" ]]; then
    log_error "Configuration file must be a readable and writable regular file: ${CONFIG_FILE}."
    return 1
  fi

  if [[ ! -w "$(dirname -- "${CONFIG_FILE}")" ]]; then
    log_error "Configuration directory is not writable: $(dirname -- "${CONFIG_FILE}")."
    return 1
  fi

  INSTALLER_SECRET_KEY_COUNTS=()
  for key in "${INSTALLER_SECRET_KEYS[@]}"; do
    INSTALLER_SECRET_KEY_COUNTS["${key}"]=0
  done

  while IFS= read -r line || [[ -n "${line}" ]]; do
    normalized_line="${line%$'\r'}"
    if [[ "${normalized_line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      key="${BASH_REMATCH[1]}"
      if secrets_is_managed_key "${key}"; then
        INSTALLER_SECRET_KEY_COUNTS["${key}"]=$((INSTALLER_SECRET_KEY_COUNTS["${key}"] + 1))
        if ((INSTALLER_SECRET_KEY_COUNTS["${key}"] > 1)); then
          log_error "Configuration contains duplicate secret field: ${key}."
          return 1
        fi
      fi
    fi
  done <"${CONFIG_FILE}"
}

secrets_write_generated_values() {
  local temporary_file
  local line
  local normalized_line
  local key
  local -A written_keys=()

  temporary_file="$(mktemp "${CONFIG_FILE}.secrets.XXXXXX")" || {
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
      if [[ -n "${INSTALLER_GENERATED_SECRETS[${key}]+configured}" ]]; then
        printf '%s=%s\n' "${key}" "${INSTALLER_GENERATED_SECRETS[${key}]}" >>"${temporary_file}"
        written_keys["${key}"]=true
        continue
      fi
    fi

    printf '%s\n' "${line}" >>"${temporary_file}"
  done <"${CONFIG_FILE}"

  for key in "${INSTALLER_SECRET_KEYS[@]}"; do
    if [[ -n "${INSTALLER_GENERATED_SECRETS[${key}]+configured}" &&
      -z "${written_keys[${key}]+written}" ]]; then
      printf '%s=%s\n' "${key}" "${INSTALLER_GENERATED_SECRETS[${key}]}" >>"${temporary_file}"
    fi
  done

  if ! mv -f -- "${temporary_file}" "${CONFIG_FILE}"; then
    rm -f -- "${temporary_file}"
    log_error "Unable to save generated installation secrets."
    return 1
  fi

  if ! chmod 600 "${CONFIG_FILE}"; then
    log_error "Installation secrets were saved, but .env permissions could not be restricted to 0600."
    return 1
  fi
}

secrets_ensure() {
  local restore_xtrace="false"
  local key
  local byte_count
  local generated_count=0

  if [[ "$-" == *x* ]]; then
    restore_xtrace="true"
    set +x
  fi

  secrets_validate_config_file || return 1
  INSTALLER_GENERATED_SECRETS=()

  for key in "${INSTALLER_SECRET_KEYS[@]}"; do
    if ((INSTALLER_SECRET_KEY_COUNTS["${key}"] == 1)) && [[ -n "${!key:-}" ]]; then
      continue
    fi

    byte_count="$(secrets_required_bytes "${key}")" || {
      log_error "No generation policy is defined for secret field: ${key}."
      return 1
    }
    secrets_generate_hex "${byte_count}" || return 1
    INSTALLER_GENERATED_SECRETS["${key}"]="${SECRETS_GENERATED_VALUE}"
    printf -v "${key}" '%s' "${SECRETS_GENERATED_VALUE}"
    export "${key}"
    generated_count=$((generated_count + 1))
  done

  if ((generated_count == 0)); then
    log_info "Installation secrets are already configured; existing values were preserved."
  else
    secrets_write_generated_values || return 1
    config_load_file "${CONFIG_FILE}"
    log_ok "Generated and securely saved ${generated_count} missing installation secret(s)."
  fi

  if ! chmod 600 "${CONFIG_FILE}"; then
    log_error "Unable to restrict .env permissions to 0600."
    return 1
  fi

  if [[ "${restore_xtrace}" == "true" ]]; then
    set -x
  fi
}
