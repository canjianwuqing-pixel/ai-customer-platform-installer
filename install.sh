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
  "modules/ai-service/check.sh"
  "modules/ai-service/configure.sh"
  "modules/ai-service/install.sh"
  "modules/ai-service/verify.sh"
  "modules/nginx/install.sh"
  "modules/nginx/configure.sh"
  "modules/nginx/verify.sh"
  "modules/domain/check.sh"
  "modules/domain/configure.sh"
  "modules/domain/ssl.sh"
  "modules/domain/verify.sh"
  "modules/backup/check.sh"
  "modules/backup/backup.sh"
  "modules/backup/restore.sh"
  "modules/backup/verify.sh"
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
  register_installer_step "ai-service.check" ai_service_check
  register_installer_step "ai-service.configure" ai_service_configure
  register_installer_step "ai-service.install" ai_service_install
  register_installer_step "ai-service.verify" ai_service_verify
  register_installer_step "nginx.install" nginx_install
  register_installer_step "nginx.configure" nginx_configure
  register_installer_step "nginx.verify" nginx_verify
  register_installer_step "domain.check" domain_check
  register_installer_step "domain.configure" domain_configure
  register_installer_step "domain.ssl" domain_ssl
  register_installer_step "domain.verify" domain_verify
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

run_install_command() {
  validation_validate_base_config
  register_core_steps
  run_registered_steps
}

run_backup_command() {
  validation_validate_base_config
  backup_check
  backup_create
}

run_restore_command() {
  local backup_id=""
  local confirmed="false"

  if [[ "${1:-}" == "--list" ]]; then
    backup_initialize_config
    backup_list_versions
    return 0
  fi

  while (($# > 0)); do
    case "$1" in
      --yes) confirmed="true" ;;
      --*) die "Unknown restore option: $1" ;;
      *)
        if [[ -n "${backup_id}" ]]; then
          die "Restore accepts exactly one backup ID."
        fi
        backup_id="$1"
        ;;
    esac
    shift
  done

  [[ -n "${backup_id}" ]] || die "A backup ID is required. Use ./restore.sh --list to view available backups."
  validation_validate_base_config
  backup_check
  backup_restore "${backup_id}" "${confirmed}"
}

main() {
  local command_name="${1:-install}"

  trap 'on_error "${LINENO}" "$?"' ERR

  log_info "AI customer platform installer started."
  config_load_optional "${CONFIG_FILE:-${INSTALLER_ROOT}/.env}"

  if (($# > 0)); then
    shift
  fi

  case "${command_name}" in
    install) run_install_command "$@" ;;
    backup) run_backup_command "$@" ;;
    restore) run_restore_command "$@" ;;
    list-backups)
      backup_initialize_config
      backup_list_versions
      ;;
    *) die "Unknown command '${command_name}'. Expected install, backup, restore, or list-backups." ;;
  esac

  log_ok "Installer workflow completed."
}

main "$@"
