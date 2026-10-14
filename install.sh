#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALLER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export INSTALLER_ROOT

# Shared libraries are loaded before modules so every module uses the same
# logging, configuration, state, and validation interfaces.
source "${INSTALLER_ROOT}/lib/common.sh"
source "${INSTALLER_ROOT}/lib/config.sh"
source "${INSTALLER_ROOT}/lib/secrets.sh"
source "${INSTALLER_ROOT}/lib/state.sh"
source "${INSTALLER_ROOT}/lib/validation.sh"
source "${INSTALLER_ROOT}/lib/setup.sh"
source "${INSTALLER_ROOT}/lib/records.sh"
source "${INSTALLER_ROOT}/lib/services.sh"
source "${INSTALLER_ROOT}/lib/domains.sh"
source "${INSTALLER_ROOT}/lib/admin.sh"

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
  "modules/admin/check.sh"
  "modules/admin/configure.sh"
  "modules/admin/install.sh"
  "modules/admin/verify.sh"
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
  "modules/upgrade/check.sh"
  "modules/upgrade/verify.sh"
  "modules/upgrade/upgrade.sh"
  "modules/upgrade/rollback.sh"
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
  if [[ "${AI_SERVICE_ENABLED:-false}" == "true" ]]; then
    register_installer_step "ai-service.check" ai_service_check
    register_installer_step "ai-service.configure" ai_service_configure
    register_installer_step "ai-service.install" ai_service_install
    if [[ "${ADMIN_ENABLED:-false}" == true ]]; then
      register_installer_step "admin.install" admin_install
      register_installer_step "admin.verify" admin_verify
    fi
    register_installer_step "ai-service.verify" ai_service_verify
  fi
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
  setup_prepare_configuration
  setup_configure_optional_modules
  # Configuration-only initialization point for the future management core.
  # No domain, service, channel, knowledge source, or model is created here.
  services_load
  domains_load
  secrets_ensure
  admin_prepare_credentials
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

run_upgrade_command() {
  local confirmed="false"

  while (($# > 0)); do
    case "$1" in
      --yes) confirmed="true" ;;
      *) die "Unknown upgrade option: $1" ;;
    esac
    shift
  done

  validation_validate_base_config
  upgrade_execute "${confirmed}"
}

run_rollback_command() {
  local record_id=""
  local confirmed="false"

  if [[ "${1:-}" == "--list" ]]; then
    upgrade_list_records
    return 0
  fi

  while (($# > 0)); do
    case "$1" in
      --yes) confirmed="true" ;;
      --*) die "Unknown rollback option: $1" ;;
      *)
        if [[ -n "${record_id}" ]]; then
          die "Rollback accepts at most one upgrade record ID."
        fi
        record_id="$1"
        ;;
    esac
    shift
  done

  validation_validate_base_config
  upgrade_initialize_config
  if [[ -z "${record_id}" ]]; then
    record_id="$(upgrade_find_latest_completed_record)" ||
      die "No completed upgrade record is available for rollback."
    log_info "[rollback] Selected latest completed upgrade record: ${record_id}."
  fi
  upgrade_rollback "${record_id}" "${confirmed}"
}

main() {
  local command_name="${1:-install}"

  trap 'on_error "${LINENO}" "$?"' ERR

  log_info "AI customer platform installer started."

  if [[ "${command_name}" != "install" ]]; then
    config_load_installer_configuration "${CONFIG_FILE:-${INSTALLER_ROOT}/.env}"
  fi

  if (($# > 0)); then
    shift
  fi

  case "${command_name}" in
    install) run_install_command "$@" ;;
    backup) run_backup_command "$@" ;;
    restore) run_restore_command "$@" ;;
    upgrade) run_upgrade_command "$@" ;;
    rollback) run_rollback_command "$@" ;;
    list-backups)
      backup_initialize_config
      backup_list_versions
      ;;
    list-upgrades) upgrade_list_records ;;
    *) die "Unknown command '${command_name}'. Expected install, backup, restore, upgrade, rollback, list-backups, or list-upgrades." ;;
  esac

  log_ok "Installer workflow completed."
  if [[ "${command_name}" == "install" ]]; then
    setup_print_completion
  fi
}

main "$@"
