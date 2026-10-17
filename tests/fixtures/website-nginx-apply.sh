#!/usr/bin/env bash
# Isolated integration adapter: production candidate/rollback functions, real
# Nginx syntax/reload, no system service, root operation or production files.
set -Eeuo pipefail
source "${INSTALLER_ROOT}/lib/common.sh"
source "${INSTALLER_ROOT}/modules/nginx/configure.sh"
run_as_root() { "$@"; }
nginx_test_configuration() {
  "${TEST_NGINX_BINARY}" -p "${TEST_WEBSITE_PREFIX}/" -c "${TEST_WEBSITE_PREFIX}/nginx.conf" -e stderr -t
}
nginx_reload_service() {
  "${TEST_NGINX_BINARY}" -p "${TEST_WEBSITE_PREFIX}/" -c "${TEST_WEBSITE_PREFIX}/nginx.conf" -e stderr -s reload
}
nginx_apply_candidate "$1"
