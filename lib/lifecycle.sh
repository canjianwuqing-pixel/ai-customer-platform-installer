#!/usr/bin/env bash
set -Eeuo pipefail

# Configuration migration stays in the existing dotenv model. Work on a
# private candidate in a child process so failure changes neither disk nor context.
config_prepare() {
  local mode="$1" source_file="${2:-${CONFIG_FILE}}" target_file="${3:-${2:-${CONFIG_FILE}}}"
  local candidate result=0
  case "${mode}" in install|upgrade|restore) ;; *) log_error 'Unknown configuration preparation mode.'; return 1 ;; esac
  [[ ! -L "${target_file}" && -d "$(dirname -- "${target_file}")" ]] || return 1
  candidate="$(mktemp "${target_file}.prepare.XXXXXX")" || return 1
  chmod 600 "${candidate}" || return 1
  if [[ -f "${source_file}" && ! -L "${source_file}" ]]; then
    cp -- "${source_file}" "${candidate}" || result=1
  elif [[ "${mode}" == install && ! -e "${source_file}" && ! -L "${source_file}" ]]; then
    cp -- "$(setup_template_file)" "${candidate}" || result=1
  else
    result=1
  fi
  if ((result == 0)); then
    # A separate Bash preserves errexit even when callers guard config_prepare
    # with `if`/`!`. No credentials are passed through command arguments.
    bash "${INSTALLER_ROOT}/install.sh" __prepare-configuration "${mode}" "${candidate}" || result=$?
  fi
  if ((result != 0)); then
    rm -f -- "${candidate}"
    log_error '[config] Preparation failed; the original configuration was not changed.'
    return 1
  fi
  mv -f -- "${candidate}" "${target_file}" || { rm -f -- "${candidate}"; return 1; }
  chmod 600 "${target_file}" || return 1
  config_load_installer_configuration "${target_file}"
  log_ok "[config] ${mode} configuration migrated, validated and saved."
}

config_prepare_candidate() {
  local mode="$1" candidate="$2"
  config_load_installer_configuration "${candidate}"
  config_materialize_missing_defaults "${candidate}" "$(config_images_file)" "$(config_default_file)"
  if [[ "${mode}" == install ]]; then setup_collect_configuration; fi
  config_migrate "${mode}"
  setup_configure_optional_modules
  secrets_ensure
  if [[ "${mode}" == install ]] && command_exists docker && docker_current_user_can_access_daemon; then
    config_verify_deployed_credentials
  fi
  admin_prepare_credentials
  validation_validate_base_config
  chatwoot_validate_config
  if [[ -z "${LETSENCRYPT_EMAIL:-}" ]]; then
    LETSENCRYPT_EMAIL="${EMAIL}"
    config_save_keys "${candidate}" LETSENCRYPT_EMAIL
  fi
  nginx_initialize_config
  nginx_validate_config
  domain_initialize_config
  domain_validate_config
  if config_ai_enabled; then ai_service_validate_config; fi
  if [[ "${ADMIN_ENABLED:-false}" == true ]]; then admin_configure; fi
  services_load
  domains_load
  INSTALLER_CONFIG_VERSION=1
  config_save_keys "${candidate}" INSTALLER_CONFIG_VERSION
}

config_verify_deployed_credentials() {
  set +x
  local id="${1:-}" credentials line key found=0
  if [[ -z "${id}" ]]; then
    id="$(docker_execute ps --all --filter "label=com.docker.compose.project=${CHATWOOT_COMPOSE_PROJECT}" --filter "label=com.docker.compose.service=${BACKUP_CHATWOOT_STORAGE_SERVICE}" --format '{{.ID}}')" || return 1
    [[ -n "${id}" ]] || return 0
  fi
  credentials="$(docker_execute inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${id%%$'\n'*}")" || return 1
  while IFS= read -r line; do
    case "${line%%=*}" in
      POSTGRES_PASSWORD) key=DATABASE_PASSWORD ;;
      REDIS_PASSWORD) key=REDIS_PASSWORD ;;
      SECRET_KEY_BASE) key=SECRET_KEY_BASE ;;
      *) continue ;;
    esac
    [[ -n "${!key:-}" && "${!key}" == "${line#*=}" ]] || {
      log_error "Configured ${key} does not match deployed Rails; recover the original configuration before changing services."; return 1;
    }
    found=$((found + 1))
  done <<<"${credentials}"
  [[ "${found}" == 3 ]] || { log_error 'Cannot validate deployed Chatwoot credentials.'; return 1; }
}

config_migrate() {
  local mode="$1" existing=false key resources
  [[ "${INSTALLER_CONFIG_VERSION:-1}" == 1 ]] || { log_error 'Unsupported installer configuration version.'; return 1; }
  if [[ "${mode}" != install ]]; then
    existing=true
  elif command_exists docker &&
    [[ -z "${DATABASE_PASSWORD:-}" || -z "${REDIS_PASSWORD:-}" || -z "${SECRET_KEY_BASE:-}" ]]; then
    # Use labels, not Compose parsing (which itself needs the missing secrets).
    resources="$(docker_execute volume ls --quiet --filter "label=com.docker.compose.project=${CHATWOOT_COMPOSE_PROJECT}")" || {
      log_error 'Cannot inspect existing data. Start Docker before preparing configuration.'; return 1;
    }
    [[ -z "${resources}" ]] || existing=true
  fi
  if [[ "${existing}" == true ]]; then
    for key in DATABASE_PASSWORD REDIS_PASSWORD SECRET_KEY_BASE; do
      [[ -n "${!key:-}" ]] || { log_error "Existing deployment is missing ${key}; restore its original configuration. It will not be regenerated."; return 1; }
    done
  fi
  if [[ "${mode}" == upgrade ]]; then
    CHATWOOT_VERSION="${UPGRADE_CHATWOOT_VERSION:-${CHATWOOT_VERSION}}"
    AI_SERVICE_IMAGE="${UPGRADE_AI_SERVICE_IMAGE:-${AI_SERVICE_IMAGE:-}}"
    AI_ADMIN_IMAGE="${UPGRADE_AI_ADMIN_IMAGE:-${AI_ADMIN_IMAGE:-}}"
    # A newly enabled unified Admin must not overwrite an old vendor image tag.
    if [[ "${ADMIN_ENABLED:-false}" == true && "${CONFIG_SOURCE_ADMIN_ENABLED:-false}" != true ]]; then
      AI_ADMIN_IMAGE="${UPGRADE_AI_ADMIN_IMAGE:-${ADMIN_DEFAULT_IMAGE}}"
    fi
    config_save_keys "${CONFIG_FILE}" CHATWOOT_VERSION AI_SERVICE_IMAGE AI_ADMIN_IMAGE
  fi
}
