#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALLER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export INSTALLER_ROOT

source "${INSTALLER_ROOT}/lib/common.sh"
source "${INSTALLER_ROOT}/lib/config.sh"
source "${INSTALLER_ROOT}/lib/validation.sh"
source "${INSTALLER_ROOT}/modules/docker/check.sh"
source "${INSTALLER_ROOT}/modules/chatwoot/configure.sh"
source "${INSTALLER_ROOT}/modules/ai-service/check.sh"

MANAGE_AI_ENABLED="false"
MANAGE_LOG_TAIL="${MANAGE_LOG_TAIL:-200}"
declare -a MANAGE_LOG_PIDS=()

manage_usage() {
  printf '%s\n' \
    'Usage: ./manage.sh COMMAND' \
    '' \
    'Commands:' \
    '  status       Show service status' \
    '  logs         Show recent service logs' \
    '  logs-follow  Follow service logs in real time' \
    '  restart      Restart deployed services' \
    '  stop         Stop deployed services' \
    '  start        Start deployed services' \
    '  health       Check containers, databases, and HTTP endpoints'
}

manage_initialize() {
  CONFIG_FILE="${CONFIG_FILE:-${INSTALLER_ROOT}/.env}"

  config_load_installer_configuration "${CONFIG_FILE}"
  chatwoot_initialize_config
  ai_service_initialize_config

  validation_require_keys \
    CHATWOOT_COMPOSE_PROJECT \
    DATABASE_USER \
    DATABASE_NAME \
    REDIS_PASSWORD \
    CHATWOOT_HTTP_CHECK_URL \
    CHATWOOT_HTTP_TIMEOUT_SECONDS

  if ! file_is_readable "${CHATWOOT_COMPOSE_FILE}"; then
    log_error "Chatwoot Compose file is unavailable: ${CHATWOOT_COMPOSE_FILE}"
    return 1
  fi

  if ! docker_engine_is_available || ! docker_compose_is_available; then
    log_error "Service management requires Docker Engine and the Docker Compose plugin."
    return 1
  fi

  if ! docker_current_user_can_access_daemon &&
    ! current_user_is_root &&
    ! command_exists sudo; then
    log_error "The current user cannot access the Docker daemon and sudo is unavailable."
    return 1
  fi

  if [[ -n "${AI_SERVICE_IMAGE:-}" ]]; then
    validation_require_keys \
      AI_ADMIN_IMAGE \
      AI_SERVICE_COMPOSE_PROJECT \
      AI_SERVICE_HTTP_CHECK_URL \
      AI_ADMIN_HTTP_CHECK_URL \
      AI_SERVICE_HTTP_TIMEOUT_SECONDS

    if ! file_is_readable "${AI_SERVICE_COMPOSE_FILE}"; then
      log_error "AI Service Compose file is unavailable: ${AI_SERVICE_COMPOSE_FILE}"
      return 1
    fi
    MANAGE_AI_ENABLED="true"
  fi
}

manage_ai_skip() {
  log_info "AI Service 未启用，跳过。"
}

