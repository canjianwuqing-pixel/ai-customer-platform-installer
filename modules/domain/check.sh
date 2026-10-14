#!/usr/bin/env bash
set -Eeuo pipefail

DOMAIN_CERTIFICATE_PROVIDER="${DOMAIN_CERTIFICATE_PROVIDER:-}"
DOMAIN_CHALLENGE_TYPE="${DOMAIN_CHALLENGE_TYPE:-}"
DOMAIN_DNS_CHECK="${DOMAIN_DNS_CHECK:-}"
DOMAIN_HTTPS_PORT="${DOMAIN_HTTPS_PORT:-}"
DOMAIN_HTTPS_TEMPLATE_FILE="${DOMAIN_HTTPS_TEMPLATE_FILE:-}"
DOMAIN_RENEW_HOOK_TEMPLATE_FILE="${DOMAIN_RENEW_HOOK_TEMPLATE_FILE:-}"
CERTBOT_CONFIG_DIR="${CERTBOT_CONFIG_DIR:-}"
CERTBOT_WORK_DIR="${CERTBOT_WORK_DIR:-}"
CERTBOT_LOGS_DIR="${CERTBOT_LOGS_DIR:-}"
CERTBOT_RENEWAL_HOOKS_DIR="${CERTBOT_RENEWAL_HOOKS_DIR:-}"
CERTBOT_RENEW_HOOK_PATH="${CERTBOT_RENEW_HOOK_PATH:-}"
CERTBOT_CERTIFICATE_DIR="${CERTBOT_CERTIFICATE_DIR:-}"
CERTBOT_FULLCHAIN_PATH="${CERTBOT_FULLCHAIN_PATH:-}"
CERTBOT_PRIVATE_KEY_PATH="${CERTBOT_PRIVATE_KEY_PATH:-}"
CERTBOT_RENEW_DRY_RUN="${CERTBOT_RENEW_DRY_RUN:-}"
CERTBOT_RENEWAL_TIMER="${CERTBOT_RENEWAL_TIMER:-}"

domain_initialize_config() {
  nginx_initialize_config

  DOMAIN_CERTIFICATE_PROVIDER="${DOMAIN_CERTIFICATE_PROVIDER:-letsencrypt}"
  DOMAIN_CHALLENGE_TYPE="${DOMAIN_CHALLENGE_TYPE:-http-01}"
  DOMAIN_DNS_CHECK="${DOMAIN_DNS_CHECK:-true}"
  DOMAIN_HTTPS_PORT="${DOMAIN_HTTPS_PORT:-443}"
  DOMAIN_HTTPS_TEMPLATE_FILE="${DOMAIN_HTTPS_TEMPLATE_FILE:-${INSTALLER_ROOT}/templates/domain/chatwoot-https.conf.template}"
  DOMAIN_RENEW_HOOK_TEMPLATE_FILE="${DOMAIN_RENEW_HOOK_TEMPLATE_FILE:-${INSTALLER_ROOT}/templates/domain/renew-nginx.sh.template}"
  CERTBOT_CONFIG_DIR="${CERTBOT_CONFIG_DIR:-/etc/letsencrypt}"
  CERTBOT_WORK_DIR="${CERTBOT_WORK_DIR:-/var/lib/letsencrypt}"
  CERTBOT_LOGS_DIR="${CERTBOT_LOGS_DIR:-/var/log/letsencrypt}"
  CERTBOT_RENEWAL_HOOKS_DIR="${CERTBOT_RENEWAL_HOOKS_DIR:-${CERTBOT_CONFIG_DIR}/renewal-hooks/deploy}"
  CERTBOT_RENEW_HOOK_PATH="${CERTBOT_RENEW_HOOK_PATH:-${CERTBOT_RENEWAL_HOOKS_DIR}/ai-customer-platform-nginx-reload}"
  CERTBOT_CERTIFICATE_DIR="${CERTBOT_CERTIFICATE_DIR:-${CERTBOT_CONFIG_DIR}/live/${DOMAIN}}"
  CERTBOT_FULLCHAIN_PATH="${CERTBOT_FULLCHAIN_PATH:-${CERTBOT_CERTIFICATE_DIR}/fullchain.pem}"
  CERTBOT_PRIVATE_KEY_PATH="${CERTBOT_PRIVATE_KEY_PATH:-${CERTBOT_CERTIFICATE_DIR}/privkey.pem}"
  CERTBOT_RENEW_DRY_RUN="${CERTBOT_RENEW_DRY_RUN:-true}"
  CERTBOT_RENEWAL_TIMER="${CERTBOT_RENEWAL_TIMER:-certbot.timer}"

  export CERTBOT_CONFIG_DIR CERTBOT_WORK_DIR CERTBOT_LOGS_DIR
}

