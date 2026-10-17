#!/usr/bin/env bash
# Isolated integration adapter: production candidate/rollback functions, real
# Nginx syntax/reload, no system service, root operation or production files.
set -Eeuo pipefail
source "${INSTALLER_ROOT}/lib/common.sh"
source "${INSTALLER_ROOT}/modules/nginx/configure.sh"
run_as_root() { "$@"; }
nginx_candidate_event() { printf 'PLATFORM_SITE_EVENT %s %s\n' "$1" "$2"; }
nginx_test_configuration() {
  "${TEST_NGINX_BINARY}" -p "${TEST_WEBSITE_PREFIX}/" -c "${TEST_WEBSITE_PREFIX}/nginx.conf" -e stderr -t
}
nginx_reload_service() {
  # A successful service-manager acknowledgement is not worker readiness.
  [[ "${TEST_WEBSITE_DEFER_RELOAD:-false}" != true ]] || return 0
  "${TEST_NGINX_BINARY}" -p "${TEST_WEBSITE_PREFIX}/" -c "${TEST_WEBSITE_PREFIX}/nginx.conf" -e stderr -s reload
}
nginx_apply_candidate "$1"