manage_check_services() {
  local compose_function="$1"
  shift
  local running_services
  local service_name
  local service_label
  local failed="false"

  if ! running_services="$("${compose_function}" ps --status running --services)"; then
    log_error "Unable to inspect service status."
    return 1
  fi

  while (($# >= 2)); do
    service_name="$1"
    service_label="$2"
    shift 2

    if grep -Fqx -- "${service_name}" <<<"${running_services}"; then
      log_ok "${service_label} running"
    else
      log_error "${service_label} not running"
      failed="true"
    fi
  done

  [[ "${failed}" == "false" ]]
}

manage_chatwoot_status() {
  manage_check_services chatwoot_compose \
    rails "Chatwoot Rails" \
    sidekiq "Chatwoot Sidekiq" \
    postgres "PostgreSQL" \
    redis "Redis"
}

manage_ai_status() {
  manage_check_services ai_service_compose \
    ai-service "AI Service" \
    ai-admin "AI Admin"
}

manage_status() {
  local failed="false"

  manage_chatwoot_status || failed="true"
  if [[ "${MANAGE_AI_ENABLED}" == "true" ]]; then
    manage_ai_status || failed="true"
  else
    manage_ai_skip
  fi

  [[ "${failed}" == "false" ]]
}

manage_logs() {
  log_info "Showing the latest ${MANAGE_LOG_TAIL} Chatwoot log lines."
  chatwoot_compose logs --tail="${MANAGE_LOG_TAIL}" postgres redis rails sidekiq

  if [[ "${MANAGE_AI_ENABLED}" == "true" ]]; then
    log_info "Showing the latest ${MANAGE_LOG_TAIL} AI Service log lines."
    ai_service_compose logs --tail="${MANAGE_LOG_TAIL}" ai-service ai-admin
  else
    manage_ai_skip
  fi
}

manage_cleanup_log_followers() {
  local process_id

  for process_id in "${MANAGE_LOG_PIDS[@]}"; do
    kill "${process_id}" >/dev/null 2>&1 || true
  done
  for process_id in "${MANAGE_LOG_PIDS[@]}"; do
    wait "${process_id}" >/dev/null 2>&1 || true
  done
  MANAGE_LOG_PIDS=()
}

manage_logs_follow() {
  local wait_result=0

  if [[ "${MANAGE_AI_ENABLED}" != "true" ]]; then
    manage_ai_skip
    chatwoot_compose logs --follow postgres redis rails sidekiq
    return $?
  fi

  log_info "Following Chatwoot and AI Service logs. Press Ctrl+C to stop."
  chatwoot_compose logs --follow postgres redis rails sidekiq &
  MANAGE_LOG_PIDS+=("$!")
  ai_service_compose logs --follow ai-service ai-admin &
  MANAGE_LOG_PIDS+=("$!")

  trap manage_cleanup_log_followers INT TERM
  if wait -n "${MANAGE_LOG_PIDS[@]}"; then
    wait_result=0
  else
    wait_result=$?
  fi
  manage_cleanup_log_followers
  trap - INT TERM

  return "${wait_result}"
}

manage_restart() {
  if ! chatwoot_compose restart postgres redis rails sidekiq; then
    log_error "Failed to restart Chatwoot services."
    return 1
  fi
  log_ok "Chatwoot services restarted"

  if [[ "${MANAGE_AI_ENABLED}" == "true" ]]; then
    if ! ai_service_compose restart ai-service ai-admin; then
      log_error "Failed to restart AI Service."
      return 1
    fi
    log_ok "AI Service restarted"
  else
    manage_ai_skip
  fi
}

manage_stop() {
  if [[ "${MANAGE_AI_ENABLED}" == "true" ]]; then
    if ! ai_service_compose stop ai-service ai-admin; then
      log_error "Failed to stop AI Service."
      return 1
    fi
    log_ok "AI Service stopped"
  else
    manage_ai_skip
  fi

  if ! chatwoot_compose stop rails sidekiq postgres redis; then
    log_error "Failed to stop Chatwoot services."
    return 1
  fi
  log_ok "Chatwoot services stopped"
}

manage_start() {
  if ! chatwoot_compose start postgres redis rails sidekiq; then
    log_error "Failed to start Chatwoot services."
    return 1
  fi
  log_ok "Chatwoot services started"

  if [[ "${MANAGE_AI_ENABLED}" == "true" ]]; then
    if ! ai_service_compose start ai-service ai-admin; then
      log_error "Failed to start AI Service."
      return 1
    fi
    log_ok "AI Service started"
  else
    manage_ai_skip
  fi
}

manage_http_healthy() {
  local url="$1"
  local timeout_seconds="$2"

  if command_exists curl; then
    curl --fail --silent --max-time "${timeout_seconds}" --output /dev/null "${url}"
    return $?
  fi
  if command_exists wget; then
    wget --quiet --spider --timeout="${timeout_seconds}" "${url}"
    return $?
  fi

  log_error "HTTP health checks require curl or wget."
  return 1
}

manage_health() {
  local failed="false"

  manage_chatwoot_status || failed="true"

  if chatwoot_compose exec -T postgres \
    pg_isready -U "${DATABASE_USER}" -d "${DATABASE_NAME}" >/dev/null 2>&1; then
    log_ok "PostgreSQL healthy"
  else
    log_error "PostgreSQL unhealthy"
    failed="true"
  fi

  if chatwoot_compose exec -T redis sh -c \
    'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping | grep -q PONG' \
    >/dev/null 2>&1; then
    log_ok "Redis healthy"
  else
    log_error "Redis unhealthy"
    failed="true"
  fi

  if manage_http_healthy "${CHATWOOT_HTTP_CHECK_URL}" "${CHATWOOT_HTTP_TIMEOUT_SECONDS}"; then
    log_ok "Chatwoot Rails healthy"
  else
    log_error "Chatwoot Rails unhealthy"
    failed="true"
  fi

  if [[ "${MANAGE_AI_ENABLED}" == "true" ]]; then
    manage_ai_status || failed="true"

    if manage_http_healthy "${AI_SERVICE_HTTP_CHECK_URL}" "${AI_SERVICE_HTTP_TIMEOUT_SECONDS}"; then
      log_ok "AI Service healthy"
    else
      log_error "AI Service unhealthy"
      failed="true"
    fi

    if manage_http_healthy "${AI_ADMIN_HTTP_CHECK_URL}" "${AI_SERVICE_HTTP_TIMEOUT_SECONDS}"; then
      log_ok "AI Admin healthy"
    else
      log_error "AI Admin unhealthy"
      failed="true"
    fi
  else
    manage_ai_skip
  fi

  [[ "${failed}" == "false" ]]
}

main() {
  local command_name="${1:-}"

  if (($# != 1)); then
    manage_usage >&2
    return 1
  fi

  case "${command_name}" in
    --help | -h | help)
      manage_usage
      return 0
      ;;
    status | logs | logs-follow | restart | stop | start | health) ;;
    *)
      log_error "Unknown management command: ${command_name}"
      manage_usage >&2
      return 1
      ;;
  esac

  manage_initialize

  case "${command_name}" in
    status) manage_status ;;
    logs) manage_logs ;;
    logs-follow) manage_logs_follow ;;
    restart) manage_restart ;;
    stop) manage_stop ;;
    start) manage_start ;;
    health) manage_health ;;
  esac
}

main "$@"
