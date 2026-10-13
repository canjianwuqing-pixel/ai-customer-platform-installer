#!/usr/bin/env bash
set -Eeuo pipefail

state_file_path() {
  printf '%s\n' "${INSTALLER_STATE_FILE:-${INSTALLER_ROOT}/.installer-state}"
}

state_is_complete() {
  local step_name="$1"
  local state_file
  state_file="$(state_file_path)"

  [[ -f "${state_file}" ]] && grep -Fqx -- "${step_name}" "${state_file}"
}

state_mark_complete() {
  local step_name="$1"
  local state_file
  state_file="$(state_file_path)"

  if state_is_complete "${step_name}"; then
    return 0
  fi

  umask 077
  printf '%s\n' "${step_name}" >>"${state_file}"
}
