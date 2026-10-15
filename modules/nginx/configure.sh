#!/usr/bin/env bash
set -Eeuo pipefail

nginx_validate_config() {
  validation_require_keys \
    DOMAIN \
    CHATWOOT_HTTP_PORT \
    NGINX_LISTEN_PORT \
    NGINX_UPSTREAM_HOST \
    NGINX_SITE_NAME \
    NGINX_CLIENT_MAX_BODY_SIZE \
    NGINX_PROXY_READ_TIMEOUT_SECONDS

  if ! validation_is_hostname "${DOMAIN}"; then
    log_error "Invalid Nginx server domain: ${DOMAIN}"
    return 1
  fi

  if ! validation_is_port "${NGINX_LISTEN_PORT}" || ! validation_is_port "${CHATWOOT_HTTP_PORT}"; then
    log_error "Nginx listen and Chatwoot upstream ports must be between 1 and 65535."
    return 1
  fi

  if ! validation_is_bind_address "${NGINX_UPSTREAM_HOST}"; then
    log_error "NGINX_UPSTREAM_HOST must be a valid IPv4 address or hostname."
    return 1
  fi

  if [[ ! "${NGINX_SITE_NAME}" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    log_error "NGINX_SITE_NAME must use lowercase letters, digits, dots, hyphens, or underscores."
    return 1
  fi

  if [[ ! "${NGINX_CLIENT_MAX_BODY_SIZE}" =~ ^[1-9][0-9]*[kKmMgG]$ ]]; then
    log_error "NGINX_CLIENT_MAX_BODY_SIZE must use a positive size such as 40m."
    return 1
  fi

  if ! validation_is_positive_integer "${NGINX_PROXY_READ_TIMEOUT_SECONDS}"; then
    log_error "NGINX_PROXY_READ_TIMEOUT_SECONDS must be a positive integer."
    return 1
  fi

  if [[ "${NGINX_CONFIG_ROOT}" != /* ]] ||
    [[ "${NGINX_SITES_AVAILABLE_DIR}" != /* ]] ||
    [[ "${NGINX_SITES_ENABLED_DIR}" != /* ]]; then
    log_error "Nginx configuration paths must be absolute."
    return 1
  fi

  if ! file_is_readable "${NGINX_TEMPLATE_FILE}"; then
    log_error "Nginx template is unavailable: ${NGINX_TEMPLATE_FILE}"
    return 1
  fi
}

nginx_https_port_suffix() {
  local port="${DOMAIN_HTTPS_PORT:-443}"
  validation_is_port "${port}" || { log_error 'Invalid HTTPS redirect port.'; return 1; }
  if [[ "${port}" != 443 ]]; then printf ':%s' "${port}"; fi
}

nginx_render_admin_location() {
  local output_file="$1" snippet combined
  snippet="$(mktemp)"; combined="$(mktemp)"
  if [[ "${ADMIN_ENABLED:-false}" == true ]]; then
    local https_port_suffix
    https_port_suffix="$(nginx_https_port_suffix)" || { rm -f -- "${snippet}" "${combined}"; return 1; }
    if ! validation_is_bind_address "${ADMIN_UPSTREAM_HOST:-}" ||
      ! validation_is_port "${AI_ADMIN_HTTP_PORT:-}"; then
      rm -f -- "${snippet}" "${combined}"
      log_error "Unified Admin upstream configuration is invalid."
      return 1
    fi
    sed -e "s|{{ADMIN_UPSTREAM_HOST}}|${ADMIN_UPSTREAM_HOST}|g" \
      -e "s|{{AI_ADMIN_HTTP_PORT}}|${AI_ADMIN_HTTP_PORT}|g" \
      -e "s|{{HTTPS_PORT_SUFFIX}}|${https_port_suffix}|g" \
      "${INSTALLER_ROOT}/templates/nginx/admin.conf.template" >"${snippet}" || return 1
    if [[ "${AI_RUNTIME_BUILD:-false}" == true ]]; then
      if ! validation_is_bind_address "${AI_SERVICE_BIND_ADDRESS:-}" || ! validation_is_port "${AI_SERVICE_HTTP_PORT:-}"; then
        rm -f -- "${snippet}" "${combined}"
        log_error 'AI Runtime webhook upstream configuration is invalid.'
        return 1
      fi
      sed -e "s|{{AI_RUNTIME_UPSTREAM_HOST}}|${AI_SERVICE_BIND_ADDRESS}|g" \
        -e "s|{{AI_SERVICE_HTTP_PORT}}|${AI_SERVICE_HTTP_PORT}|g" \
        -e "s|{{HTTPS_PORT_SUFFIX}}|${https_port_suffix}|g" \
        "${INSTALLER_ROOT}/templates/nginx/chatwoot-ai.conf.template" >>"${snippet}" || return 1
    fi
  fi
  awk -v snippet="${snippet}" '/# PLATFORM_ADMIN_LOCATION/ {while ((getline line < snippet) > 0) print line; close(snippet); next} {print}' \
    "${output_file}" >"${combined}" || return 1
  mv -- "${combined}" "${output_file}"
  rm -f -- "${snippet}"
}

nginx_render_template() {
  local output_file="$1"

  if ! sed \
    -e "s|{{DOMAIN}}|${DOMAIN}|g" \
    -e "s|{{NGINX_LISTEN_PORT}}|${NGINX_LISTEN_PORT}|g" \
    -e "s|{{NGINX_UPSTREAM_HOST}}|${NGINX_UPSTREAM_HOST}|g" \
    -e "s|{{CHATWOOT_HTTP_PORT}}|${CHATWOOT_HTTP_PORT}|g" \
    -e "s|{{NGINX_CLIENT_MAX_BODY_SIZE}}|${NGINX_CLIENT_MAX_BODY_SIZE}|g" \
    -e "s|{{NGINX_PROXY_READ_TIMEOUT_SECONDS}}|${NGINX_PROXY_READ_TIMEOUT_SECONDS}|g" \
    "${NGINX_TEMPLATE_FILE}" >"${output_file}"; then
    log_error "Failed to render the Chatwoot Nginx template."
    return 1
  fi

  nginx_render_admin_location "${output_file}" || return 1
  if grep -Eq '{{[A-Z0-9_]+}}' "${output_file}"; then
    log_error "Rendered Nginx configuration contains unresolved template variables."
    return 1
  fi
}

nginx_test_configuration() {
  if ! run_as_root nginx -t; then
    log_error "Nginx configuration syntax check failed."
    return 1
  fi

  log_ok "[nginx] Nginx configuration syntax is valid."
}

nginx_reload_service() {
  if command_exists systemctl; then
    if nginx_service_is_active; then
      run_as_root systemctl reload "${NGINX_SERVICE_NAME}"
    else
      log_warn "[nginx] Service is inactive; starting it after successful configuration validation."
      run_as_root systemctl enable --now "${NGINX_SERVICE_NAME}"
    fi
    return $?
  fi

  if command_exists service; then
    if nginx_service_is_active; then
      run_as_root service "${NGINX_SERVICE_NAME}" reload
    else
      log_warn "[nginx] Service is inactive; starting it after successful configuration validation."
      run_as_root service "${NGINX_SERVICE_NAME}" start
    fi
    return $?
  fi

  log_error "Neither systemctl nor service is available to reload Nginx."
  return 1
}

nginx_enabled_site_is_compatible() {
  local link_target
  local resolved_link_target
  local resolved_site_path

  if [[ ! -e "${NGINX_SITE_ENABLED_PATH}" && ! -L "${NGINX_SITE_ENABLED_PATH}" ]]; then
    return 0
  fi

  if [[ ! -L "${NGINX_SITE_ENABLED_PATH}" ]]; then
    if [[ -f "${NGINX_SITE_ENABLED_PATH}" ]] &&
      run_as_root grep -Fq -- '# Managed by AI Customer Platform Installer.' "${NGINX_SITE_ENABLED_PATH}" &&
      run_as_root cmp -s -- "${NGINX_SITE_ENABLED_PATH}" "${NGINX_SITE_CONFIG_PATH}"; then
      log_warn "[nginx] Managed enabled-site is a regular file; it will be migrated safely."
      return 2
    fi

    log_error "Nginx enabled-site path already exists and is not a compatible managed site: ${NGINX_SITE_ENABLED_PATH}"
    return 1
  fi

  link_target="$(readlink -- "${NGINX_SITE_ENABLED_PATH}")"
  if [[ "${link_target}" == /* ]]; then
    resolved_link_target="$(readlink -m -- "${link_target}")"
  else
    resolved_link_target="$(readlink -m -- "$(dirname -- "${NGINX_SITE_ENABLED_PATH}")/${link_target}")"
  fi
  resolved_site_path="$(readlink -m -- "${NGINX_SITE_CONFIG_PATH}")"

  if [[ "${resolved_link_target}" != "${resolved_site_path}" ]]; then
    log_error "Nginx enabled-site symlink is owned by another configuration: ${NGINX_SITE_ENABLED_PATH}"
    return 1
  fi
}

nginx_rollback_site_config() {
  local had_existing_config="$1"
  local backup_path="$2"
  local config_changed="$3"
  local enabled_link_created="$4"
  local enabled_regular_replaced="${5:-false}"

  if [[ "${enabled_link_created}" == "true" ]]; then
    run_as_root rm -f -- "${NGINX_SITE_ENABLED_PATH}" || true
  fi

  if [[ "${config_changed}" == "true" ]]; then
    if [[ "${had_existing_config}" == "true" && -n "${backup_path}" ]]; then
      run_as_root mv -f -- "${backup_path}" "${NGINX_SITE_CONFIG_PATH}" || true
    else
      run_as_root rm -f -- "${NGINX_SITE_CONFIG_PATH}" || true
    fi
  elif [[ -n "${backup_path}" ]]; then
    run_as_root rm -f -- "${backup_path}" || true
  fi

  if [[ "${enabled_regular_replaced}" == "true" ]]; then
    run_as_root cp -a -- "${NGINX_SITE_CONFIG_PATH}" "${NGINX_SITE_ENABLED_PATH}" || true
  fi

  log_warn "[nginx] Restored the previously active site configuration."
}

nginx_apply_candidate() {
  local candidate_file="$1"
  local backup_path=""
  local staged_path="${NGINX_SITE_CONFIG_PATH}.new.$$"
  local had_existing_config="false"
  local config_changed="false"
  local enabled_link_created="false"
  local enabled_regular_site="false"
  local enabled_regular_replaced="false"
  local enabled_site_status=0

  if [[ ! -d "${NGINX_SITES_AVAILABLE_DIR}" ]] &&
    ! run_as_root install -d -m 0755 "${NGINX_SITES_AVAILABLE_DIR}"; then
    log_error "Failed to create the Nginx sites-available directory."
    return 1
  fi

  if [[ ! -d "${NGINX_SITES_ENABLED_DIR}" ]] &&
    ! run_as_root install -d -m 0755 "${NGINX_SITES_ENABLED_DIR}"; then
    log_error "Failed to create the Nginx sites-enabled directory."
    return 1
  fi

  if nginx_enabled_site_is_compatible; then
    :
  else
    enabled_site_status=$?
    if ((enabled_site_status == 2)); then
      enabled_regular_site="true"
    else
      return 1
    fi
  fi

  if [[ -L "${NGINX_SITE_CONFIG_PATH}" ]]; then
    log_error "Managed Nginx site path must not be a symlink: ${NGINX_SITE_CONFIG_PATH}"
    return 1
  fi

  if [[ -f "${NGINX_SITE_CONFIG_PATH}" ]]; then
    had_existing_config="true"
    if ! run_as_root grep -Fq -- '# Managed by AI Customer Platform Installer.' "${NGINX_SITE_CONFIG_PATH}"; then
      log_error "Existing Nginx site is not managed by this installer and will not be overwritten: ${NGINX_SITE_CONFIG_PATH}"
      return 1
    fi

    if run_as_root cmp -s -- "${candidate_file}" "${NGINX_SITE_CONFIG_PATH}"; then
      log_ok "[nginx] Existing managed site configuration is already current."
    else
      backup_path="${NGINX_SITE_CONFIG_PATH}.backup.$(date '+%Y%m%d%H%M%S').$$"
      if ! run_as_root cp -a -- "${NGINX_SITE_CONFIG_PATH}" "${backup_path}"; then
        log_error "Failed to back up the existing Nginx site configuration."
        return 1
      fi
    fi
  elif [[ -e "${NGINX_SITE_CONFIG_PATH}" ]]; then
    log_error "Managed Nginx site path exists but is not a regular file: ${NGINX_SITE_CONFIG_PATH}"
    return 1
  fi

  if [[ "${had_existing_config}" == "false" || -n "${backup_path}" ]]; then
    if ! run_as_root install -m 0644 "${candidate_file}" "${staged_path}"; then
      [[ -n "${backup_path}" ]] && run_as_root rm -f -- "${backup_path}" || true
      log_error "Failed to stage the generated Nginx site configuration."
      return 1
    fi

    if ! run_as_root mv -f -- "${staged_path}" "${NGINX_SITE_CONFIG_PATH}"; then
      run_as_root rm -f -- "${staged_path}" || true
      [[ -n "${backup_path}" ]] && run_as_root rm -f -- "${backup_path}" || true
      log_error "Failed to install the generated Nginx site configuration."
      return 1
    fi
    config_changed="true"
  fi

  if [[ "${enabled_regular_site}" == "true" && "${config_changed}" == "true" ]]; then
    if ! run_as_root rm -f -- "${NGINX_SITE_ENABLED_PATH}"; then
      nginx_rollback_site_config \
        "${had_existing_config}" "${backup_path}" "${config_changed}" "${enabled_link_created}" "${enabled_regular_replaced}"
      log_error "Failed to replace the managed regular enabled-site file."
      return 1
    fi
    enabled_regular_replaced="true"
  fi

  if [[ ! -e "${NGINX_SITE_ENABLED_PATH}" && ! -L "${NGINX_SITE_ENABLED_PATH}" ]]; then
    if ! run_as_root ln -s "${NGINX_SITE_CONFIG_PATH}" "${NGINX_SITE_ENABLED_PATH}"; then
      nginx_rollback_site_config \
        "${had_existing_config}" "${backup_path}" "${config_changed}" "${enabled_link_created}" "${enabled_regular_replaced}"
      log_error "Failed to enable the Chatwoot Nginx site."
      return 1
    fi
    enabled_link_created="true"
  fi

  if ! nginx_test_configuration; then
    nginx_rollback_site_config \
      "${had_existing_config}" "${backup_path}" "${config_changed}" "${enabled_link_created}" "${enabled_regular_replaced}"
    return 1
  fi

  if ! nginx_reload_service; then
    nginx_rollback_site_config \
      "${had_existing_config}" "${backup_path}" "${config_changed}" "${enabled_link_created}" "${enabled_regular_replaced}"
    if nginx_test_configuration; then
      if ! nginx_reload_service; then
        log_warn "[nginx] Restored configuration is valid on disk, but the service still could not be reloaded."
      fi
    else
      log_warn "[nginx] Restored configuration also fails validation; the running Nginx process was left untouched."
    fi
    log_error "Nginx configuration was valid, but the service could not be reloaded."
    return 1
  fi

  if [[ -n "${backup_path}" ]]; then
    log_info "[nginx] Previous managed site configuration retained at ${backup_path}."
  fi

  log_ok "[nginx] Chatwoot site configuration is active."
}

nginx_configure() {
  local candidate_file

  nginx_initialize_config
  log_info "[nginx] Rendering the Chatwoot reverse-proxy configuration."

  if ! nginx_is_installed; then
    log_error "Nginx configuration cannot continue because nginx is unavailable."
    return 1
  fi

  nginx_validate_config

  if [[ -f "${NGINX_SITE_CONFIG_PATH}" ]] &&
    run_as_root grep -Fq -- '# Managed by AI Customer Platform Installer domain module.' "${NGINX_SITE_CONFIG_PATH}"; then
    log_ok "[nginx] Domain module already manages the HTTPS site; preserving its active configuration."
    nginx_test_configuration
    return 0
  fi

  candidate_file="$(mktemp)"

  if ! nginx_render_template "${candidate_file}"; then
    rm -f -- "${candidate_file}"
    return 1
  fi

  if ! nginx_apply_candidate "${candidate_file}"; then
    rm -f -- "${candidate_file}"
    return 1
  fi

  rm -f -- "${candidate_file}"
}
