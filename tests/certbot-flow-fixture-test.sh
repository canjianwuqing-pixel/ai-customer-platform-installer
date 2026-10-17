#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/certbot-flow-fixture.XXXXXX")"
readonly CERTBOT_CALL_LOG="${TEST_TEMP_ROOT}/certbot.log"
readonly CERTBOT_INSTALL_LOG="${TEST_TEMP_ROOT}/certbot-install.log"
readonly NGINX_CALL_LOG="${TEST_TEMP_ROOT}/nginx.log"
readonly SITE_CONFIG="${TEST_TEMP_ROOT}/nginx/sites-available/ai-customer-platform-chatwoot"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/certbot-flow-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

export PATH="${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${NGINX_CALL_LOG}"
export CERTBOT_FIXTURE_LOG="${CERTBOT_CALL_LOG}"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"
export CERTBOT_WORK_DIR="${TEST_TEMP_ROOT}/certbot-work"
export CERTBOT_LOGS_DIR="${TEST_TEMP_ROOT}/certbot-logs"

INSTALLER_ROOT="${TEST_ROOT}"
CONFIG_FILE="${TEST_ROOT}/tests/fixtures/chatwoot.test.env"
CONFIG_LOADED="true"
export INSTALLER_ROOT CONFIG_FILE CONFIG_LOADED

source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/modules/nginx/install.sh"
source "${TEST_ROOT}/modules/nginx/configure.sh"
source "${TEST_ROOT}/modules/domain/check.sh"
source "${TEST_ROOT}/modules/domain/configure.sh"
source "${TEST_ROOT}/modules/domain/ssl.sh"
source "${TEST_ROOT}/modules/domain/verify.sh"

config_load_file "${CONFIG_FILE}"

(
  CERTBOT_FIXTURE_INSTALLED="false"

  command_exists() {
    case "$1" in
      apt-get | sudo) return 0 ;;
      certbot) [[ "${CERTBOT_FIXTURE_INSTALLED}" == "true" ]] ;;
      *) command -v "$1" >/dev/null 2>&1 ;;
    esac
  }

  current_user_is_root() {
    return 1
  }

  domain_certbot_nginx_plugin_available() {
    [[ "${CERTBOT_FIXTURE_INSTALLED}" == "true" ]]
  }

  run_as_root() {
    printf '%s\n' "$*" >>"${CERTBOT_INSTALL_LOG}"
    if [[ "$*" == *'apt-get install -y --no-remove certbot python3-certbot-nginx'* ]]; then
      CERTBOT_FIXTURE_INSTALLED="true"
    fi
    return 0
  }

  domain_install_certbot
)

install_update_line="$(grep -nFx 'apt-get update' "${CERTBOT_INSTALL_LOG}" | cut -d: -f1)"
install_package_line="$(grep -nF 'apt-get install -y --no-remove certbot python3-certbot-nginx' "${CERTBOT_INSTALL_LOG}" | cut -d: -f1)"
if [[ -z "${install_update_line}" || -z "${install_package_line}" ]] || ((install_update_line >= install_package_line)); then
  printf '%s\n' 'Certbot packages were not installed in the expected order.' >&2
  exit 1
fi

nginx_configure
domain_configure
domain_ssl
domain_verify

cp -- "${SITE_CONFIG}" "${TEST_TEMP_ROOT}/https-config.snapshot"
nginx_configure
cmp -s -- "${SITE_CONFIG}" "${TEST_TEMP_ROOT}/https-config.snapshot"
domain_ssl
domain_verify

grep -Eq -- 'certbot .* plugins' "${CERTBOT_CALL_LOG}"
grep -Eq -- 'certbot .* certonly .*--nginx .*--email certificates@example.test .*--cert-name chatwoot.example.test .*--domains chatwoot.example.test' "${CERTBOT_CALL_LOG}"
grep -Eq -- 'certbot .* certificates --cert-name chatwoot.example.test' "${CERTBOT_CALL_LOG}"
grep -Eq -- 'certbot .* renew --dry-run --cert-name chatwoot.example.test' "${CERTBOT_CALL_LOG}"
grep -Fq -- 'systemctl enable --now certbot.timer' "${NGINX_CALL_LOG}"
grep -Fq -- 'systemctl is-enabled --quiet certbot.timer' "${NGINX_CALL_LOG}"
grep -Fq -- 'systemctl is-active --quiet certbot.timer' "${NGINX_CALL_LOG}"

certonly_line="$(grep -nE 'certbot .* certonly ' "${CERTBOT_CALL_LOG}" | head -n 1 | cut -d: -f1)"
renew_line="$(grep -nE 'certbot .* renew --dry-run' "${CERTBOT_CALL_LOG}" | head -n 1 | cut -d: -f1)"
if [[ -z "${certonly_line}" || -z "${renew_line}" ]] || ((certonly_line >= renew_line)); then
  printf '%s\n' 'Certbot renewal test must run after certificate issuance.' >&2
  exit 1
fi

source "${TEST_ROOT}/modules/domain/websites.sh"
cp -- "${SITE_CONFIG}" "${TEST_TEMP_ROOT}/before-alias.snapshot"
website_task issue backup.example.test
website_task issue backup.example.test
cmp -s -- "${SITE_CONFIG}" "${TEST_TEMP_ROOT}/before-alias.snapshot"
grep -Eq -- 'certbot .* certonly --webroot .*--cert-name backup.example.test --domains backup.example.test' "${CERTBOT_CALL_LOG}"
grep -Eq -- 'certbot .* renew --dry-run --cert-name backup.example.test' "${CERTBOT_CALL_LOG}"
if CERTBOT_FIXTURE_RENEW_FAILURE=true website_task issue backup.example.test; then
  printf '%s\n' 'Alias issuance ignored renewal validation failure.' >&2; exit 1
fi
cmp -s -- "${SITE_CONFIG}" "${TEST_TEMP_ROOT}/before-alias.snapshot"
ADMIN_MAINTENANCE_STATE_DIR="${TEST_TEMP_ROOT}/maintenance"
mkdir -p -- "${ADMIN_MAINTENANCE_STATE_DIR}"
printf '%s\n' '{"version":1,"primary":"chatwoot.example.test","domains":[]}' >"$(website_registry_path)"
# Verify the installer dispatches persisted registries to the multi-domain
# renderer instead of replacing them with the legacy single-domain template.
website_domains() { printf '%s\n' "$*" >>"${TEST_TEMP_ROOT}/reconcile.log"; }
domain_activate_https
grep -Fxq reconcile "${TEST_TEMP_ROOT}/reconcile.log"
cmp -s -- "${SITE_CONFIG}" "${TEST_TEMP_ROOT}/before-alias.snapshot"
printf '%s\n' 'Certbot single/alias webroot, repeat issuance and renewal failure fixtures passed.'
