#!/usr/bin/env bash
set -Eeuo pipefail

domain_verify() {
  domain_initialize_config
  log_info "[domain] Verifying HTTPS and certificate renewal."

  if ! domain_certificate_files_exist; then
    log_error "Domain verification failed: certificate or private key is missing."
    return 1
  fi
  log_ok "[domain] Certificate and private key files exist."

  if [[ ! -f "${NGINX_SITE_CONFIG_PATH}" ]] ||
    ! run_as_root grep -Fq -- "listen ${DOMAIN_HTTPS_PORT} ssl;" "${NGINX_SITE_CONFIG_PATH}" ||
    ! run_as_root grep -Fq -- "server_name ${DOMAIN};" "${NGINX_SITE_CONFIG_PATH}"; then
    log_error "Domain verification failed: the managed HTTPS Nginx site is incomplete."
    return 1
  fi

  if ! run_as_root test -x "${CERTBOT_RENEW_HOOK_PATH}"; then
    log_error "Domain verification failed: the Certbot deploy hook is missing or not executable."
    return 1
  fi

  nginx_test_configuration
  if ! nginx_service_is_active; then
    log_error "Domain verification failed: Nginx is not active."
    return 1
  fi

  if ! domain_certbot certificates --cert-name "${DOMAIN}" >/dev/null; then
    log_error "Domain verification failed: Certbot does not recognize the certificate."
    return 1
  fi

  if ! run_as_root systemctl is-enabled --quiet "${CERTBOT_RENEWAL_TIMER}" ||
    ! run_as_root systemctl is-active --quiet "${CERTBOT_RENEWAL_TIMER}"; then
    log_error "Domain verification failed: ${CERTBOT_RENEWAL_TIMER} is not enabled and active."
    return 1
  fi

  domain_test_renewal
  log_ok "[domain] HTTPS and automatic renewal verification completed successfully."
}
