#!/usr/bin/env bash
set -Eeuo pipefail

SERVICES_CONFIG_LOADED="false"
SERVICES_DEFAULT_CONFIG_FILE=""
SERVICES_USER_CONFIG_FILE=""

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

services_field_is_supported() {
  case "$1" in
    id | name | enabled | ai_config | prompt_config | knowledge_config | channel_config)
      return 0
      ;;
    *) return 1 ;;
  esac
}

services_commit_parsed_entry() {
  local config_file="$1"
  local -n entry_values_ref="$2"
  local -n entry_fields_ref="$3"
  local -n file_ids_ref="$4"
  local service_id="${entry_values_ref[id]-}"
  local field

  if [[ -z "${service_id}" ]]; then
    log_error "Service instance entry is missing id in ${config_file}."
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

  for field in "${entry_fields_ref[@]}"; do
    SERVICE_INSTANCE_VALUES["${service_id}|${field}"]="${entry_values_ref[${field}]}"
  done
}

services_parse_file() {
  local config_file="$1"
  local line
  local content
  local leading_spaces
  local field
  local raw_value
  local version=""
  local line_number=0
  local indent=0
  local collection_seen="false"
  local entry_active="false"
  local -A entry_values=()
  local -A entry_seen_fields=()
  local -A file_ids=()
  local -a entry_fields=()

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

    if ((indent == 0)); then
      if [[ "${content}" =~ ^version:[[:space:]]*([0-9]+)[[:space:]]*$ ]]; then
        version="${BASH_REMATCH[1]}"
        continue
      fi
      if [[ "${content}" == "services:" ]] ||
        [[ "${content}" =~ ^services:[[:space:]]*\[\][[:space:]]*$ ]]; then
        collection_seen="true"
        continue
      fi

      log_error "Unsupported top-level service configuration at line ${line_number} in ${config_file}."
      return 1
    fi

    if [[ "${collection_seen}" != "true" ]]; then
      log_error "Service entries must appear below services at line ${line_number} in ${config_file}."
      return 1
    fi

    if ((indent == 2)) &&
      [[ "${content}" =~ ^-[[:space:]]+id:[[:space:]]*(.*)$ ]]; then
      raw_value="${BASH_REMATCH[1]}"
      if [[ "${entry_active}" == "true" ]]; then
        services_commit_parsed_entry \
          "${config_file}" entry_values entry_fields file_ids || return 1
      fi

      entry_active="true"
      entry_values=()
      entry_seen_fields=()
      entry_fields=()
      if ! records_decode_scalar "${raw_value}" || [[ -z "${RECORDS_DECODED_VALUE}" ]]; then
        log_error "Service instance id is missing or invalid at line ${line_number} in ${config_file}."
        return 1
      fi
      entry_values[id]="${RECORDS_DECODED_VALUE}"
      entry_seen_fields[id]=true
      entry_fields+=(id)
      continue
    fi

    if [[ "${entry_active}" != "true" ]] || ((indent != 4)) ||
      [[ ! "${content}" =~ ^([a-z][a-z0-9_]*):[[:space:]]*(.*)$ ]]; then
      log_error "Unsupported service configuration syntax at line ${line_number} in ${config_file}."
      return 1
    fi

    field="${BASH_REMATCH[1]}"
    raw_value="${BASH_REMATCH[2]}"
    if ! services_field_is_supported "${field}" || [[ "${field}" == "id" ]]; then
      log_error "Unsupported service instance field '${field}' in ${config_file}."
      return 1
    fi
    if [[ -n "${entry_seen_fields[${field}]+defined}" ]]; then
      log_error "Duplicate service instance field '${field}' in ${config_file}."
      return 1
    fi
    if ! records_decode_scalar "${raw_value}"; then
      log_error "Unsupported scalar value at line ${line_number} in ${config_file}."
      return 1
    fi

    entry_values["${field}"]="${RECORDS_DECODED_VALUE}"
    entry_seen_fields["${field}"]=true
    entry_fields+=("${field}")
  done <"${config_file}"

  if [[ "${version}" != "1" ]]; then
    log_error "Service configuration must declare supported version 1: ${config_file}."
    return 1
  fi
  if [[ "${collection_seen}" != "true" ]]; then
    log_error "Service configuration must define a services collection: ${config_file}."
    return 1
  fi
  if [[ "${entry_active}" == "true" ]]; then
    services_commit_parsed_entry \
      "${config_file}" entry_values entry_fields file_ids || return 1
  fi
}

services_value_exists() {
  local service_id="$1"
  local field="$2"
  [[ -n "${SERVICE_INSTANCE_VALUES[${service_id}|${field}]+defined}" ]]
}

services_validate_reference() {
  local service_id="$1"
  local field="$2"
  local value="${SERVICE_INSTANCE_VALUES[${service_id}|${field}]}"

  if [[ -n "${value}" && ! "${value}" =~ ^[a-z][a-z0-9._/-]{0,127}$ ]]; then
    log_error "Service instance '${service_id}' field '${field}' must be an empty value or a generic configuration reference."
    return 1
  fi
}

services_validate_instance() {
  local service_id="$1"
  local required_field
  local -a required_fields=(
    id
    name
    enabled
    ai_config
    prompt_config
    knowledge_config
    channel_config
  )
  local -a reference_fields=(
    ai_config
    prompt_config
    knowledge_config
    channel_config
  )

  for required_field in "${required_fields[@]}"; do
    if ! services_value_exists "${service_id}" "${required_field}"; then
      log_error "Service instance '${service_id}' is missing required field: ${required_field}."
      return 1
    fi
  done

  if [[ "${SERVICE_INSTANCE_VALUES[${service_id}|id]}" != "${service_id}" ]]; then
    log_error "Service instance identifier mismatch for '${service_id}'."
    return 1
  fi
  if [[ -z "${SERVICE_INSTANCE_VALUES[${service_id}|name]}" ||
    ${#SERVICE_INSTANCE_VALUES[${service_id}|name]} -gt 128 ]]; then
    log_error "Service instance '${service_id}' must have a name of 1-128 characters."
    return 1
  fi
  if ! validation_is_boolean "${SERVICE_INSTANCE_VALUES[${service_id}|enabled]}"; then
    log_error "Service instance '${service_id}' enabled state must be true or false."
    return 1
  fi

  for required_field in "${reference_fields[@]}"; do
    services_validate_reference "${service_id}" "${required_field}" || return 1
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
    records_check_user_file_permissions \
      "${SERVICES_USER_CONFIG_FILE}" "User service configuration" || return 1
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

services_is_enabled() {
  local service_id="$1"

  services_has_instance "${service_id}" &&
    [[ "${SERVICE_INSTANCE_VALUES[${service_id}|enabled]}" == "true" ]]
}

services_get_instance() {
  local service_id="$1"
  local composite_key
  local field

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
    field="${composite_key#*|}"
    SERVICE_INSTANCE_CONFIG["${field}"]="${SERVICE_INSTANCE_VALUES[${composite_key}]}"
  done
}

services_get_value() {
  local service_id="$1"
  local field="$2"

  services_get_instance "${service_id}" || return 1
  if [[ -z "${SERVICE_INSTANCE_CONFIG[${field}]+defined}" ]]; then
    return 1
  fi
  printf '%s\n' "${SERVICE_INSTANCE_CONFIG[${field}]}"
}

services_list_ids() {
  if ((${#SERVICE_INSTANCE_IDS[@]} > 0)); then
    printf '%s\n' "${SERVICE_INSTANCE_IDS[@]}"
  fi
}
