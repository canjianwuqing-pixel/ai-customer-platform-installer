#!/usr/bin/env bash
set -Eeuo pipefail

ai_service_validate_secret() {
  local key_name="$1"
  local minimum_length="$2"
  local value="${!key_name:-}"

  if ! validation_has_minimum_length "${value}" "${minimum_length}" ||
    [[ "${value}" == *$'\n'* ]] || [[ "${value}" == *$'\r'* ]]; then
    log_error "${key_name} must contain at least ${minimum_length} characters and no line breaks."
    return 1
  fi
}

ai_service_validate_chatwoot_reservation() {
  local configured_count=0

  [[ -n "${CHATWOOT_WEBHOOK_URL:-}" ]] && configured_count=$((configured_count + 1))
  [[ -n "${CHATWOOT_WEBHOOK_SECRET:-}" ]] && configured_count=$((configured_count + 1))
  [[ -n "${CHATWOOT_INBOX_ID:-}" ]] && configured_count=$((configured_count + 1))

  if ((configured_count == 0)); then
    log_info "[ai-service] Chatwoot webhook integration is reserved but not configured."
    return 0
  fi

  if ((configured_count != 3)); then
    log_error "CHATWOOT_WEBHOOK_URL, CHATWOOT_WEBHOOK_SECRET, and CHATWOOT_INBOX_ID must be configured together."
    return 1
  fi

  if ! validation_is_http_url "${CHATWOOT_WEBHOOK_URL}"; then
    log_error "CHATWOOT_WEBHOOK_URL must be an HTTP or HTTPS URL."
    return 1
  fi

  if ! validation_has_minimum_length "${CHATWOOT_WEBHOOK_SECRET}" 32; then
    log_error "CHATWOOT_WEBHOOK_SECRET must contain at least 32 characters."
    return 1
  fi

  if ! validation_is_positive_integer "${CHATWOOT_INBOX_ID}"; then
    log_error "CHATWOOT_INBOX_ID must be a positive integer."
    return 1
  fi
}

