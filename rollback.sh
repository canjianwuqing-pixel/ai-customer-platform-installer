#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_ROOT}/install.sh" rollback "$@"
