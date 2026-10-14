#!/usr/bin/env bash
set -Eeuo pipefail

domain_configure() {
  domain_initialize_config
  log_info "[domain] Verifying the Nginx binding for ${DOMAIN}."
  domain_validate_config

  if ! nginx_is_installed; then
    log_error "Domain configuration requires Nginx."
    return 1
  fi

  if [[ ! -f "${NGINX_SITE_CONFIG_PATH}" ]]; then
    log_error "Domain configuration requires the managed Nginx site: ${NGINX_SITE_CONFIG_PATH}"
    return 1
  fi

  if ! run_as_root grep -Fq -- "server_name ${DOMAIN};" "${NGINX_SITE_CONFIG_PATH}"; then
    log_error "The managed Nginx site is not bound to DOMAIN=${DOMAIN}."
    return 1
  fi

  if [[ ! -e "${NGINX_SITE_ENABLED_PATH}" && ! -L "${NGINX_SITE_ENABLED_PATH}" ]]; then
    log_error "The managed Nginx site is not enabled: ${NGINX_SITE_ENABLED_PATH}"
    return 1
  fi

  nginx_test_configuration
  log_ok "[domain] Nginx is ready to answer the HTTP-01 challenge for ${DOMAIN}."
}
