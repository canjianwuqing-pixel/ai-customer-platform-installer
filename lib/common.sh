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

log_warn() {
  log_message "WARN" "$@" >&2
}

log_error() {
  log_message "ERROR" "$@" >&2
}

die() {
  log_error "$@"
  return 1
}

run_step() {
  local step_name="$1"
  local function_name="$2"

  log_info "Starting step: ${step_name}"
  "${function_name}"
  log_info "Finished step: ${step_name}"
}
