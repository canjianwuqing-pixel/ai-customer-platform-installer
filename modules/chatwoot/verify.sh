#!/usr/bin/env bash
set -Eeuo pipefail

chatwoot_service_is_running() {
  local service_name="$1"
  local running_services="$2"

  grep -Fqx -- "${service_name}" <<<"${running_services}"
}

chatwoot_verify_running_services() {
  local service_name
  local running_services
  local -a required_services=(postgres redis rails sidekiq)

  if ! running_services="$(chatwoot_compose ps --status running --services)"; then
    log_error "Unable to inspect Chatwoot container status."
    return 1
  fi

  for service_name in "${required_services[@]}"; do
    if ! chatwoot_service_is_running "${service_name}" "${running_services}"; then
      log_error "Chatwoot service is not running: ${service_name}"
      return 1
    fi
    log_ok "Chatwoot service is running: ${service_name}."
  done
}

chatwoot_verify_postgres() {
  if ! chatwoot_compose exec -T postgres \
    pg_isready -U "${DATABASE_USER}" -d "${DATABASE_NAME}" >/dev/null 2>&1; then
    log_error "PostgreSQL health verification failed."
    return 1
  fi

  log_ok "PostgreSQL health check passed."
}

chatwoot_verify_redis() {
  if ! chatwoot_compose exec -T redis sh -c \
    'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping | grep -q PONG' \
    >/dev/null 2>&1; then
    log_error "Redis health verification failed."
    return 1
  fi

  log_ok "Redis health check passed."
}

chatwoot_http_request_succeeds() {
  if command_exists curl; then
    curl --fail --silent \
      --max-time "${CHATWOOT_HTTP_TIMEOUT_SECONDS}" \
      --output /dev/null \
      "${CHATWOOT_HTTP_CHECK_URL}"
    return $?
  elif command_exists wget; then
    wget --quiet --spider \
      --timeout="${CHATWOOT_HTTP_TIMEOUT_SECONDS}" \
      "${CHATWOOT_HTTP_CHECK_URL}"
    return $?
  fi

  return 127
}

chatwoot_verify_http() {
  local deadline=$((SECONDS + CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS))

  if ! command_exists curl && ! command_exists wget; then
    log_error "Chatwoot HTTP verification requires curl or wget."
    return 1
  fi

  log_info "[chatwoot] Waiting for the Rails HTTP endpoint."
  until chatwoot_http_request_succeeds; do
    if ((SECONDS >= deadline)); then
      log_error "Chatwoot did not return a successful HTTP response at ${CHATWOOT_HTTP_CHECK_URL} within ${CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS} seconds."
      return 1
    fi
    sleep "${CHATWOOT_HEALTHCHECK_INTERVAL_SECONDS}"
  done

  log_ok "Chatwoot HTTP check passed: ${CHATWOOT_HTTP_CHECK_URL}."
}

chatwoot_verify() {
  chatwoot_initialize_config
  log_info "[chatwoot] Verifying deployed services."

  chatwoot_verify_running_services
  chatwoot_verify_postgres
  chatwoot_verify_redis
  chatwoot_verify_http

  log_ok "[chatwoot] Deployment verification completed successfully."
}
