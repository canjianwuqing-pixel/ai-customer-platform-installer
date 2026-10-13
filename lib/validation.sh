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
