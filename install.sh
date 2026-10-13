#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALLER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export INSTALLER_ROOT

# Shared libraries are loaded before modules so every module uses the same
# logging, configuration, state, and validation interfaces.
source "${INSTALLER_ROOT}/lib/common.sh"
source "${INSTALLER_ROOT}/lib/config.sh"
source "${INSTALLER_ROOT}/lib/state.sh"
source "${INSTALLER_ROOT}/lib/validation.sh"

readonly -a MODULE_FILES=(
  "modules/system/check.sh"
  "modules/system/prepare.sh"
  "modules/docker/check.sh"
  "modules/docker/install.sh"
  "modules/docker/verify.sh"
  "modules/chatwoot/configure.sh"
  "modules/chatwoot/install.sh"
  "modules/chatwoot/verify.sh"
  "modules/nginx/install.sh"
  "modules/nginx/configure.sh"
  "modules/nginx/verify.sh"
  "modules/ssl/install.sh"
  "modules/ssl/configure.sh"
  "modules/ssl/verify.sh"
)

for module_file in "${MODULE_FILES[@]}"; do
  source "${INSTALLER_ROOT}/${module_file}"
done

declare -a INSTALLER_STEPS=()

register_installer_step() {
  local step_name="$1"
  local function_name="$2"

  if ! declare -F "${function_name}" >/dev/null 2>&1; then
    die "Cannot register step '${step_name}': function '${function_name}' is missing."
  fi

  INSTALLER_STEPS+=("${step_name}|${function_name}")
}

register_core_steps() {
  register_installer_step "system.check" system_check
  register_installer_step "system.prepare" system_prepare
  register_installer_step "docker.check" docker_check
  register_installer_step "docker.install" docker_install
  register_installer_step "docker.verify" docker_verify
  register_installer_step "chatwoot.configure" chatwoot_configure
  register_installer_step "chatwoot.install" chatwoot_install
  register_installer_step "chatwoot.verify" chatwoot_verify
  register_installer_step "nginx.install" nginx_install
  register_installer_step "nginx.configure" nginx_configure
  register_installer_step "nginx.verify" nginx_verify
  register_installer_step "ssl.install" ssl_install
  register_installer_step "ssl.configure" ssl_configure
  register_installer_step "ssl.verify" ssl_verify
}

run_registered_steps() {
  local registered_step
  local step_name
  local function_name

  for registered_step in "${INSTALLER_STEPS[@]}"; do
    IFS='|' read -r step_name function_name <<<"${registered_step}"
    run_step "${step_name}" "${function_name}"
  done
}

on_error() {
  local line_number="$1"
  local exit_code="$2"
  log_error "Installer stopped at line ${line_number} with exit code ${exit_code}."
  exit "${exit_code}"
}

main() {
  trap 'on_error "${LINENO}" "$?"' ERR

  log_info "AI customer platform installer skeleton started."
  config_load_optional "${CONFIG_FILE:-${INSTALLER_ROOT}/.env}"
  validation_validate_base_config

  register_core_steps
  run_registered_steps

  log_info "Project skeleton completed. No software was installed."
}

main "$@"
