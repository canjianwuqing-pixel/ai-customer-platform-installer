#!/usr/bin/env bash
set -Eeuo pipefail

CHATWOOT_COMPOSE_FILE="${CHATWOOT_COMPOSE_FILE:-}"

chatwoot_initialize_config() {
  CHATWOOT_COMPOSE_FILE="${CHATWOOT_COMPOSE_FILE:-${INSTALLER_ROOT}/stacks/chatwoot/compose.yaml}"
}

chatwoot_compose() {
  config_compose "${CHATWOOT_COMPOSE_PROJECT}" \
    --file "${CHATWOOT_COMPOSE_FILE}" \
    "$@"
}

chatwoot_validate_config() {
  validation_require_keys \
    DOMAIN \
    EMAIL \
    DATABASE_NAME \
    DATABASE_USER \
    DATABASE_HOST \
    DATABASE_PASSWORD \
    REDIS_HOST \
    REDIS_PASSWORD \
    SECRET_KEY_BASE \
    CHATWOOT_VERSION \
    POSTGRES_VERSION \
    REDIS_VERSION \
    CHATWOOT_BIND_ADDRESS \
    CHATWOOT_HTTP_PORT \
    CHATWOOT_HTTP_CHECK_URL \
    CHATWOOT_COMPOSE_PROJECT \
    CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS \
    CHATWOOT_HEALTHCHECK_INTERVAL_SECONDS \
    CHATWOOT_HTTP_TIMEOUT_SECONDS

  if ! validation_is_hostname "${DOMAIN}"; then
    log_error "Invalid Chatwoot domain: ${DOMAIN}"
    return 1
  fi

  if ! validation_is_email "${EMAIL}"; then
    log_error "Invalid operational email address: ${EMAIL}"
    return 1
  fi

  if ! validation_is_exact_semver_tag "${CHATWOOT_VERSION}"; then
    log_error "CHATWOOT_VERSION must be an exact version tag, not a moving tag such as latest."
    return 1
  fi

  if ! validation_is_exact_pgvector_tag "${POSTGRES_VERSION}"; then
    log_error "POSTGRES_VERSION must pin an exact pgvector/PostgreSQL image tag."
    return 1
  fi

  if ! validation_is_exact_semver_tag "${REDIS_VERSION}"; then
    log_error "REDIS_VERSION must be an exact version tag, not a moving tag such as latest."
    return 1
  fi

  if ! validation_is_port "${CHATWOOT_HTTP_PORT}"; then
    log_error "CHATWOOT_HTTP_PORT must be between 1 and 65535."
    return 1
  fi

  if ! validation_is_bind_address "${CHATWOOT_BIND_ADDRESS}"; then
    log_error "CHATWOOT_BIND_ADDRESS must be a valid IPv4 address or hostname."
    return 1
  fi

  if ! validation_is_http_url "${CHATWOOT_HTTP_CHECK_URL}"; then
    log_error "CHATWOOT_HTTP_CHECK_URL must be an HTTP or HTTPS URL."
    return 1
  fi

  if ! validation_is_compose_project_name "${CHATWOOT_COMPOSE_PROJECT}"; then
    log_error "CHATWOOT_COMPOSE_PROJECT must use lowercase letters, digits, hyphens, or underscores."
    return 1
  fi

  if ! validation_is_positive_integer "${CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS}" ||
    ! validation_is_positive_integer "${CHATWOOT_HEALTHCHECK_INTERVAL_SECONDS}" ||
    ! validation_is_positive_integer "${CHATWOOT_HTTP_TIMEOUT_SECONDS}"; then
    log_error "Chatwoot timeout and health-check interval values must be positive integers."
    return 1
  fi

  if ((CHATWOOT_HEALTHCHECK_INTERVAL_SECONDS > CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS)); then
    log_error "CHATWOOT_HEALTHCHECK_INTERVAL_SECONDS cannot exceed the dependency timeout."
    return 1
  fi

  if [[ ! "${DATABASE_NAME}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] ||
    [[ ! "${DATABASE_USER}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    log_error "DATABASE_NAME and DATABASE_USER must be valid PostgreSQL identifiers."
    return 1
  fi

  if [[ "${DATABASE_HOST}" != "postgres" ]] || [[ "${REDIS_HOST}" != "redis" ]]; then
    log_error "DATABASE_HOST and REDIS_HOST must use the private Compose service names postgres and redis."
    return 1
  fi

  if [[ ${#SECRET_KEY_BASE} -lt 64 ]] || [[ ! "${SECRET_KEY_BASE}" =~ ^[A-Za-z0-9]+$ ]]; then
    log_error "SECRET_KEY_BASE must contain at least 64 alphanumeric characters."
    return 1
  fi
}

chatwoot_configure() {
  chatwoot_initialize_config
  log_info "[chatwoot] Validating deployment configuration."

  if [[ "${CONFIG_LOADED}" != "true" ]] || ! file_is_readable "${CONFIG_FILE}"; then
    log_error "Chatwoot deployment requires a readable .env configuration file."
    return 1
  fi

  if ! file_is_readable "${CHATWOOT_COMPOSE_FILE}"; then
    log_error "Chatwoot Compose file is unavailable: ${CHATWOOT_COMPOSE_FILE}"
    return 1
  fi

  chatwoot_validate_config
  log_ok "[chatwoot] Deployment configuration is valid."
}
