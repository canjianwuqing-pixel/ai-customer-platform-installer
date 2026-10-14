#!/usr/bin/env bash
set -Eeuo pipefail

domain_certbot() {
  run_as_root certbot \
    --config-dir "${CERTBOT_CONFIG_DIR}" \
    --work-dir "${CERTBOT_WORK_DIR}" \
    --logs-dir "${CERTBOT_LOGS_DIR}" \
    "$@"
}

domain_certbot_nginx_plugin_available() {
  local plugins

  plugins="$(domain_certbot plugins 2>/dev/null)" || return 1
  grep -Eq '(^|[^[:alnum:]_-])nginx([^[:alnum:]_-]|$)' <<<"${plugins}"
}

domain_install_certbot() {
  local certbot_version

  if command_exists certbot && domain_certbot_nginx_plugin_available; then
    certbot_version="$(certbot --version 2>&1 || true)"
    log_ok "[domain] Existing Certbot with the Nginx plugin will be reused: ${certbot_version:-version unavailable}."
    return 0
  fi

  if ! command_exists apt-get; then
    log_error "Certbot installation requires apt-get on Ubuntu."
    return 1
  fi

  if ! current_user_is_root && ! command_exists sudo; then
    log_error "Certbot installation requires root or sudo privileges."
    return 1
  fi

  log_info "[domain] Installing Certbot and its Nginx plugin."
  if ! run_as_root apt-get update; then
    log_error "Failed to refresh the Ubuntu package index before installing Certbot."
    return 1
  fi

  if ! run_as_root env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-remove certbot python3-certbot-nginx; then
    log_error "Failed to install Certbot and the Nginx plugin."
    return 1
  fi

  if ! command_exists certbot || ! domain_certbot_nginx_plugin_available; then
    log_error "Certbot installation completed, but Certbot or its Nginx plugin is unavailable."
    return 1
  fi

  log_ok "[domain] Certbot and its Nginx plugin are available."
}

domain_certificate_files_exist() {
  run_as_root test -s "${CERTBOT_FULLCHAIN_PATH}" &&
    run_as_root test -s "${CERTBOT_PRIVATE_KEY_PATH}"
}

domain_request_certificate() {
  log_info "[domain] Requesting or reusing the Let's Encrypt certificate for ${DOMAIN}."

  if ! domain_certbot certonly \
    --nginx \
    --non-interactive \
    --agree-tos \
    --no-eff-email \
    --keep-until-expiring \
    --email "${LETSENCRYPT_EMAIL}" \
    --cert-name "${DOMAIN}" \
    --domains "${DOMAIN}"; then
    log_error "Let's Encrypt certificate issuance failed for ${DOMAIN}."
    return 1
  fi

  if ! domain_certificate_files_exist; then
    log_error "Certbot completed without creating the expected certificate files."
    return 1
  fi

  log_ok "[domain] Certificate files are available for ${DOMAIN}."
}

domain_render_renew_hook() {
  local output_file="$1"

  sed \
    -e "s|{{NGINX_SERVICE_NAME}}|${NGINX_SERVICE_NAME}|g" \
    "${DOMAIN_RENEW_HOOK_TEMPLATE_FILE}" >"${output_file}"
}

domain_install_renew_hook() {
  local candidate_file
  local staged_path="${CERTBOT_RENEW_HOOK_PATH}.new.$$"

  if run_as_root test -e "${CERTBOT_RENEW_HOOK_PATH}" ||
    run_as_root test -L "${CERTBOT_RENEW_HOOK_PATH}"; then
    if ! run_as_root grep -Fq -- '# Managed by AI Customer Platform Installer domain module.' "${CERTBOT_RENEW_HOOK_PATH}"; then
      log_error "Existing Certbot deploy hook is not managed by this installer: ${CERTBOT_RENEW_HOOK_PATH}"
      return 1
    fi
  fi

  candidate_file="$(mktemp)"
  if ! domain_render_renew_hook "${candidate_file}"; then
    rm -f -- "${candidate_file}"
    log_error "Failed to render the Certbot deploy hook."
    return 1
  fi

  if ! run_as_root install -d -m 0755 "${CERTBOT_RENEWAL_HOOKS_DIR}" ||
    ! run_as_root install -m 0755 "${candidate_file}" "${staged_path}" ||
    ! run_as_root mv -f -- "${staged_path}" "${CERTBOT_RENEW_HOOK_PATH}"; then
    run_as_root rm -f -- "${staged_path}" || true
    rm -f -- "${candidate_file}"
    log_error "Failed to install the Certbot deploy hook."
    return 1
  fi

  rm -f -- "${candidate_file}"
  log_ok "[domain] Certbot deploy hook will validate and reload Nginx after renewal."
}

