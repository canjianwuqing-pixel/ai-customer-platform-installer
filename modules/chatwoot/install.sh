#!/usr/bin/env bash
set -Eeuo pipefail

chatwoot_wait_for_postgres() {
  local deadline=$((SECONDS + CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS))

  log_info "[chatwoot] Waiting for PostgreSQL readiness."
  until chatwoot_compose exec -T postgres \
    pg_isready -U "${DATABASE_USER}" -d "${DATABASE_NAME}" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
      log_error "PostgreSQL did not become ready within ${CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS} seconds."
      return 1
    fi
    sleep "${CHATWOOT_HEALTHCHECK_INTERVAL_SECONDS}"
  done

  log_ok "[chatwoot] PostgreSQL is ready."
}

chatwoot_wait_for_redis() {
  local deadline=$((SECONDS + CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS))

  log_info "[chatwoot] Waiting for Redis readiness."
  until chatwoot_compose exec -T redis sh -c \
    'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping | grep -q PONG' \
    >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
      log_error "Redis did not become ready within ${CHATWOOT_DEPENDENCY_TIMEOUT_SECONDS} seconds."
      return 1
    fi
    sleep "${CHATWOOT_HEALTHCHECK_INTERVAL_SECONDS}"
  done

  log_ok "[chatwoot] Redis is ready."
}

chatwoot_install() {
  chatwoot_initialize_config
  log_info "[chatwoot] Validating the Docker Compose model."
  if ! chatwoot_compose config --quiet; then
    log_error "Chatwoot Compose configuration validation failed."
    return 1
  fi
  log_ok "[chatwoot] Docker Compose configuration is valid."

  log_info "[chatwoot] Pulling fixed-version service images."
  if ! chatwoot_compose pull; then
    log_error "Failed to pull one or more Chatwoot stack images."
    return 1
  fi

  log_info "[chatwoot] Starting PostgreSQL and Redis."
  if ! chatwoot_compose up -d postgres redis; then
    log_error "Failed to start PostgreSQL and Redis."
    return 1
  fi

  chatwoot_wait_for_postgres
  chatwoot_wait_for_redis

  log_info "[chatwoot] Preparing the Chatwoot database."
  if ! chatwoot_compose run --rm rails bundle exec rails db:chatwoot_prepare; then
    log_error "Chatwoot database preparation failed."
    return 1
  fi
  log_ok "[chatwoot] Chatwoot database is prepared."

  log_info "[chatwoot] Starting Rails and Sidekiq."
  if ! chatwoot_compose up -d rails sidekiq; then
    log_error "Failed to start Chatwoot Rails and Sidekiq."
    return 1
  fi

  log_ok "[chatwoot] Chatwoot stack deployment completed."
}