domain_validate_config() {
  validation_require_keys \
    DOMAIN \
    LETSENCRYPT_EMAIL \
    DOMAIN_CERTIFICATE_PROVIDER \
    DOMAIN_CHALLENGE_TYPE \
    DOMAIN_DNS_CHECK \
    DOMAIN_HTTPS_PORT \
    CERTBOT_RENEW_DRY_RUN \
    CERTBOT_RENEWAL_TIMER

  if ! validation_is_hostname "${DOMAIN}" || [[ "${DOMAIN}" =~ ^[0-9.]+$ ]]; then
    log_error "DOMAIN must be a valid fully qualified domain name."
    return 1
  fi

  if ! validation_is_email "${LETSENCRYPT_EMAIL}"; then
    log_error "LETSENCRYPT_EMAIL must be a valid email address."
    return 1
  fi

  if [[ "${DOMAIN_CERTIFICATE_PROVIDER}" != "letsencrypt" ]]; then
    log_error "Domain V1 supports only DOMAIN_CERTIFICATE_PROVIDER=letsencrypt."
    return 1
  fi

  if [[ "${DOMAIN_CHALLENGE_TYPE}" != "http-01" ]]; then
    log_error "Domain V1 supports only DOMAIN_CHALLENGE_TYPE=http-01."
    return 1
  fi

  if ! validation_is_boolean "${DOMAIN_DNS_CHECK}" ||
    ! validation_is_boolean "${CERTBOT_RENEW_DRY_RUN}"; then
    log_error "DOMAIN_DNS_CHECK and CERTBOT_RENEW_DRY_RUN must be true or false."
    return 1
  fi

  if ! validation_is_port "${DOMAIN_HTTPS_PORT}"; then
    log_error "DOMAIN_HTTPS_PORT must be between 1 and 65535."
    return 1
  fi

  if [[ ! "${CERTBOT_RENEWAL_TIMER}" =~ ^[A-Za-z0-9@._-]+\.timer$ ]]; then
    log_error "CERTBOT_RENEWAL_TIMER must be a valid systemd timer unit name."
    return 1
  fi

  if [[ "${NGINX_LISTEN_PORT}" != "80" ]]; then
    log_error "The Let's Encrypt HTTP-01 challenge requires NGINX_LISTEN_PORT=80."
    return 1
  fi

  if [[ "${DOMAIN_HTTPS_PORT}" == "${NGINX_LISTEN_PORT}" ]]; then
    log_error "HTTP and HTTPS ports must be different."
    return 1
  fi

  if [[ "${CERTBOT_CONFIG_DIR}" != /* ]] ||
    [[ "${CERTBOT_WORK_DIR}" != /* ]] ||
    [[ "${CERTBOT_LOGS_DIR}" != /* ]]; then
    log_error "Certbot configuration, work, and log directories must be absolute paths."
    return 1
  fi

  if ! file_is_readable "${DOMAIN_HTTPS_TEMPLATE_FILE}" ||
    ! file_is_readable "${DOMAIN_RENEW_HOOK_TEMPLATE_FILE}"; then
    log_error "Domain Nginx or renewal-hook template is unavailable."
    return 1
  fi
}

domain_resolve_addresses() {
  getent ahosts "${DOMAIN}" 2>/dev/null | awk '{print $1}' | sort -u
}

domain_check() {
  local resolved_addresses

  domain_initialize_config
  log_info "[domain] Checking single-domain prerequisites."
  domain_validate_config

  if [[ "${DOMAIN_DNS_CHECK}" == "false" ]]; then
    log_warn "[domain] DNS resolution check is disabled by configuration."
    return 0
  fi

  if ! command_exists getent; then
    log_error "Domain detection requires the getent command."
    return 1
  fi

  resolved_addresses="$(domain_resolve_addresses)"
  if [[ -z "${resolved_addresses}" ]]; then
    log_error "Domain does not resolve through the system resolver: ${DOMAIN}"
    return 1
  fi

  log_ok "[domain] ${DOMAIN} resolves to: $(tr '\n' ' ' <<<"${resolved_addresses}" | sed 's/[[:space:]]*$//')."
  log_info "[domain] HTTP-01 validation also requires public TCP port 80 to reach this server."
}
