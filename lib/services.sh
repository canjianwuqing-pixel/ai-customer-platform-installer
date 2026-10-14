#!/usr/bin/env bash
set -Eeuo pipefail

SERVICES_CONFIG_LOADED="false"
SERVICES_DEFAULT_CONFIG_FILE=""
SERVICES_USER_CONFIG_FILE=""
SERVICES_DECODED_VALUE=""

declare -a SERVICE_INSTANCE_IDS=()
declare -A SERVICE_INSTANCE_VALUES=()
declare -A SERVICE_INSTANCE_KNOWN=()
declare -A SERVICE_INSTANCE_CONFIG=()

services_default_config_file() {
  printf '%s\n' \
    "${SERVICE_INSTANCES_DEFAULT_FILE:-${INSTALLER_ROOT}/config/services/services.yaml}"
}

services_user_config_file() {
  printf '%s\n' \
    "${SERVICE_INSTANCES_FILE:-${INSTALLER_ROOT}/config/services/local.yaml}"
}

services_reset() {
  SERVICES_CONFIG_LOADED="false"
  SERVICES_DEFAULT_CONFIG_FILE=""
  SERVICES_USER_CONFIG_FILE=""
  SERVICE_INSTANCE_IDS=()
  SERVICE_INSTANCE_VALUES=()
  SERVICE_INSTANCE_KNOWN=()
  SERVICE_INSTANCE_CONFIG=()
}

services_decode_scalar() {
  local value="$1"
  local first_character=""
  local last_character=""

  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  SERVICES_DECODED_VALUE=""

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
  SERVICES_DECODED_VALUE="${value}"
}

services_path_is_sensitive() {
  local path="$1"
  local component
  local -a components=()

  IFS='.' read -r -a components <<<"${path,,}"
  for component in "${components[@]}"; do
    if [[ "${component}" =~ (^|[_-])(api[_-]?key|password|secret|token|private[_-]?key)($|[_-]) ]]; then
      return 0
    fi
  done
  return 1
}

services_commit_parsed_entry() {
  local config_file="$1"
  local -n entry_values_ref="$2"
  local -n entry_paths_ref="$3"
  local -n file_ids_ref="$4"
  local service_id="${entry_values_ref[service.id]-}"
  local path

  if [[ -z "${service_id}" ]]; then
    log_error "Service instance entry is missing service.id in ${config_file}."
    return 1
  fi

  if [[ ! "${service_id}" =~ ^[a-z][a-z0-9_-]{0,63}$ ]]; then
    log_error "Service instance id is invalid in ${config_file}; use lowercase letters, digits, underscores, or hyphens."
    return 1
  fi

  if [[ -n "${file_ids_ref[${service_id}]+defined}" ]]; then
    log_error "Duplicate service instance id '${service_id}' in ${config_file}."
    return 1
  fi
  file_ids_ref["${service_id}"]=true

  if [[ -z "${SERVICE_INSTANCE_KNOWN[${service_id}]+known}" ]]; then
    SERVICE_INSTANCE_IDS+=("${service_id}")
    SERVICE_INSTANCE_KNOWN["${service_id}"]=true
  fi

  for path in "${entry_paths_ref[@]}"; do
    SERVICE_INSTANCE_VALUES["${service_id}|${path}"]="${entry_values_ref[${path}]}"
  done
}

