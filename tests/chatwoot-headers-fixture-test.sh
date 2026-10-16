#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "${TEST_ROOT}/runtime/tests"
readonly TEST_TEMP="$(mktemp -d "${TEST_ROOT}/runtime/tests/chatwoot-headers.XXXXXX")"
cleanup() {
  local status=$? resolved
  if ((status)); then printf 'Header fixture retained at %s\n' "${TEST_TEMP}" >&2; return; fi
  resolved="$(readlink -m -- "${TEST_TEMP}")"
  case "${resolved}" in "${TEST_ROOT}"/runtime/tests/chatwoot-headers.*) rm -rf -- "${resolved}" ;; esac
}
trap cleanup EXIT
trap 'printf "Header fixture failed at line %s.\n" "${LINENO}" >&2' ERR
export INSTALLER_ROOT="${TEST_ROOT}" CONFIG_FILE="${TEST_TEMP}/.env"
export NGINX_CONFIG_ROOT="${TEST_TEMP}/nginx" NGINX_FIXTURE_LOG="${TEST_TEMP}/nginx.log"
export CERTBOT_CONFIG_DIR="${TEST_TEMP}/certificates"
export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${CONFIG_FILE}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/modules/nginx/install.sh"
source "${TEST_ROOT}/modules/nginx/configure.sh"
source "${TEST_ROOT}/modules/domain/check.sh"
source "${TEST_ROOT}/modules/domain/ssl.sh"
source "${TEST_ROOT}/modules/upgrade/upgrade.sh"
config_load_installer_configuration "${CONFIG_FILE}" >/dev/null
ADMIN_ENABLED=false
config_ai_enabled() { return 1; }
chatwoot_compose() { printf '%s\n' "$*" >>"${TEST_TEMP}/compose-calls"; }
sync_legacy_enabled_site() {
  # Git Bash can emulate symlinks with copies; model a consistent old site.
  if [[ ! -L "${NGINX_SITE_ENABLED_PATH}" ]]; then
    cp -- "${NGINX_SITE_CONFIG_PATH}" "${NGINX_SITE_ENABLED_PATH}"
  fi
}
readonly ENV_CHECKSUM="$(sha256sum "${CONFIG_FILE}")"
mkdir -p -- "${NGINX_CONFIG_ROOT}"
printf '%s\n' 'fixture main config; never modify' >"${NGINX_CONFIG_ROOT}/nginx.conf"
readonly MAIN_CHECKSUM="$(sha256sum "${NGINX_CONFIG_ROOT}/nginx.conf")"

nginx_configure >"${TEST_TEMP}/install.log"
[[ "$(grep -Fxc 'underscores_in_headers on;' "${NGINX_SITE_CONFIG_PATH}")" == 1 ]]
readonly HTTP_CHECKSUM="$(sha256sum "${NGINX_SITE_CONFIG_PATH}")"
nginx_configure >>"${TEST_TEMP}/install.log"
[[ "$(sha256sum "${NGINX_SITE_CONFIG_PATH}")" == "${HTTP_CHECKSUM}" ]]
domain_initialize_config
domain_activate_https >>"${TEST_TEMP}/install.log"

# An actual pre-fix HTTPS site. Reinstall must only prepend the shared policy:
# certificate paths, all locations, redirects and TLS settings remain intact.
sed '/underscores_in_headers/d; /# Managed Chatwoot API header policy (http context)./d' \
  "${NGINX_SITE_CONFIG_PATH}" >"${TEST_TEMP}/legacy-https.conf"
cp -- "${TEST_TEMP}/legacy-https.conf" "${NGINX_SITE_CONFIG_PATH}"
sync_legacy_enabled_site
nginx_configure >>"${TEST_TEMP}/install.log"
tail -n +5 "${NGINX_SITE_CONFIG_PATH}" | cmp -s - "${TEST_TEMP}/legacy-https.conf"
grep -Fq 'listen 443 ssl;' "${NGINX_SITE_CONFIG_PATH}"
readonly HTTPS_CHECKSUM="$(sha256sum "${NGINX_SITE_CONFIG_PATH}")"
nginx_configure >>"${TEST_TEMP}/install.log"
[[ "$(sha256sum "${NGINX_SITE_CONFIG_PATH}")" == "${HTTPS_CHECKSUM}" ]]

# Upgrade's real post-restart path must migrate Nginx even with Admin disabled.
cp -- "${TEST_TEMP}/legacy-https.conf" "${NGINX_SITE_CONFIG_PATH}"
sync_legacy_enabled_site
upgrade_restart_application_services >>"${TEST_TEMP}/install.log"
[[ "$(grep -Fxc 'underscores_in_headers on;' "${NGINX_SITE_CONFIG_PATH}")" == 1 ]]
tail -n +5 "${NGINX_SITE_CONFIG_PATH}" | cmp -s - "${TEST_TEMP}/legacy-https.conf"

# Old backup candidates share the exact same safe application path.
nginx_apply_candidate "${TEST_TEMP}/legacy-https.conf" >>"${TEST_TEMP}/install.log"
readonly ACTIVE_CHECKSUM="$(sha256sum "${NGINX_SITE_CONFIG_PATH}")"
readonly RELOAD_COUNT="$(grep -Fc 'systemctl reload nginx' "${NGINX_FIXTURE_LOG}")"
cp -- "${TEST_TEMP}/legacy-https.conf" "${TEST_TEMP}/failing.conf"
printf '%s\n' '# changed failing candidate' >>"${TEST_TEMP}/failing.conf"
if NGINX_FIXTURE_TEST_FAILURE=true nginx_apply_candidate "${TEST_TEMP}/failing.conf" >>"${TEST_TEMP}/install.log" 2>&1; then
  printf '%s\n' 'Invalid Nginx candidate was unexpectedly activated.' >&2; exit 1
fi
[[ "$(sha256sum "${NGINX_SITE_CONFIG_PATH}")" == "${ACTIVE_CHECKSUM}" ]]
[[ "$(grep -Fc 'systemctl reload nginx' "${NGINX_FIXTURE_LOG}")" == "${RELOAD_COUNT}" ]]
[[ "$(sha256sum "${CONFIG_FILE}")" == "${ENV_CHECKSUM}" ]]
[[ "$(sha256sum "${NGINX_CONFIG_ROOT}/nginx.conf")" == "${MAIN_CHECKSUM}" ]]
! grep -REq '^[[:space:]]*ignore_invalid_headers[[:space:]]+off' "${TEST_ROOT}/templates/nginx" "${TEST_ROOT}/templates/domain"
printf '%s\n' 'Header migration fixtures passed: HTTP/HTTPS, repeat install, Admin-disabled upgrade, old restore and syntax rollback; credentials untouched.'
export PYTHONPATH="${TEST_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1
"${ADMIN_TEST_PYTHON:-python3}" -m unittest discover -s "${TEST_ROOT}/tests" -p chatwoot_header_proxy_test.py -v
