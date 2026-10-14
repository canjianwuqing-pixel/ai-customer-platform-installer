#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_LOADED="false"
CONFIG_FILE="${CONFIG_FILE:-}"
declare -A CONFIG_CONTEXT_KEYS=()
declare -A CONFIG_USER_KEYS=()

config_reset_context() {
  local layer line key
  local -a inherited_keys=()
  read -r -a inherited_keys <<<"${CONFIG_PARENT_KEYS:-}"
  for key in "${inherited_keys[@]}"; do
    [[ "${key}" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    CONFIG_CONTEXT_KEYS["${key}"]=true
  done
  # Include schema keys even on the first load: inherited process variables
  # must not become undeclared defaults during a restore.
  for layer in "$(config_default_file)" "$(config_images_file)" "${INSTALLER_ROOT}/.env.example"; do
    while IFS= read -r line || [[ -n "${line}" ]]; do
      if [[ "${line}" =~ ^([A-Z][A-Z0-9_]*)= ]]; then CONFIG_CONTEXT_KEYS["${BASH_REMATCH[1]}"]=true; fi
    done <"${layer}"
  done
  # Include optional interpolation keys that are not present in default.env.
  for layer in "${INSTALLER_ROOT}"/stacks/{chatwoot,ai-service,admin}/compose*.yaml; do
    [[ -f "${layer}" ]] || continue
    while IFS= read -r line || [[ -n "${line}" ]]; do
      while [[ "${line}" =~ \$\{([A-Z_][A-Z0-9_]*) ]]; do
        key="${BASH_REMATCH[1]}"
        line="${line#*"${BASH_REMATCH[0]}"}"
        CONFIG_CONTEXT_KEYS["${key}"]=true
      done
    done <"${layer}"
  done
  for key in "${!CONFIG_CONTEXT_KEYS[@]}"; do unset "${key}"; done
  # These are derived caches, not a source of restored configuration. Explicit
  # overrides are loaded again from the replacement .env below.
  unset CERTBOT_CERTIFICATE_DIR CERTBOT_FULLCHAIN_PATH CERTBOT_PRIVATE_KEY_PATH
  unset CERTBOT_RENEWAL_HOOKS_DIR CERTBOT_RENEW_HOOK_PATH
  unset NGINX_TEMPLATE_FILE DOMAIN_HTTPS_TEMPLATE_FILE DOMAIN_RENEW_HOOK_TEMPLATE_FILE
  CONFIG_CONTEXT_KEYS=()
  CONFIG_USER_KEYS=()
  AI_SERVICE_ENABLED=false
}

config_default_file() {
  printf '%s\n' "${INSTALLER_DEFAULT_CONFIG_FILE:-${INSTALLER_ROOT}/config/default.env}"
}

config_images_file() {
  printf '%s\n' "${INSTALLER_IMAGES_CONFIG_FILE:-${INSTALLER_ROOT}/config/images.env}"
}

config_load_installer_configuration() {
  local user_config_file="$1"
  local default_config_file
  local images_config_file
  local config_layer

  default_config_file="$(config_default_file)"
  images_config_file="$(config_images_file)"

  for config_layer in "${default_config_file}" "${images_config_file}"; do
    if [[ ! -f "${config_layer}" || ! -r "${config_layer}" ]]; then
      log_error "Installer configuration layer is not readable: ${config_layer}."
      return 1
    fi
  done

  if [[ ! -f "${user_config_file}" || ! -r "${user_config_file}" ]]; then
    log_error "User configuration file is not readable: ${user_config_file}."
    return 1
  fi

  config_reset_context
  CONFIG_FILE="$(readlink -m -- "${user_config_file}")"
  CONFIG_LOADED="false"

  # Lowest precedence first. A user's .env is loaded last and therefore wins.
  config_load_file "${default_config_file}"
  config_load_file "${images_config_file}"
  config_load_file "${CONFIG_FILE}" user
  [[ -z "${AI_SERVICE_IMAGE:-}" ]] || AI_SERVICE_ENABLED=true
  CONFIG_PARENT_KEYS="${!CONFIG_CONTEXT_KEYS[*]}"
  export CONFIG_PARENT_KEYS

  CONFIG_LOADED="true"
  log_info "Loaded installer defaults, pinned image versions, and user overrides."
}

config_materialize_missing_defaults() {
  local target_file="$1"
  shift
  local source_file
  local line
  local key
  local -a missing_keys=()
  local -A target_keys=()
  local -A queued_keys=()

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    if [[ "${line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      target_keys["${BASH_REMATCH[1]}"]=true
    fi
  done <"${target_file}"

  # Callers pass higher-precedence layers first so duplicate default keys keep
  # the value already present in the environment after layered loading.
  for source_file in "$@"; do
    while IFS= read -r line || [[ -n "${line}" ]]; do
      line="${line%$'\r'}"
      if [[ "${line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
        key="${BASH_REMATCH[1]}"
        if [[ -z "${target_keys[${key}]+present}" && -z "${queued_keys[${key}]+queued}" ]]; then
          missing_keys+=("${key}")
          queued_keys["${key}"]=true
        fi
      fi
    done <"${source_file}"
  done

  if ((${#missing_keys[@]} > 0)); then
    config_save_keys "${target_file}" "${missing_keys[@]}"
    log_ok "Stored installer-managed defaults in the protected configuration file."
  fi
}

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

    # A deliberately non-evaluating dotenv subset. Never source a user file.
    if [[ "${value:0:1}" == "'" ]]; then
      value="${value:1}"
      [[ "${value}" == *"'"* ]] || { log_error "Unclosed configuration quote at line ${line_number}."; return 1; }
      local tail="${value#*\'}"
      value="${value%%\'*}"
      [[ "${tail}" =~ ^[[:space:]]*(#.*)?$ ]] || { log_error "Unsupported configuration quoting at line ${line_number}."; return 1; }
    elif [[ "${value:0:1}" == '"' ]]; then
      value="${value:1}"
      [[ "${value}" == *'"'* ]] || { log_error "Unclosed configuration quote at line ${line_number}."; return 1; }
      local tail="${value#*\"}"
      value="${value%%\"*}"
      [[ "${tail}" =~ ^[[:space:]]*(#.*)?$ ]] || { log_error "Unsupported configuration quoting at line ${line_number}."; return 1; }
      [[ "${value}" != *\\* ]] || { log_error "Use literal single quotes for backslashes in configuration (line ${line_number})."; return 1; }
    else
      # Preserve the old installer's literal credential values, including
      # dollars and inline '#'. Never reinterpret an existing database secret.
      case "${key}" in *_PASSWORD|*_SECRET|*_TOKEN|*_KEY|SECRET_KEY_BASE) ;;
        *) value="${value%%[[:space:]]#*}" ;;
      esac
      value="${value%"${value##*[![:space:]]}"}"
    fi

    case "${key}" in CONFIG_*|INSTALLER_*|BASH*|SHELLOPTS|PATH|ENV|LD_*|PYTHONPATH)
      [[ "${key}" == INSTALLER_CONFIG_VERSION ]] || { log_error "Reserved configuration key at line ${line_number}."; return 1; } ;;
    esac

    printf -v "${key}" '%s' "${value}"
    export "${key}"
    CONFIG_CONTEXT_KEYS["${key}"]=true
    if [[ "${2:-}" == user ]]; then CONFIG_USER_KEYS["${key}"]=true; fi
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
        config_write_scalar "${key}" "${requested_values[${key}]}" >>"${temporary_file}" || { rm -f -- "${temporary_file}"; return 1; }
        written_keys["${key}"]=true
        continue
      fi
    fi
    printf '%s\n' "${line}" >>"${temporary_file}"
  done <"${config_file}"

  for key in "${requested_keys[@]}"; do
    if [[ -z "${written_keys[${key}]+written}" ]]; then
      config_write_scalar "${key}" "${requested_values[${key}]}" >>"${temporary_file}" || { rm -f -- "${temporary_file}"; return 1; }
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

config_write_scalar() {
  local key="$1" value="$2"
  if [[ "${value}" =~ ^[A-Za-z0-9_./:@,+=-]*$ ]]; then
    printf '%s=%s\n' "${key}" "${value}"
  elif [[ "${value}" != *"'"* && "${value}" != *$'\n'* && "${value}" != *$'\r'* ]]; then
    printf "%s='%s'\n" "${key}" "${value}"
  elif [[ "${value}" != *'"'* && "${value}" != *'$'* && "${value}" != *\\* && "${value}" != *$'\n'* && "${value}" != *$'\r'* ]]; then
    printf '%s="%s"\n' "${key}" "${value}"
  else
    log_error "Configuration field ${key} cannot be safely serialized; existing configuration was preserved."
    return 1
  fi
}

config_ai_enabled() { [[ -n "${AI_SERVICE_IMAGE:-}" ]]; }

# One Compose context for every CLI, independent of cwd and sudo's env_reset.
# A private, short-lived snapshot of the loaded layers makes literal values
# identical across sudo/cwd changes. No secrets in argv or wholesale injection
# into container environments. Never write the user's .env during management.
config_compose() (
  set +x
  umask 077
  local project="$1"; shift
  local snapshot key
  snapshot="$(mktemp "${TMPDIR:-/tmp}/ai-platform-compose.XXXXXX")" || return 1
  trap 'rm -f -- "${snapshot}"' EXIT
  local value
  for key in "${!CONFIG_CONTEXT_KEYS[@]}"; do
    value="${!key-}"
    [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] || return 1
    # Compose decodes quoted escapes then interpolates. Encode both steps so
    # arbitrary literal quotes, slashes and dollars retain their exact value.
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\$/\$\$}"
    printf '%s="%s"\n' "${key}" "${value}" >>"${snapshot}"
  done
  local -a arguments=(compose --project-name "${project}" --env-file "${snapshot}" "$@")
  docker_execute "${arguments[@]}"
)
