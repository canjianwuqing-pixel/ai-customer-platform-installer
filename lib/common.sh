#!/usr/bin/env bash
set -Eeuo pipefail

log_message() {
  local level="$1"
  shift
  printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${level}" "$*"
}

log_info() {
  log_message "INFO" "$@"
}

log_ok() {
  log_message "OK" "$@"
}

log_warn() {
  log_message "WARN" "$@" >&2
}

log_error() {
  log_message "ERROR" "$@" >&2
}

exit_with_error() {
  local message="${1:-Unexpected installer error.}"
  local exit_code="${2:-1}"

  log_error "${message}"
  exit "${exit_code}"
}

die() {
  exit_with_error "$@"
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

file_is_readable() {
  [[ -f "$1" && -r "$1" ]]
}

run_step() {
  local step_name="$1"
  local function_name="$2"

  log_info "Starting step: ${step_name}"
  "${function_name}"
  log_ok "Finished step: ${step_name}"
}
