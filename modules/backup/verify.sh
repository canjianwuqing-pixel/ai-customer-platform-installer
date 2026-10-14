#!/usr/bin/env bash
set -Eeuo pipefail

backup_is_valid_version_id() {
  local backup_id="$1"
  [[ "${backup_id}" =~ ^[0-9]{8}T[0-9]{6}Z(-[0-9]{2})?$ ]]
}

backup_contains_ai() {
  # Before optional stacks were introduced every v1 backup contained AI data.
  [[ "$(read_key_value_file "$1/manifest.env" AI_ENABLED || true)" != false ]]
}

backup_get_version_directory() {
  local backup_id="$1"
  local resolved_root
  local resolved_version

  if ! backup_is_valid_version_id "${backup_id}"; then
    log_error "Invalid backup ID format."
    return 1
  fi

  resolved_root="$(readlink -m -- "${BACKUP_DIR}")"
  resolved_version="$(readlink -m -- "${BACKUP_DIR}/${backup_id}")"
  if [[ "$(dirname -- "${resolved_version}")" != "${resolved_root}" ]]; then
    log_error "Backup ID resolves outside the configured backup directory."
    return 1
  fi

  printf '%s\n' "${resolved_version}"
}

backup_verify_permissions() {
  local backup_directory="$1"
  local mode
  local path

  while IFS= read -r -d '' path; do
    mode="$(stat -c '%a' -- "${path}")"
    if (((8#${mode} & 8#077) != 0)); then
      log_error "Backup permissions expose data to group or other users."
      return 1
    fi
  done < <(find "${backup_directory}" -print0)
}

backup_verify_directory() {
  local backup_directory="$1"
  local expected_id="$2"
  local archive_suffix
  local manifest_id
  local manifest_version
  local required_path
  local -a required_paths=()

  if [[ ! -d "${backup_directory}" ]] || [[ -L "${backup_directory}" ]]; then
    log_error "Backup version directory is missing or unsafe."
    return 1
  fi

  if ! file_is_readable "${backup_directory}/manifest.env" ||
    ! file_is_readable "${backup_directory}/SHA256SUMS"; then
    log_error "Backup manifest or checksum record is missing."
    return 1
  fi

  manifest_version="$(read_key_value_file "${backup_directory}/manifest.env" BACKUP_FORMAT_VERSION)"
  manifest_id="$(read_key_value_file "${backup_directory}/manifest.env" BACKUP_ID)"
  archive_suffix="$(read_key_value_file "${backup_directory}/manifest.env" ARCHIVE_SUFFIX)"

  if [[ "${manifest_version}" != "${BACKUP_FORMAT_VERSION}" ]] ||
    [[ "${manifest_id}" != "${expected_id}" ]] ||
    [[ ! "${archive_suffix}" =~ ^tar(\.gz)?$ ]]; then
    log_error "Backup manifest identity, version, or archive format is invalid."
    return 1
  fi

  required_paths=(
    "data/chatwoot-postgres.dump"
    "data/chatwoot-storage.${archive_suffix}"
    "config/platform.env"
    "config/ssl-record.env"
    "compose/chatwoot.compose.yaml"
    "compose/ai-service.compose.yaml"
    "nginx/site.conf"
  )
  if backup_contains_ai "${backup_directory}"; then
    required_paths+=("data/ai-service-data.${archive_suffix}" "data/ai-admin-data.${archive_suffix}")
  fi

  for required_path in "${required_paths[@]}"; do
    if [[ ! -s "${backup_directory}/${required_path}" ]]; then
      log_error "Backup artifact is missing or empty: ${required_path}"
      return 1
    fi
  done

  if ! (
    cd -- "${backup_directory}"
    sha256sum --check --quiet --strict SHA256SUMS
  ); then
    log_error "Backup checksum verification failed."
    return 1
  fi

  for required_path in "${required_paths[@]}"; do
    [[ "${required_path}" == *.tar || "${required_path}" == *.tar.gz ]] || continue
    if ! tar -tf "${backup_directory}/${required_path}" >/dev/null; then
      log_error "Backup archive validation failed: ${required_path}"
      return 1
    fi
  done

  backup_verify_permissions "${backup_directory}" || return 1
  log_ok "[backup] Backup integrity verified: ${expected_id}."
}

backup_verify_version() {
  local backup_id="$1"
  local backup_directory

  backup_directory="$(backup_get_version_directory "${backup_id}")" || return 1
  backup_verify_directory "${backup_directory}" "${backup_id}"
}

backup_list_versions() {
  local backup_id
  local created_at

  if [[ -z "${BACKUP_DIR:-}" ]] || ! validation_is_safe_absolute_path "${BACKUP_DIR}"; then
    log_error "BACKUP_DIR is not configured with a safe absolute path."
    return 1
  fi

  if [[ ! -d "${BACKUP_DIR}" ]]; then
    log_info "[backup] No backup directory exists yet."
    return 0
  fi

  log_info "[backup] Available backup versions:"
  while IFS= read -r backup_id; do
    backup_is_valid_version_id "${backup_id}" || continue
    [[ -f "${BACKUP_DIR}/${backup_id}/manifest.env" ]] || continue
    created_at="$(read_key_value_file "${BACKUP_DIR}/${backup_id}/manifest.env" CREATED_AT || true)"
    printf '%s\t%s\n' "${backup_id}" "${created_at:-unknown}"
  done < <(find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r)
}
