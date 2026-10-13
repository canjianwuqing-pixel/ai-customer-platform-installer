#!/usr/bin/env bash
set -Eeuo pipefail

docker_check() {
  log_info "[docker] Check placeholder; Docker was not inspected."
  return 0
}
