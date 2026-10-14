#!/usr/bin/env bash
set -Eeuo pipefail
set +x
export INSTALLER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${INSTALLER_ROOT}/lib/common.sh"
source "${INSTALLER_ROOT}/lib/config.sh"
config_load_installer_configuration "${CONFIG_FILE:-${INSTALLER_ROOT}/.env}"
cd -- "${INSTALLER_ROOT}"
exec python3 -m platform_admin.maintenance_worker
