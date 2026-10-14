#!/usr/bin/env bash
set -Eeuo pipefail

NGINX_SERVICE_NAME="${NGINX_SERVICE_NAME:-}"
NGINX_PACKAGE_NAME="${NGINX_PACKAGE_NAME:-}"
NGINX_CONFIG_ROOT="${NGINX_CONFIG_ROOT:-}"
NGINX_SITES_AVAILABLE_DIR="${NGINX_SITES_AVAILABLE_DIR:-}"
NGINX_SITES_ENABLED_DIR="${NGINX_SITES_ENABLED_DIR:-}"
NGINX_TEMPLATE_FILE="${NGINX_TEMPLATE_FILE:-}"
NGINX_SITE_CONFIG_PATH="${NGINX_SITE_CONFIG_PATH:-}"
NGINX_SITE_ENABLED_PATH="${NGINX_SITE_ENABLED_PATH:-}"
NGINX_LISTEN_PORT="${NGINX_LISTEN_PORT:-}"
NGINX_UPSTREAM_HOST="${NGINX_UPSTREAM_HOST:-}"
NGINX_SITE_NAME="${NGINX_SITE_NAME:-}"
NGINX_CLIENT_MAX_BODY_SIZE="${NGINX_CLIENT_MAX_BODY_SIZE:-}"
NGINX_PROXY_READ_TIMEOUT_SECONDS="${NGINX_PROXY_READ_TIMEOUT_SECONDS:-}"

nginx_initialize_config() {
  NGINX_SERVICE_NAME="${NGINX_SERVICE_NAME:-nginx}"
  NGINX_PACKAGE_NAME="${NGINX_PACKAGE_NAME:-nginx}"
  NGINX_CONFIG_ROOT="${NGINX_CONFIG_ROOT:-/etc/nginx}"
  NGINX_SITES_AVAILABLE_DIR="${NGINX_CONFIG_ROOT}/sites-available"
  NGINX_SITES_ENABLED_DIR="${NGINX_CONFIG_ROOT}/sites-enabled"
  NGINX_TEMPLATE_FILE="${NGINX_TEMPLATE_FILE:-${INSTALLER_ROOT}/templates/nginx/chatwoot.conf.template}"
  NGINX_SITE_CONFIG_PATH="${NGINX_SITES_AVAILABLE_DIR}/${NGINX_SITE_NAME}"
  NGINX_SITE_ENABLED_PATH="${NGINX_SITES_ENABLED_DIR}/${NGINX_SITE_NAME}"
}

nginx_is_installed() {
  command_exists nginx
}

nginx_service_is_active() {
  nginx_initialize_config

  if command_exists systemctl; then
    systemctl is-active --quiet "${NGINX_SERVICE_NAME}"
    return $?
  fi

  if command_exists service; then
    service "${NGINX_SERVICE_NAME}" status >/dev/null 2>&1
    return $?
  fi

  return 1
}

nginx_install() {
  local nginx_version

  nginx_initialize_config
  log_info "[nginx] Checking the Nginx installation."

  if nginx_is_installed; then
    nginx_version="$(nginx -v 2>&1 || true)"
    log_ok "[nginx] Existing Nginx installation will be reused: ${nginx_version:-version unavailable}."
    return 0
  fi

  if ! command_exists apt-get; then
    log_error "Nginx installation requires apt-get on Ubuntu."
    return 1
  fi

  if ! current_user_is_root && ! command_exists sudo; then
    log_error "Nginx installation requires root or sudo privileges."
    return 1
  fi

  log_info "[nginx] Installing the Ubuntu Nginx package."
  if ! run_as_root apt-get update; then
    log_error "Failed to refresh the Ubuntu package index before installing Nginx."
    return 1
  fi

  if ! run_as_root env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-remove "${NGINX_PACKAGE_NAME}"; then
    log_error "Failed to install Nginx from the Ubuntu package repository."
    return 1
  fi

  if ! nginx_is_installed; then
    log_error "Nginx package installation completed, but the nginx command is unavailable."
    return 1
  fi

  nginx_version="$(nginx -v 2>&1 || true)"
  log_ok "[nginx] Nginx installed successfully: ${nginx_version:-version unavailable}."
}
