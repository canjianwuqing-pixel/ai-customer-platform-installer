#!/usr/bin/env bash
set -Eeuo pipefail

ai_service_verify_running_services() {
  local service_name
  local running_services
  local -a required_services=(ai-service ai-admin)

  if ! running_services="$(ai_service_compose ps --status running --services)"; then
    log_error "Unable to inspect AI Service container status."
    return 1
  fi

  for service_name in "${required_services[@]}"; do
    if ! grep -Fqx -- "${service_name}" <<<"${running_services}"; then
      log_error "AI Service container is not running: ${service_name}"
      return 1
    fi
    log_ok "AI Service container is running: ${service_name}."
  done
}

ai_service_http_request_succeeds() {
  local url="$1"

  if command_exists curl; then
    curl --fail --silent \
      --max-time "${AI_SERVICE_HTTP_TIMEOUT_SECONDS}" \
      --output /dev/null \
      "${url}"
    return $?
  elif command_exists wget; then
    wget --quiet --spider \
      --timeout="${AI_SERVICE_HTTP_TIMEOUT_SECONDS}" \
      "${url}"
    return $?
  fi

  return 127
}

ai_service_wait_for_http() {
  local service_label="$1"
  local url="$2"
  local deadline=$((SECONDS + AI_SERVICE_STARTUP_TIMEOUT_SECONDS))

  log_info "[ai-service] Waiting for the ${service_label} health endpoint."
  until ai_service_http_request_succeeds "${url}"; do
    if ((SECONDS >= deadline)); then
      log_error "${service_label} did not return a successful HTTP response within ${AI_SERVICE_STARTUP_TIMEOUT_SECONDS} seconds."
      return 1
    fi
    sleep "${AI_SERVICE_HEALTHCHECK_INTERVAL_SECONDS}"
  done

  log_ok "${service_label} HTTP health check passed."
}

ai_service_verify() {
  ai_service_initialize_config
  log_info "[ai-service] Verifying the FastAPI and administration services."

  if ! command_exists curl && ! command_exists wget; then
    log_error "AI Service HTTP verification requires curl or wget."
    return 1
  fi

  ai_service_verify_running_services
  ai_service_wait_for_http "FastAPI service" "${AI_SERVICE_HTTP_CHECK_URL}"
  ai_service_wait_for_http "AI administration backend" "${AI_ADMIN_HTTP_CHECK_URL}"

  log_ok "[ai-service] Deployment verification completed successfully."
}
