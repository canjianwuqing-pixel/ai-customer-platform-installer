#!/usr/bin/env bash
set -Eeuo pipefail

validation_require_keys() {
  local key
  local -a missing_keys=()

  for key in "$@"; do
    if [[ -z "${!key:-}" ]]; then
      missing_keys+=("${key}")
    fi
  done

  if ((${#missing_keys[@]} > 0)); then
    die "Missing required configuration values: ${missing_keys[*]}"
  fi
}

validation_is_positive_integer() {
  local value="$1"
  [[ "${value}" =~ ^[0-9]+$ && "${value}" -gt 0 ]]
}

validation_value_at_least() {
  local actual="$1"
  local minimum="$2"

  validation_is_positive_integer "${actual}" &&
    validation_is_positive_integer "${minimum}" &&
    ((actual >= minimum))
}

validation_is_supported_ubuntu_version() {
  local version="$1"

  case "${version}" in
    22.04 | 24.04) return 0 ;;
    *) return 1 ;;
  esac
}

validation_is_common_server_architecture() {
  local architecture="$1"

  case "${architecture}" in
    x86_64 | amd64 | aarch64 | arm64) return 0 ;;
    *) return 1 ;;
  esac
}

validation_is_http_url() {
  local url="$1"
  [[ "${url}" =~ ^https?://[^[:space:]]+$ ]]
}

validation_validate_base_config() {
  if [[ "${CONFIG_LOADED}" != "true" ]]; then
    log_warn "Base configuration validation skipped because no .env file is loaded."
    return 0
  fi

  validation_require_keys \
    DOMAIN \
    EMAIL \
    DATABASE_PASSWORD \
    REDIS_PASSWORD \
    SECRET_KEY_BASE \
    CHATWOOT_VERSION

  log_info "Base configuration contains all required keys."
}
