#!/usr/bin/env bash
set -Eeuo pipefail

DOMAINS_CONFIG_LOADED="false"
DOMAINS_DEFAULT_CONFIG_FILE=""
DOMAINS_USER_CONFIG_FILE=""

declare -a DOMAIN_BINDING_DOMAINS=()
declare -A DOMAIN_BINDING_VALUES=()
declare -A DOMAIN_BINDING_KNOWN=()
declare -A DOMAIN_BINDING_CONFIG=()

domains_default_config_file() {
  printf '%s\n' \
    "${DOMAIN_BINDINGS_DEFAULT_FILE:-${INSTALLER_ROOT}/config/domains/domains.yaml}"
}

domains_user_config_file() {
  printf '%s\n' \
    "${DOMAIN_BINDINGS_FILE:-${INSTALLER_ROOT}/config/domains/local.yaml}"
}

domains_reset() {
  DOMAINS_CONFIG_LOADED="false"
  DOMAINS_DEFAULT_CONFIG_FILE=""
  DOMAINS_USER_CONFIG_FILE=""
  DOMAIN_BINDING_DOMAINS=()
  DOMAIN_BINDING_VALUES=()
  DOMAIN_BINDING_KNOWN=()
  DOMAIN_BINDING_CONFIG=()
}

domains_field_is_supported() {
  case "$1" in
    domain | service_id | enabled | default_entry) return 0 ;;
    *) return 1 ;;
  esac
}

domains_commit_parsed_entry() {
  local config_file="$1"
  local -n entry_values_ref="$2"
  local -n entry_fields_ref="$3"
  local -n file_domains_ref="$4"
  local domain="${entry_values_ref[domain]-}"
  local normalized_domain
  local field

  if [[ -z "${domain}" ]]; then
    log_error "Domain binding entry is missing domain in ${config_file}."
    return 1
  fi
  normalized_domain="${domain,,}"
  if [[ "${domain}" != "${normalized_domain}" ]] ||
    ! validation_is_hostname "${domain}"; then
    log_error "Domain binding contains an invalid domain in ${config_file}."
    return 1
  fi

  if [[ -n "${file_domains_ref[${normalized_domain}]+defined}" ]]; then
    log_error "Duplicate domain binding '${normalized_domain}' in ${config_file}."
    return 1
  fi
  file_domains_ref["${normalized_domain}"]=true

  if [[ -z "${DOMAIN_BINDING_KNOWN[${normalized_domain}]+known}" ]]; then
    DOMAIN_BINDING_DOMAINS+=("${normalized_domain}")
    DOMAIN_BINDING_KNOWN["${normalized_domain}"]=true
  fi

  for field in "${entry_fields_ref[@]}"; do
    DOMAIN_BINDING_VALUES["${normalized_domain}|${field}"]="${entry_values_ref[${field}]}"
  done
  DOMAIN_BINDING_VALUES["${normalized_domain}|domain"]="${normalized_domain}"
}

