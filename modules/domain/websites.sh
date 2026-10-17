#!/usr/bin/env bash
set -Eeuo pipefail

website_domains() {
  domain_initialize_config
  nginx_validate_config >/dev/null
  domain_validate_config >/dev/null
  export NGINX_SITE_CONFIG_PATH NGINX_SITE_ENABLED_PATH DOMAIN_HTTPS_TEMPLATE_FILE
  export CERTBOT_FULLCHAIN_PATH CERTBOT_PRIVATE_KEY_PATH
  export WEBSITE_CERTBOT_WEBROOT="${WEBSITE_CERTBOT_WEBROOT:-${CERTBOT_WORK_DIR}/platform-webroot}"
  PYTHONPATH="${INSTALLER_ROOT}" python3 -m platform_admin.site_domains "$@"
}

website_reconcile() {
  # Old single-domain installations are lazily imported, without inventing TLS
  # state before the existing first-install certificate workflow has finished.
  [[ -f "$(website_registry_path)" ]] || return 0
  website_domains reconcile
}

website_task() {
  local action="$1"; shift
  domain_initialize_config
  case "${action}" in
    apply) nginx_apply_candidate "$1" >/dev/null ;;
    alias)
      [[ "${AI_RUNTIME_BUILD:-false}" == true ]] || return 0
      validation_is_hostname "$1" || return 1
      WEBSITE_ACTION=alias WEBSITE_NEW_DOMAIN="$1" WEBSITE_OLD_DOMAIN="${DOMAIN}" \
        PYTHONPATH="${INSTALLER_ROOT}" python3 -m platform_admin.site_origin >/dev/null
      ;;
    retire)
      [[ "${AI_RUNTIME_BUILD:-false}" == true ]] || return 0
      validation_is_hostname "$1" && validation_is_hostname "$2" || return 1
      WEBSITE_ACTION=retire WEBSITE_OLD_DOMAIN="$1" WEBSITE_NEW_DOMAIN="$2" \
        PYTHONPATH="${INSTALLER_ROOT}" python3 -m platform_admin.site_origin >/dev/null
      ;;
    issue)
      validation_is_hostname "$1" || return 1
      local webroot="${WEBSITE_CERTBOT_WEBROOT:-${CERTBOT_WORK_DIR}/platform-webroot}"
      run_as_root install -d -m 0755 "${webroot}"
      domain_certbot certonly --webroot --webroot-path "${webroot}" --non-interactive --agree-tos \
        --no-eff-email --keep-until-expiring --email "${LETSENCRYPT_EMAIL}" --cert-name "$1" --domains "$1" >/dev/null
      domain_install_renew_hook >/dev/null
      domain_enable_automatic_renewal >/dev/null
      if [[ "${CERTBOT_RENEW_DRY_RUN}" == true ]]; then
        domain_certbot renew --dry-run --cert-name "$1" >/dev/null
      fi
      ;;
    primary)
      validation_is_hostname "$1" && validation_is_hostname "$2" || return 1
      local previous="${DOMAIN}" target="$1" old_origin="$2" result=0
      local original_chain="${CERTBOT_FULLCHAIN_PATH}" original_key="${CERTBOT_PRIVATE_KEY_PATH}" original_dir="${CERTBOT_CERTIFICATE_DIR}"
      DOMAIN="${target}"
      CERTBOT_FULLCHAIN_PATH="$3" CERTBOT_PRIVATE_KEY_PATH="$4" CERTBOT_CERTIFICATE_DIR="$(dirname -- "$3")"
      config_save_keys "${CONFIG_FILE}" DOMAIN CERTBOT_CERTIFICATE_DIR CERTBOT_FULLCHAIN_PATH CERTBOT_PRIVATE_KEY_PATH
      # Recreate only application containers; never down, migrate or recreate DB.
      local -a apps=(); read -r -a apps <<<"${BACKUP_CHATWOOT_APP_SERVICES}"
      chatwoot_compose up -d --no-deps "${apps[@]}" >/dev/null || result=1
      if ((result == 0)); then chatwoot_verify_http >/dev/null || result=1; fi
      if ((result == 0)) && [[ "${AI_RUNTIME_BUILD:-false}" == true ]]; then
        # Token stays inside the existing protected environment, not curl argv.
        WEBSITE_NEW_DOMAIN="${target}" WEBSITE_OLD_DOMAIN="${old_origin}" \
          PYTHONPATH="${INSTALLER_ROOT}" python3 -m platform_admin.site_origin >/dev/null || result=1
      fi
      if ((result != 0)); then
        DOMAIN="${previous}"; config_save_keys "${CONFIG_FILE}" DOMAIN
        CERTBOT_FULLCHAIN_PATH="${original_chain}" CERTBOT_PRIVATE_KEY_PATH="${original_key}" CERTBOT_CERTIFICATE_DIR="${original_dir}"
        config_save_keys "${CONFIG_FILE}" CERTBOT_CERTIFICATE_DIR CERTBOT_FULLCHAIN_PATH CERTBOT_PRIVATE_KEY_PATH
        chatwoot_compose up -d --no-deps "${apps[@]}" >/dev/null || true
        return 1
      fi
      ;;
    *) return 1 ;;
  esac
}