services_parse_file() {
  local config_file="$1"
  local line
  local content
  local leading_spaces
  local key
  local raw_value
  local path
  local parent_path
  local service_version=""
  local line_number=0
  local indent=0
  local parent_indent=0
  local in_services="false"
  local entry_active="false"
  local depth
  local -A path_at_indent=()
  local -A entry_values=()
  local -A entry_seen_paths=()
  local -A file_ids=()
  local -a entry_paths=()

  if ! file_is_readable "${config_file}"; then
    log_error "Service instance configuration is not readable: ${config_file}."
    return 1
  fi

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_number=$((line_number + 1))
    line="${line%$'\r'}"

    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue

    if [[ "${line}" == *$'\t'* ]]; then
      log_error "Tabs are not allowed at line ${line_number} in ${config_file}."
      return 1
    fi

    leading_spaces="${line%%[! ]*}"
    indent=${#leading_spaces}
    content="${line:${indent}}"

    if ((indent % 2 != 0)); then
      log_error "Service configuration indentation must use two-space steps at line ${line_number} in ${config_file}."
      return 1
    fi

    if ((indent == 0)); then
      if [[ "${content}" =~ ^version:[[:space:]]*([0-9]+)[[:space:]]*$ ]]; then
        service_version="${BASH_REMATCH[1]}"
        continue
      fi
      if [[ "${content}" == "services:" ]]; then
        in_services="true"
        continue
      fi
      if [[ "${content}" =~ ^services:[[:space:]]*\[\][[:space:]]*$ ]]; then
        in_services="true"
        continue
      fi

      log_error "Unsupported top-level service configuration at line ${line_number} in ${config_file}."
      return 1
    fi

    if [[ "${in_services}" != "true" ]]; then
      log_error "Service entries must appear below services at line ${line_number} in ${config_file}."
      return 1
    fi

    if ((indent == 2)) && [[ "${content}" =~ ^-[[:space:]]+service:[[:space:]]*$ ]]; then
      if [[ "${entry_active}" == "true" ]]; then
        services_commit_parsed_entry \
          "${config_file}" entry_values entry_paths file_ids || return 1
      fi

      entry_active="true"
      entry_values=()
      entry_seen_paths=()
      entry_paths=()
      path_at_indent=()
      path_at_indent[4]="service"
      continue
    fi

    if [[ "${entry_active}" != "true" ]]; then
      log_error "Expected a '- service:' entry at line ${line_number} in ${config_file}."
      return 1
    fi

    if ((indent < 4)) || [[ ! "${content}" =~ ^([a-z][a-z0-9_-]*):[[:space:]]*(.*)$ ]]; then
      log_error "Unsupported service configuration syntax at line ${line_number} in ${config_file}."
      return 1
    fi

    key="${BASH_REMATCH[1]}"
    raw_value="${BASH_REMATCH[2]}"

    for depth in "${!path_at_indent[@]}"; do
      if ((depth >= indent)); then
        unset 'path_at_indent[$depth]'
      fi
    done

    if ((indent == 4)); then
      path="${key}"
    else
      parent_indent=$((indent - 2))
      parent_path="${path_at_indent[${parent_indent}]-}"
      if [[ -z "${parent_path}" ]]; then
        log_error "Missing parent mapping at line ${line_number} in ${config_file}."
        return 1
      fi
      if [[ -n "${entry_values[${parent_path}]+defined}" &&
        -n "${entry_values[${parent_path}]}" ]]; then
        log_error "Scalar field cannot contain nested configuration at line ${line_number} in ${config_file}."
        return 1
      fi
      path="${parent_path}.${key}"
    fi

    if [[ -n "${entry_seen_paths[${path}]+defined}" ]]; then
      log_error "Duplicate service configuration field '${path}' in ${config_file}."
      return 1
    fi

    if services_path_is_sensitive "${path}"; then
      log_error "Sensitive field names are forbidden in service configuration: ${path}. Use a credential reference instead."
      return 1
    fi

    if ! services_decode_scalar "${raw_value}"; then
      log_error "Unsupported scalar value at line ${line_number} in ${config_file}."
      return 1
    fi

    entry_values["${path}"]="${SERVICES_DECODED_VALUE}"
    entry_seen_paths["${path}"]=true
    entry_paths+=("${path}")
    path_at_indent["${indent}"]="${path}"
  done <"${config_file}"

  if [[ "${service_version}" != "1" ]]; then
    log_error "Service configuration must declare supported version 1: ${config_file}."
    return 1
  fi

  if [[ "${in_services}" != "true" ]]; then
    log_error "Service configuration must define a services collection: ${config_file}."
    return 1
  fi

  if [[ "${entry_active}" == "true" ]]; then
    services_commit_parsed_entry \
      "${config_file}" entry_values entry_paths file_ids || return 1
  fi
}

services_check_user_file_permissions() {
  local config_file="$1"
  local mode
  local permission_bits

  if [[ -L "${config_file}" ]]; then
    log_error "User service configuration must not be a symbolic link: ${config_file}."
    return 1
  fi

  if ! mode="$(stat -c '%a' "${config_file}" 2>/dev/null)"; then
    log_error "Unable to verify user service configuration permissions: ${config_file}."
    return 1
  fi
  mode="${mode: -3}"
  if [[ ! "${mode}" =~ ^[0-7]{3}$ ]]; then
    log_error "Unable to interpret user service configuration permissions: ${config_file}."
    return 1
  fi

  permission_bits=$((8#${mode}))
  if ((permission_bits & 077)); then
    log_error "User service configuration permissions must be 0600 or stricter: ${config_file}."
    return 1
  fi
}

services_value_exists() {
  local service_id="$1"
  local path="$2"
  [[ -n "${SERVICE_INSTANCE_VALUES[${service_id}|${path}]+defined}" ]]
}

services_validate_instance() {
  local service_id="$1"
  local required_path
  local path
  local composite_key
  local value
  local root
  local -a required_paths=(
    service.id
    service.name
    service.enabled
    ai.model
    ai.prompt
    knowledge.source
    channels.enabled
    integration.provider
    integration.config
  )

  for required_path in "${required_paths[@]}"; do
    if ! services_value_exists "${service_id}" "${required_path}"; then
      log_error "Service instance '${service_id}' is missing required field: ${required_path}."
      return 1
    fi
  done

  if [[ "${SERVICE_INSTANCE_VALUES[${service_id}|service.id]}" != "${service_id}" ]]; then
    log_error "Service instance identifier mismatch for '${service_id}'."
    return 1
  fi

  value="${SERVICE_INSTANCE_VALUES[${service_id}|service.name]}"
  if [[ -z "${value}" || ${#value} -gt 128 ]]; then
    log_error "Service instance '${service_id}' must have a name of 1-128 characters."
    return 1
  fi

  value="${SERVICE_INSTANCE_VALUES[${service_id}|service.enabled]}"
  if ! validation_is_boolean "${value}"; then
    log_error "Service instance '${service_id}' enabled state must be true or false."
    return 1
  fi

  value="${SERVICE_INSTANCE_VALUES[${service_id}|channels.enabled]}"
  if [[ "${value}" != "true" && "${value}" != "false" &&
    ! "${value}" =~ ^\[[^][]*\]$ ]]; then
    log_error "Service instance '${service_id}' channels.enabled must be a boolean or inline list."
    return 1
  fi

  for composite_key in "${!SERVICE_INSTANCE_VALUES[@]}"; do
    [[ "${composite_key}" == "${service_id}|"* ]] || continue
    path="${composite_key#*|}"
    root="${path%%.*}"
    case "${root}" in
      service | ai | knowledge | channels | integration) ;;
      *)
        log_error "Service instance '${service_id}' contains unsupported section: ${root}."
        return 1
        ;;
    esac
  done
}

services_validate() {
  local service_id

  for service_id in "${SERVICE_INSTANCE_IDS[@]}"; do
    services_validate_instance "${service_id}" || return 1
  done
}

services_load() {
  local default_file
  local user_file
  local user_file_required="false"

  if (($# >= 1)); then
    default_file="$1"
  else
    default_file="$(services_default_config_file)"
  fi

  if (($# >= 2)); then
    user_file="$2"
    [[ -n "${user_file}" ]] && user_file_required="true"
  else
    user_file="$(services_user_config_file)"
    [[ -n "${SERVICE_INSTANCES_FILE:-}" ]] && user_file_required="true"
  fi

  services_reset
  SERVICES_DEFAULT_CONFIG_FILE="${default_file}"
  SERVICES_USER_CONFIG_FILE="${user_file}"

  services_parse_file "${SERVICES_DEFAULT_CONFIG_FILE}" || return 1

  if [[ -n "${SERVICES_USER_CONFIG_FILE}" &&
    (-e "${SERVICES_USER_CONFIG_FILE}" || -L "${SERVICES_USER_CONFIG_FILE}") ]]; then
    services_check_user_file_permissions "${SERVICES_USER_CONFIG_FILE}" || return 1
    if ! file_is_readable "${SERVICES_USER_CONFIG_FILE}"; then
      log_error "User service configuration is not readable: ${SERVICES_USER_CONFIG_FILE}."
      return 1
    fi
    services_parse_file "${SERVICES_USER_CONFIG_FILE}" || return 1
  elif [[ "${user_file_required}" == "true" ]]; then
    log_error "User service configuration is unavailable: ${SERVICES_USER_CONFIG_FILE}."
    return 1
  fi

  services_validate || return 1
  SERVICES_CONFIG_LOADED="true"
  log_ok "Loaded and validated ${#SERVICE_INSTANCE_IDS[@]} service instance configuration(s)."
}

services_has_instance() {
  local service_id="$1"
  [[ -n "${SERVICE_INSTANCE_KNOWN[${service_id}]+known}" ]]
}

services_get_instance() {
  local service_id="$1"
  local composite_key
  local path

  if [[ "${SERVICES_CONFIG_LOADED}" != "true" ]]; then
    log_error "Service instance configuration has not been loaded."
    return 1
  fi
  if ! services_has_instance "${service_id}"; then
    log_error "Unknown service instance id: ${service_id}."
    return 1
  fi

  SERVICE_INSTANCE_CONFIG=()
  for composite_key in "${!SERVICE_INSTANCE_VALUES[@]}"; do
    [[ "${composite_key}" == "${service_id}|"* ]] || continue
    path="${composite_key#*|}"
    SERVICE_INSTANCE_CONFIG["${path}"]="${SERVICE_INSTANCE_VALUES[${composite_key}]}"
  done
}

services_get_value() {
  local service_id="$1"
  local path="$2"

  services_get_instance "${service_id}" || return 1
  if [[ -z "${SERVICE_INSTANCE_CONFIG[${path}]+defined}" ]]; then
    return 1
  fi
  printf '%s\n' "${SERVICE_INSTANCE_CONFIG[${path}]}"
}

services_list_ids() {
  if ((${#SERVICE_INSTANCE_IDS[@]} > 0)); then
    printf '%s\n' "${SERVICE_INSTANCE_IDS[@]}"
  fi
}