domains_parse_file() {
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
  local -A file_domains=()
  local -a entry_fields=()

  if ! file_is_readable "${config_file}"; then
    log_error "Domain binding configuration is not readable: ${config_file}."
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
      if [[ "${content}" == "domains:" ]] ||
        [[ "${content}" =~ ^domains:[[:space:]]*\[\][[:space:]]*$ ]]; then
        collection_seen="true"
        continue
      fi

      log_error "Unsupported top-level domain configuration at line ${line_number} in ${config_file}."
      return 1
    fi

    if [[ "${collection_seen}" != "true" ]]; then
      log_error "Domain entries must appear below domains at line ${line_number} in ${config_file}."
      return 1
    fi

    if ((indent == 2)) &&
      [[ "${content}" =~ ^-[[:space:]]+domain:[[:space:]]*(.*)$ ]]; then
      raw_value="${BASH_REMATCH[1]}"
      if [[ "${entry_active}" == "true" ]]; then
        domains_commit_parsed_entry \
          "${config_file}" entry_values entry_fields file_domains || return 1
      fi

      entry_active="true"
      entry_values=()
      entry_seen_fields=()
      entry_fields=()
      if ! records_decode_scalar "${raw_value}" || [[ -z "${RECORDS_DECODED_VALUE}" ]]; then
        log_error "Domain is missing or invalid at line ${line_number} in ${config_file}."
        return 1
      fi
      entry_values[domain]="${RECORDS_DECODED_VALUE}"
      entry_seen_fields[domain]=true
      entry_fields+=(domain)
      continue
    fi

    if [[ "${entry_active}" != "true" ]] || ((indent != 4)) ||
      [[ ! "${content}" =~ ^([a-z][a-z0-9_]*):[[:space:]]*(.*)$ ]]; then
      log_error "Unsupported domain configuration syntax at line ${line_number} in ${config_file}."
      return 1
    fi

    field="${BASH_REMATCH[1]}"
    raw_value="${BASH_REMATCH[2]}"
    if ! domains_field_is_supported "${field}" || [[ "${field}" == "domain" ]]; then
      log_error "Unsupported domain binding field '${field}' in ${config_file}."
      return 1
    fi
    if [[ -n "${entry_seen_fields[${field}]+defined}" ]]; then
      log_error "Duplicate domain binding field '${field}' in ${config_file}."
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
    log_error "Domain configuration must declare supported version 1: ${config_file}."
    return 1
  fi
  if [[ "${collection_seen}" != "true" ]]; then
    log_error "Domain configuration must define a domains collection: ${config_file}."
    return 1
  fi
  if [[ "${entry_active}" == "true" ]]; then
    domains_commit_parsed_entry \
      "${config_file}" entry_values entry_fields file_domains || return 1
  fi
}

domains_value_exists() {
  local domain="$1"
  local field="$2"
  [[ -n "${DOMAIN_BINDING_VALUES[${domain}|${field}]+defined}" ]]
}

domains_validate_binding() {
  local domain="$1"
  local required_field
  local service_id
  local -a required_fields=(domain service_id enabled default_entry)

  for required_field in "${required_fields[@]}"; do
    if ! domains_value_exists "${domain}" "${required_field}"; then
      log_error "Domain binding '${domain}' is missing required field: ${required_field}."
      return 1
    fi
  done

  service_id="${DOMAIN_BINDING_VALUES[${domain}|service_id]}"
  if ! services_has_instance "${service_id}"; then
    log_error "Domain binding '${domain}' references unknown service instance '${service_id}'."
    return 1
  fi
  if ! validation_is_boolean "${DOMAIN_BINDING_VALUES[${domain}|enabled]}"; then
    log_error "Domain binding '${domain}' enabled state must be true or false."
    return 1
  fi
  if ! validation_is_boolean "${DOMAIN_BINDING_VALUES[${domain}|default_entry]}"; then
    log_error "Domain binding '${domain}' default_entry must be true or false."
    return 1
  fi
  if [[ "${DOMAIN_BINDING_VALUES[${domain}|default_entry]}" == "true" &&
    "${DOMAIN_BINDING_VALUES[${domain}|enabled]}" != "true" ]]; then
    log_error "Default domain binding '${domain}' must be enabled."
    return 1
  fi
  if [[ "${DOMAIN_BINDING_VALUES[${domain}|enabled]}" == "true" ]] &&
    ! services_is_enabled "${service_id}"; then
    log_error "Enabled domain binding '${domain}' references disabled service instance '${service_id}'."
    return 1
  fi
}

