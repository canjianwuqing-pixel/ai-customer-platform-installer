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

current_user_is_root() {
  [[ "${EUID:-$(id -u)}" == "0" ]]
}

run_as_root() {
  if current_user_is_root; then
    "$@"
    return $?
  fi

  if ! command_exists sudo; then
    log_error "Root privileges are required, but sudo is unavailable."
    return 1
  fi

  sudo "$@"
}

read_key_value_file() {
  local file_path="$1"
  local requested_key="$2"
  local key
  local value

  if ! file_is_readable "${file_path}"; then
    return 1
  fi

  while IFS='=' read -r key value; do
    if [[ "${key}" != "${requested_key}" ]]; then
      continue
    fi

    value="${value%$'\r'}"
    value="${value#\"}"
    value="${value%\"}"
    value="${value#\'}"
    value="${value%\'}"
    printf '%s\n' "${value}"
    return 0
  done <"${file_path}"

  return 1
}

run_step() {
  local step_name="$1"
  local function_name="$2"

  log_info "Starting step: ${step_name}"
  "${function_name}"
  log_ok "Finished step: ${step_name}"
}
