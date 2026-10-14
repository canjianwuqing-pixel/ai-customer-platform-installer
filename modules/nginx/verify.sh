#!/usr/bin/env bash
set -Eeuo pipefail

nginx_verify() {
  nginx_initialize_config
  log_info "[nginx] Verifying the reverse-proxy deployment."

  if ! nginx_is_installed; then
    log_error "Nginx verification failed: the nginx command is unavailable."
    return 1
  fi

  if [[ ! -f "${NGINX_SITE_CONFIG_PATH}" ]]; then
    log_error "Nginx verification failed: site configuration is missing: ${NGINX_SITE_CONFIG_PATH}"
    return 1
  fi
  log_ok "Nginx site configuration exists: ${NGINX_SITE_CONFIG_PATH}."

  if [[ ! -e "${NGINX_SITE_ENABLED_PATH}" && ! -L "${NGINX_SITE_ENABLED_PATH}" ]]; then
    log_error "Nginx verification failed: site is not enabled: ${NGINX_SITE_ENABLED_PATH}"
    return 1
  fi
  log_ok "Nginx site is enabled: ${NGINX_SITE_ENABLED_PATH}."

  nginx_test_configuration

  if ! nginx_service_is_active; then
    log_error "Nginx verification failed: the ${NGINX_SERVICE_NAME} service is not active."
    return 1
  fi
  log_ok "Nginx service status: active."

  log_ok "[nginx] Reverse-proxy verification completed successfully."
}
