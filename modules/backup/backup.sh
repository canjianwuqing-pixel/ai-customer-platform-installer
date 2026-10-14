#!/usr/bin/env bash
set -Eeuo pipefail

declare -a BACKUP_CHATWOOT_RESTART_SERVICES=()
declare -a BACKUP_AI_RESTART_SERVICES=()

backup_prepare_root_directory() {
  local current_gid
  local current_uid

  current_uid="$(id -u)"
  current_gid="$(id -g)"

  if [[ ! -d "${BACKUP_DIR}" ]]; then
    if ! mkdir -p -- "${BACKUP_DIR}" 2>/dev/null; then
      if ! run_as_root mkdir -p -- "${BACKUP_DIR}" ||
        ! run_as_root chown "${current_uid}:${current_gid}" "${BACKUP_DIR}"; then
        log_error "Unable to create the configured backup directory."
        return 1
      fi
    fi
  fi

  if [[ -L "${BACKUP_DIR}" ]] || [[ ! -d "${BACKUP_DIR}" ]]; then
    log_error "BACKUP_DIR must be a real directory, not a symbolic link."
    return 1
  fi

  if [[ ! -w "${BACKUP_DIR}" ]]; then
    log_error "The configured backup directory is not writable by the current user."
    return 1
  fi

  if ! chmod 0700 "${BACKUP_DIR}"; then
    log_error "Unable to restrict the configured backup directory permissions."
    return 1
  fi
}

backup_create_version_id() {
  local base_id
  local candidate_id
  local suffix=0

  base_id="$(date -u '+%Y%m%dT%H%M%SZ')"
  candidate_id="${base_id}"

  while [[ -e "${BACKUP_DIR}/${candidate_id}" ]] ||
    compgen -G "${BACKUP_DIR}/.incomplete.${candidate_id}.*" >/dev/null; do
    suffix=$((suffix + 1))
    printf -v candidate_id '%s-%02d' "${base_id}" "${suffix}"
  done

  printf '%s\n' "${candidate_id}"
}

backup_copy_protected_file() {
  local source_path="$1"
  local destination_path="$2"

  if ! run_as_root cat -- "${source_path}" >"${destination_path}"; then
    log_error "Failed to copy a protected backup source file."
    return 1
  fi
  chmod 0600 "${destination_path}"
}

backup_get_container_id() {
  local compose_function="$1"
  local service_name="$2"
  local container_id

  if ! container_id="$(${compose_function} ps --all --quiet "${service_name}")"; then
    log_error "Unable to inspect the configured Compose service: ${service_name}"
    return 1
  fi

  container_id="$(head -n 1 <<<"${container_id}")"
  if [[ -z "${container_id}" ]]; then
    log_error "No container exists for the configured Compose service: ${service_name}"
    return 1
  fi

  printf '%s\n' "${container_id}"
}

backup_record_running_services() {
  local compose_function="$1"
  local configured_services="$2"
  local destination_name="$3"
  local running_services
  local service_name
  local -a configured=()
  local -n destination="${destination_name}"

  destination=()
  running_services="$(${compose_function} ps --status running --services)" || return 1
  read -r -a configured <<<"${configured_services}"

  for service_name in "${configured[@]}"; do
    if grep -Fqx -- "${service_name}" <<<"${running_services}"; then
      destination+=("${service_name}")
    fi
  done
}

backup_suspend_application_services() {
  local -a chatwoot_services=()
  local -a ai_services=()

  read -r -a chatwoot_services <<<"${BACKUP_CHATWOOT_APP_SERVICES}"
  read -r -a ai_services <<<"${BACKUP_AI_APP_SERVICES}"

  backup_record_running_services \
    chatwoot_compose "${BACKUP_CHATWOOT_APP_SERVICES}" BACKUP_CHATWOOT_RESTART_SERVICES || return 1
  backup_record_running_services \
    ai_service_compose "${BACKUP_AI_APP_SERVICES}" BACKUP_AI_RESTART_SERVICES || return 1

  log_info "[backup] Stopping configured application services for a consistent snapshot."
  if ! chatwoot_compose stop "${chatwoot_services[@]}"; then
    log_error "Failed to stop the configured Chatwoot application services."
    backup_resume_application_services || true
    return 1
  fi

  if ! ai_service_compose stop "${ai_services[@]}"; then
    log_error "Failed to stop the configured AI application services."
    backup_resume_application_services || true
    return 1
  fi
}