domains_validate() {
  local domain
  local default_entry_count=0

  if [[ "${SERVICES_CONFIG_LOADED}" != "true" ]]; then
    log_error "Service instance configuration must be loaded before domain bindings."
    return 1
  fi

  for domain in "${DOMAIN_BINDING_DOMAINS[@]}"; do
    domains_validate_binding "${domain}" || return 1
    if [[ "${DOMAIN_BINDING_VALUES[${domain}|default_entry]}" == "true" ]]; then
      default_entry_count=$((default_entry_count + 1))
    fi
  done

  if ((default_entry_count > 1)); then
    log_error "Only one enabled domain binding can be marked as the default entry."
    return 1
  fi
}

domains_load() {
  local default_file
  local user_file
  local user_file_required="false"

  if (($# >= 1)); then
    default_file="$1"
  else
    default_file="$(domains_default_config_file)"
  fi

  if (($# >= 2)); then
    user_file="$2"
    [[ -n "${user_file}" ]] && user_file_required="true"
  else
    user_file="$(domains_user_config_file)"
    [[ -n "${DOMAIN_BINDINGS_FILE:-}" ]] && user_file_required="true"
  fi

  domains_reset
  DOMAINS_DEFAULT_CONFIG_FILE="${default_file}"
  DOMAINS_USER_CONFIG_FILE="${user_file}"
  domains_parse_file "${DOMAINS_DEFAULT_CONFIG_FILE}" || return 1

  if [[ -n "${DOMAINS_USER_CONFIG_FILE}" &&
    (-e "${DOMAINS_USER_CONFIG_FILE}" || -L "${DOMAINS_USER_CONFIG_FILE}") ]]; then
    records_check_user_file_permissions \
      "${DOMAINS_USER_CONFIG_FILE}" "User domain configuration" || return 1
    if ! file_is_readable "${DOMAINS_USER_CONFIG_FILE}"; then
      log_error "User domain configuration is not readable: ${DOMAINS_USER_CONFIG_FILE}."
      return 1
    fi
    domains_parse_file "${DOMAINS_USER_CONFIG_FILE}" || return 1
  elif [[ "${user_file_required}" == "true" ]]; then
    log_error "User domain configuration is unavailable: ${DOMAINS_USER_CONFIG_FILE}."
    return 1
  fi

  domains_validate || return 1
  DOMAINS_CONFIG_LOADED="true"
  log_ok "Loaded and validated ${#DOMAIN_BINDING_DOMAINS[@]} domain binding configuration(s)."
}

domains_has_binding() {
  local domain="${1,,}"
  [[ -n "${DOMAIN_BINDING_KNOWN[${domain}]+known}" ]]
}

domains_get_binding() {
  local domain="${1,,}"
  local composite_key
  local field

  if [[ "${DOMAINS_CONFIG_LOADED}" != "true" ]]; then
    log_error "Domain binding configuration has not been loaded."
    return 1
  fi
  if ! domains_has_binding "${domain}"; then
    log_error "Unknown domain binding: ${domain}."
    return 1
  fi

  DOMAIN_BINDING_CONFIG=()
  for composite_key in "${!DOMAIN_BINDING_VALUES[@]}"; do
    [[ "${composite_key}" == "${domain}|"* ]] || continue
    field="${composite_key#*|}"
    DOMAIN_BINDING_CONFIG["${field}"]="${DOMAIN_BINDING_VALUES[${composite_key}]}"
  done
}

domains_get_service_id() {
  local domain="$1"

  domains_get_binding "${domain}" || return 1
  printf '%s\n' "${DOMAIN_BINDING_CONFIG[service_id]}"
}

domains_resolve_service_id() {
  local domain="$1"

  domains_get_binding "${domain}" || return 1
  if [[ "${DOMAIN_BINDING_CONFIG[enabled]}" != "true" ]]; then
    log_error "Domain binding '${domain,,}' is disabled."
    return 1
  fi
  printf '%s\n' "${DOMAIN_BINDING_CONFIG[service_id]}"
}

domains_list() {
  if ((${#DOMAIN_BINDING_DOMAINS[@]} > 0)); then
    printf '%s\n' "${DOMAIN_BINDING_DOMAINS[@]}"
  fi
}