ai_service_validate_config() {
  validation_require_keys \
    AI_SERVICE_IMAGE \
    AI_ADMIN_IMAGE \
    OPENAI_API_KEY \
    AI_MODEL \
    AI_SERVICE_INTERNAL_TOKEN \
    AI_ADMIN_USERNAME \
    AI_ADMIN_PASSWORD \
    AI_ADMIN_SESSION_SECRET \
    AI_SERVICE_BIND_ADDRESS \
    AI_SERVICE_HTTP_PORT \
    AI_SERVICE_CONTAINER_PORT \
    AI_ADMIN_BIND_ADDRESS \
    AI_ADMIN_HTTP_PORT \
    AI_ADMIN_CONTAINER_PORT \
    AI_SERVICE_HTTP_CHECK_URL \
    AI_ADMIN_HTTP_CHECK_URL \
    AI_SERVICE_DATA_PATH \
    AI_ADMIN_DATA_PATH \
    AI_SERVICE_COMPOSE_PROJECT \
    AI_SERVICE_STARTUP_TIMEOUT_SECONDS \
    AI_SERVICE_HEALTHCHECK_INTERVAL_SECONDS \
    AI_SERVICE_HTTP_TIMEOUT_SECONDS \
    AI_LOG_LEVEL \
    AI_LOG_SENSITIVE_DATA \
    AI_LOG_MAX_SIZE \
    AI_LOG_MAX_FILES

  if ! validation_is_pinned_container_image "${AI_SERVICE_IMAGE}" ||
    ! validation_is_pinned_container_image "${AI_ADMIN_IMAGE}"; then
    log_error "AI_SERVICE_IMAGE and AI_ADMIN_IMAGE must use fixed tags or sha256 digests; latest is forbidden."
    return 1
  fi

  if ! ai_service_validate_secret OPENAI_API_KEY 20 ||
    ! ai_service_validate_secret AI_SERVICE_INTERNAL_TOKEN 32 ||
    ! ai_service_validate_secret AI_ADMIN_PASSWORD 16 ||
    ! ai_service_validate_secret AI_ADMIN_SESSION_SECRET 32; then
    return 1
  fi

  if [[ ! "${AI_MODEL}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*$ ]]; then
    log_error "AI_MODEL contains unsupported characters."
    return 1
  fi

  if [[ ! "${AI_ADMIN_USERNAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$ ]]; then
    log_error "AI_ADMIN_USERNAME must contain 3-64 safe account-name characters."
    return 1
  fi

  if [[ -n "${AI_SERVICE_DOMAIN:-}" ]] && ! validation_is_hostname "${AI_SERVICE_DOMAIN}"; then
    log_error "AI_SERVICE_DOMAIN must be empty or a valid fully qualified domain name."
    return 1
  fi

  if ! validation_is_bind_address "${AI_SERVICE_BIND_ADDRESS}" ||
    ! validation_is_bind_address "${AI_ADMIN_BIND_ADDRESS}"; then
    log_error "AI service bind addresses must be valid IPv4 addresses or hostnames."
    return 1
  fi

  if [[ "${AI_SERVICE_BIND_ADDRESS}" != "127.0.0.1" ]] ||
    [[ "${AI_ADMIN_BIND_ADDRESS}" != "127.0.0.1" ]]; then
    log_warn "[ai-service] One or more AI ports are exposed beyond the IPv4 loopback interface."
  fi

  if ! validation_is_port "${AI_SERVICE_HTTP_PORT}" ||
    ! validation_is_port "${AI_SERVICE_CONTAINER_PORT}" ||
    ! validation_is_port "${AI_ADMIN_HTTP_PORT}" ||
    ! validation_is_port "${AI_ADMIN_CONTAINER_PORT}"; then
    log_error "AI service host and container ports must be between 1 and 65535."
    return 1
  fi

  if [[ "${AI_SERVICE_HTTP_PORT}" == "${AI_ADMIN_HTTP_PORT}" ]]; then
    log_error "AI_SERVICE_HTTP_PORT and AI_ADMIN_HTTP_PORT must be different."
    return 1
  fi

  if ! validation_is_http_url "${AI_SERVICE_HTTP_CHECK_URL}" ||
    ! validation_is_http_url "${AI_ADMIN_HTTP_CHECK_URL}"; then
    log_error "AI service health-check URLs must use HTTP or HTTPS."
    return 1
  fi

  if ! validation_is_safe_container_path "${AI_SERVICE_DATA_PATH}" ||
    ! validation_is_safe_container_path "${AI_ADMIN_DATA_PATH}"; then
    log_error "AI service data paths must be safe absolute container paths."
    return 1
  fi

  if ! validation_is_compose_project_name "${AI_SERVICE_COMPOSE_PROJECT}"; then
    log_error "AI_SERVICE_COMPOSE_PROJECT must use lowercase letters, digits, hyphens, or underscores."
    return 1
  fi

  if ! validation_is_positive_integer "${AI_SERVICE_STARTUP_TIMEOUT_SECONDS}" ||
    ! validation_is_positive_integer "${AI_SERVICE_HEALTHCHECK_INTERVAL_SECONDS}" ||
    ! validation_is_positive_integer "${AI_SERVICE_HTTP_TIMEOUT_SECONDS}" ||
    ! validation_is_positive_integer "${AI_LOG_MAX_FILES}"; then
    log_error "AI service timeout, interval, and log retention values must be positive integers."
    return 1
  fi

  if ((AI_SERVICE_HEALTHCHECK_INTERVAL_SECONDS > AI_SERVICE_STARTUP_TIMEOUT_SECONDS)); then
    log_error "AI_SERVICE_HEALTHCHECK_INTERVAL_SECONDS cannot exceed the startup timeout."
    return 1
  fi

  if [[ ! "${AI_LOG_LEVEL}" =~ ^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$ ]]; then
    log_error "AI_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL."
    return 1
  fi

  if [[ "${AI_LOG_SENSITIVE_DATA}" != "false" ]]; then
    log_error "AI_LOG_SENSITIVE_DATA must remain false to prevent secret-bearing application logs."
    return 1
  fi

  if [[ ! "${AI_LOG_MAX_SIZE}" =~ ^[1-9][0-9]*[kKmMgG]$ ]]; then
    log_error "AI_LOG_MAX_SIZE must use a positive size such as 10m."
    return 1
  fi

  ai_service_validate_chatwoot_reservation
}

ai_service_configure() {
  ai_service_initialize_config
  log_info "[ai-service] Validating FastAPI and administration deployment configuration."

  if [[ "${CONFIG_LOADED}" != "true" ]] || ! file_is_readable "${CONFIG_FILE}"; then
    log_error "AI Service deployment requires a loaded .env configuration file."
    return 1
  fi

  if ! file_is_readable "${AI_SERVICE_COMPOSE_FILE}"; then
    log_error "AI Service Compose file is unavailable: ${AI_SERVICE_COMPOSE_FILE}"
    return 1
  fi

  ai_service_validate_config
  log_ok "[ai-service] Deployment configuration is valid; secret values were not logged."
}