backup_resume_application_services() {
  local result=0

  if ((${#BACKUP_CHATWOOT_RESTART_SERVICES[@]} > 0)); then
    chatwoot_compose start "${BACKUP_CHATWOOT_RESTART_SERVICES[@]}" || result=1
  fi

  if ((${#BACKUP_AI_RESTART_SERVICES[@]} > 0)); then
    ai_service_compose start "${BACKUP_AI_RESTART_SERVICES[@]}" || result=1
  fi

  if ((result != 0)); then
    log_error "One or more application services could not be restarted after the backup."
    return 1
  fi

  log_ok "[backup] Previously running application services were restarted."
}

backup_dump_chatwoot_database() {
  local destination_path="$1"
  local compression_level=0

  [[ "${BACKUP_COMPRESS}" == "true" ]] && compression_level=6

  if ! chatwoot_compose exec -T "${BACKUP_CHATWOOT_DATABASE_SERVICE}" \
    pg_dump \
    --username "${DATABASE_USER}" \
    --dbname "${DATABASE_NAME}" \
    --format custom \
    --compress "${compression_level}" >"${destination_path}"; then
    log_error "Chatwoot PostgreSQL backup failed."
    return 1
  fi

  if [[ ! -s "${destination_path}" ]]; then
    log_error "Chatwoot PostgreSQL backup produced an empty dump."
    return 1
  fi
  chmod 0600 "${destination_path}"
}

backup_archive_container_path() {
  local container_id="$1"
  local source_path="$2"
  local destination_path="$3"
  local -a tar_arguments=()

  if [[ "${BACKUP_COMPRESS}" == "true" ]]; then
    tar_arguments=(-czf - .)
  else
    tar_arguments=(-cf - .)
  fi

  if ! docker_execute run --rm \
    --volumes-from "${container_id}:ro" \
    "${BACKUP_TOOL_IMAGE}" \
    tar -C "${source_path}" "${tar_arguments[@]}" >"${destination_path}"; then
    log_error "Failed to archive a configured persistent data path."
    return 1
  fi

  if [[ ! -s "${destination_path}" ]]; then
    log_error "A persistent data archive was empty."
    return 1
  fi
  chmod 0600 "${destination_path}"
}

backup_write_ssl_record() {
  local destination_path="$1"
  local certificate_present="false"

  if run_as_root test -f "${CERTBOT_FULLCHAIN_PATH}" &&
    run_as_root test -f "${CERTBOT_PRIVATE_KEY_PATH}"; then
    certificate_present="true"
  fi

  {
    printf 'DOMAIN=%s\n' "${DOMAIN}"
    printf 'PROVIDER=%s\n' "${DOMAIN_CERTIFICATE_PROVIDER}"
    printf 'CHALLENGE_TYPE=%s\n' "${DOMAIN_CHALLENGE_TYPE}"
    printf 'HTTPS_PORT=%s\n' "${DOMAIN_HTTPS_PORT}"
    printf 'RENEWAL_TIMER=%s\n' "${CERTBOT_RENEWAL_TIMER}"
    printf 'CERTIFICATE_PRESENT=%s\n' "${certificate_present}"
    printf 'PRIVATE_KEY_INCLUDED=false\n'
  } >"${destination_path}" || return 1
  chmod 0600 "${destination_path}" || return 1
}

backup_write_configuration_files() {
  local staging_directory="$1"

  mkdir -p -- \
    "${staging_directory}/config" \
    "${staging_directory}/compose" \
    "${staging_directory}/nginx" || return 1
  chmod 0700 \
    "${staging_directory}/config" \
    "${staging_directory}/compose" \
    "${staging_directory}/nginx" || return 1

  backup_copy_protected_file "${CONFIG_FILE}" "${staging_directory}/config/platform.env" || return 1
  backup_copy_protected_file "${CHATWOOT_COMPOSE_FILE}" "${staging_directory}/compose/chatwoot.compose.yaml" || return 1
  backup_copy_protected_file "${AI_SERVICE_COMPOSE_FILE}" "${staging_directory}/compose/ai-service.compose.yaml" || return 1
  backup_copy_protected_file "${NGINX_SITE_CONFIG_PATH}" "${staging_directory}/nginx/site.conf" || return 1
  backup_write_ssl_record "${staging_directory}/config/ssl-record.env" || return 1
}

backup_write_manifest() {
  local backup_id="$1"
  local staging_directory="$2"
  local archive_suffix="tar"

  [[ "${BACKUP_COMPRESS}" == "true" ]] && archive_suffix="tar.gz"

  {
    printf 'BACKUP_FORMAT_VERSION=%s\n' "${BACKUP_FORMAT_VERSION}"
    printf 'BACKUP_ID=%s\n' "${backup_id}"
    printf 'CREATED_AT=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'COMPRESSED=%s\n' "${BACKUP_COMPRESS}"
    printf 'ARCHIVE_SUFFIX=%s\n' "${archive_suffix}"
    printf 'CHATWOOT_VERSION=%s\n' "${CHATWOOT_VERSION}"
    printf 'POSTGRES_VERSION=%s\n' "${POSTGRES_VERSION}"
    printf 'REDIS_VERSION=%s\n' "${REDIS_VERSION}"
    printf 'AI_SERVICE_IMAGE=%s\n' "${AI_SERVICE_IMAGE}"
    printf 'AI_ADMIN_IMAGE=%s\n' "${AI_ADMIN_IMAGE}"
    printf 'CHATWOOT_COMPOSE_PROJECT=%s\n' "${CHATWOOT_COMPOSE_PROJECT}"
    printf 'AI_SERVICE_COMPOSE_PROJECT=%s\n' "${AI_SERVICE_COMPOSE_PROJECT}"
    printf 'DATABASE_NAME=%s\n' "${DATABASE_NAME}"
    printf 'DATABASE_USER=%s\n' "${DATABASE_USER}"
    printf 'CHATWOOT_DATABASE_SERVICE=%s\n' "${BACKUP_CHATWOOT_DATABASE_SERVICE}"
    printf 'CHATWOOT_STORAGE_SERVICE=%s\n' "${BACKUP_CHATWOOT_STORAGE_SERVICE}"
    printf 'AI_DATA_SERVICE=%s\n' "${BACKUP_AI_DATA_SERVICE}"
    printf 'AI_ADMIN_SERVICE=%s\n' "${BACKUP_AI_ADMIN_SERVICE}"
    printf 'AI_DATA_COVERAGE=database,knowledge-base,application-data\n'
    printf 'ENV_CONTAINS_SECRETS=true\n'
    printf 'SSL_PRIVATE_KEY_INCLUDED=false\n'
  } >"${staging_directory}/manifest.env" || return 1
  chmod 0600 "${staging_directory}/manifest.env" || return 1
}

backup_write_checksums() {
  local staging_directory="$1"

  if ! (
    cd -- "${staging_directory}"
    find . -type f ! -name SHA256SUMS -print0 |
      sort -z |
      xargs -0 sha256sum
  ) >"${staging_directory}/SHA256SUMS"; then
    log_error "Failed to generate the backup checksum record."
    return 1
  fi
  chmod 0600 "${staging_directory}/SHA256SUMS"
}

backup_remove_directory_safely() {
  local target_path="$1"
  local resolved_root
  local resolved_target

  resolved_root="$(readlink -m -- "${BACKUP_DIR}")"
  resolved_target="$(readlink -m -- "${target_path}")"

  if [[ "${resolved_target}" == "${resolved_root}" ]] ||
    [[ "$(dirname -- "${resolved_target}")" != "${resolved_root}" ]]; then
    log_error "Refusing to remove an unexpected backup path."
    return 1
  fi

  rm -rf -- "${resolved_target}"
}

backup_apply_retention() {
  local index
  local version_name
  local -a discovered_versions=()
  local -a versions=()

  mapfile -t discovered_versions < <(
    find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' |
      sort -r
  )

  for version_name in "${discovered_versions[@]}"; do
    if backup_is_valid_version_id "${version_name}" &&
      [[ -f "${BACKUP_DIR}/${version_name}/manifest.env" ]]; then
      versions+=("${version_name}")
    fi
  done

  for ((index = BACKUP_RETENTION; index < ${#versions[@]}; index++)); do
    version_name="${versions[index]}"
    backup_remove_directory_safely "${BACKUP_DIR}/${version_name}" || return 1
    log_info "[backup] Removed expired backup version: ${version_name}."
  done
}

backup_create_payload() {
  local backup_id="$1"
  local staging_directory="$2"
  local storage_container_id="$3"
  local ai_data_container_id="$4"
  local ai_admin_container_id="$5"
  local archive_suffix="tar"

  [[ "${BACKUP_COMPRESS}" == "true" ]] && archive_suffix="tar.gz"

  mkdir -p -- "${staging_directory}/data" || return 1
  chmod 0700 "${staging_directory}/data" || return 1
  backup_dump_chatwoot_database "${staging_directory}/data/chatwoot-postgres.dump" || return 1
  backup_archive_container_path \
    "${storage_container_id}" "${BACKUP_CHATWOOT_STORAGE_PATH}" \
    "${staging_directory}/data/chatwoot-storage.${archive_suffix}" || return 1
  backup_archive_container_path \
    "${ai_data_container_id}" "${AI_SERVICE_DATA_PATH}" \
    "${staging_directory}/data/ai-service-data.${archive_suffix}" || return 1
  backup_archive_container_path \
    "${ai_admin_container_id}" "${AI_ADMIN_DATA_PATH}" \
    "${staging_directory}/data/ai-admin-data.${archive_suffix}" || return 1
  backup_write_configuration_files "${staging_directory}" || return 1
  backup_write_manifest "${backup_id}" "${staging_directory}" || return 1
  backup_write_checksums "${staging_directory}" || return 1
  chmod -R go-rwx "${staging_directory}" || return 1
}

backup_create() {
  local backup_id
  local staging_directory
  local final_directory
  local storage_container_id
  local ai_data_container_id
  local ai_admin_container_id
  local payload_result=0
  local resume_result=0

  backup_initialize_config
  backup_prepare_root_directory
  backup_id="$(backup_create_version_id)"
  staging_directory="${BACKUP_DIR}/.incomplete.${backup_id}.$$"
  final_directory="${BACKUP_DIR}/${backup_id}"

  mkdir -- "${staging_directory}"
  chmod 0700 "${staging_directory}"
  log_info "[backup] Creating backup version ${backup_id}."

  storage_container_id="$(backup_get_container_id chatwoot_compose "${BACKUP_CHATWOOT_STORAGE_SERVICE}")" || {
    backup_remove_directory_safely "${staging_directory}"
    return 1
  }
  ai_data_container_id="$(backup_get_container_id ai_service_compose "${BACKUP_AI_DATA_SERVICE}")" || {
    backup_remove_directory_safely "${staging_directory}"
    return 1
  }
  ai_admin_container_id="$(backup_get_container_id ai_service_compose "${BACKUP_AI_ADMIN_SERVICE}")" || {
    backup_remove_directory_safely "${staging_directory}"
    return 1
  }

  if ! backup_suspend_application_services; then
    backup_remove_directory_safely "${staging_directory}"
    return 1
  fi

  if ! backup_create_payload \
    "${backup_id}" "${staging_directory}" \
    "${storage_container_id}" "${ai_data_container_id}" "${ai_admin_container_id}"; then
    payload_result=1
  fi

  if ! backup_resume_application_services; then
    resume_result=1
  fi

  if ((payload_result != 0)); then
    backup_remove_directory_safely "${staging_directory}"
    log_error "Backup creation failed; the incomplete version was removed."
    return 1
  fi

  if ! backup_verify_directory "${staging_directory}" "${backup_id}"; then
    backup_remove_directory_safely "${staging_directory}"
    log_error "Backup verification failed; the incomplete version was removed."
    return 1
  fi

  if [[ -e "${final_directory}" ]]; then
    backup_remove_directory_safely "${staging_directory}"
    log_error "Backup version already exists; refusing to overwrite it."
    return 1
  fi

  if ! mv -- "${staging_directory}" "${final_directory}"; then
    backup_remove_directory_safely "${staging_directory}"
    log_error "Failed to finalize the verified backup version."
    return 1
  fi
  backup_apply_retention || return 1
  log_ok "[backup] Backup completed: ${backup_id}."

  if ((resume_result != 0)); then
    return 1
  fi
}