domain_render_https_template() {
  local output_file="$1"

  if ! sed \
    -e "s|{{DOMAIN}}|${DOMAIN}|g" \
    -e "s|{{NGINX_LISTEN_PORT}}|${NGINX_LISTEN_PORT}|g" \
    -e "s|{{DOMAIN_HTTPS_PORT}}|${DOMAIN_HTTPS_PORT}|g" \
    -e "s|{{NGINX_UPSTREAM_HOST}}|${NGINX_UPSTREAM_HOST}|g" \
    -e "s|{{CHATWOOT_HTTP_PORT}}|${CHATWOOT_HTTP_PORT}|g" \
    -e "s|{{NGINX_CLIENT_MAX_BODY_SIZE}}|${NGINX_CLIENT_MAX_BODY_SIZE}|g" \
    -e "s|{{NGINX_PROXY_READ_TIMEOUT_SECONDS}}|${NGINX_PROXY_READ_TIMEOUT_SECONDS}|g" \
    -e "s|{{CERTBOT_FULLCHAIN_PATH}}|${CERTBOT_FULLCHAIN_PATH}|g" \
    -e "s|{{CERTBOT_PRIVATE_KEY_PATH}}|${CERTBOT_PRIVATE_KEY_PATH}|g" \
    "${DOMAIN_HTTPS_TEMPLATE_FILE}" >"${output_file}"; then
    log_error "Failed to render the HTTPS Nginx template."
    return 1
  fi

  if grep -Eq '{{[A-Z0-9_]+}}' "${output_file}"; then
    log_error "Rendered HTTPS Nginx configuration contains unresolved template variables."
    return 1
  fi
}

domain_activate_https() {
  local candidate_file

  candidate_file="$(mktemp)"
  if ! domain_render_https_template "${candidate_file}"; then
    rm -f -- "${candidate_file}"
    return 1
  fi

  if ! nginx_apply_candidate "${candidate_file}"; then
    rm -f -- "${candidate_file}"
    log_error "HTTPS activation failed; the previously active Nginx configuration was restored."
    return 1
  fi

  rm -f -- "${candidate_file}"
  log_ok "[domain] HTTPS is active for ${DOMAIN}."
}

domain_enable_automatic_renewal() {
  if ! command_exists systemctl; then
    log_error "Automatic Certbot renewal requires systemd on supported Ubuntu releases."
    return 1
  fi

  if ! run_as_root systemctl enable --now "${CERTBOT_RENEWAL_TIMER}"; then
    log_error "Failed to enable the Certbot renewal timer."
    return 1
  fi

  log_ok "[domain] Certbot automatic-renewal timer is enabled."
}

domain_test_renewal() {
  if [[ "${CERTBOT_RENEW_DRY_RUN}" == "false" ]]; then
    log_warn "[domain] Certbot renewal dry-run is disabled by configuration."
    return 0
  fi

  log_info "[domain] Testing automatic certificate renewal with Certbot staging."
  if ! domain_certbot renew --dry-run --cert-name "${DOMAIN}"; then
    log_error "Certbot renewal dry-run failed for ${DOMAIN}."
    return 1
  fi

  log_ok "[domain] Certbot renewal dry-run succeeded."
}

domain_ssl() {
  domain_initialize_config
  log_info "[domain] Configuring Let's Encrypt HTTPS for ${DOMAIN}."

  domain_validate_config
  domain_install_certbot
  domain_request_certificate
  domain_install_renew_hook
  domain_activate_https
  domain_enable_automatic_renewal
}
