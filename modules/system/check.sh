#!/usr/bin/env bash
set -Eeuo pipefail

readonly SYSTEM_MIN_CPU_CORES="${SYSTEM_MIN_CPU_CORES:-2}"
readonly SYSTEM_MIN_MEMORY_MB="${SYSTEM_MIN_MEMORY_MB:-4096}"
readonly SYSTEM_MIN_DISK_MB="${SYSTEM_MIN_DISK_MB:-20480}"
readonly SYSTEM_OS_RELEASE_FILE="${SYSTEM_OS_RELEASE_FILE:-/etc/os-release}"
readonly SYSTEM_DISK_PATH="${SYSTEM_DISK_PATH:-/}"
readonly SYSTEM_NETWORK_CHECK_URL="${SYSTEM_NETWORK_CHECK_URL:-https://download.docker.com/}"
readonly SYSTEM_NETWORK_TIMEOUT_SECONDS="${SYSTEM_NETWORK_TIMEOUT_SECONDS:-10}"

system_read_os_release_value() {
  local requested_key="$1"
  local key
  local value

  while IFS='=' read -r key value; do
    if [[ "${key}" != "${requested_key}" ]]; then
      continue
    fi

    value="${value%$'\r'}"
    value="${value#\"}"
    value="${value%\"}"
    value="${value#\'}"
    value="${value%\'}"
    printf '%s\n' "${value}"
    return 0
  done <"${SYSTEM_OS_RELEASE_FILE}"

  return 1
}

system_check_operating_system() {
  local os_id
  local os_version
  local pretty_name

  if ! file_is_readable "${SYSTEM_OS_RELEASE_FILE}"; then
    log_error "Operating-system metadata is unavailable: ${SYSTEM_OS_RELEASE_FILE}"
    return 1
  fi

  os_id="$(system_read_os_release_value ID || true)"
  os_version="$(system_read_os_release_value VERSION_ID || true)"
  pretty_name="$(system_read_os_release_value PRETTY_NAME || true)"

  if [[ "${os_id}" != "ubuntu" ]]; then
    log_error "Unsupported operating system: ${pretty_name:-${os_id:-unknown}}. Only Ubuntu 22.04 and 24.04 are supported."
    return 1
  fi

  if ! validation_is_supported_ubuntu_version "${os_version}"; then
    log_error "Unsupported Ubuntu version: ${os_version:-unknown}. Only Ubuntu 22.04 and 24.04 are supported."
    return 1
  fi

  log_ok "Operating system: ${pretty_name:-Ubuntu ${os_version}}"
}

system_check_user_permissions() {
  local user_id
  local user_name

  user_id="$(id -u)"
  user_name="$(id -un 2>/dev/null || printf 'unknown')"

  if [[ "${user_id}" == "0" ]]; then
    log_ok "User permissions: running as root (${user_name})."
    return 0
  fi

  if ! command_exists sudo; then
    log_error "User permissions: ${user_name} is not root and sudo is unavailable."
    return 1
  fi

  if sudo -n true >/dev/null 2>&1; then
    log_ok "User permissions: ${user_name} has non-interactive sudo access."
  else
    log_warn "User permissions: ${user_name} is not root; later installation steps may request a sudo password."
  fi
}

system_cpu_core_count() {
  local core_count=""

  if command_exists nproc; then
    core_count="$(nproc 2>/dev/null || true)"
  elif command_exists getconf; then
    core_count="$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)"
  elif file_is_readable /proc/cpuinfo; then
    core_count="$(grep -c '^processor[[:space:]]*:' /proc/cpuinfo 2>/dev/null || true)"
  fi

  if validation_is_positive_integer "${core_count}"; then
    printf '%s\n' "${core_count}"
    return 0
  fi

  return 1
}

system_check_cpu() {
  local core_count

  if ! validation_is_positive_integer "${SYSTEM_MIN_CPU_CORES}"; then
    log_error "CPU check: invalid minimum core setting: ${SYSTEM_MIN_CPU_CORES}"
    return 1
  fi

  if ! core_count="$(system_cpu_core_count)"; then
    log_error "CPU check: unable to determine the number of available cores."
    return 1
  fi

  if ! validation_value_at_least "${core_count}" "${SYSTEM_MIN_CPU_CORES}"; then
    log_error "CPU check: ${core_count} core(s) detected; at least ${SYSTEM_MIN_CPU_CORES} are required."
    return 1
  fi

  log_ok "CPU check: ${core_count} core(s) available."
}

system_memory_total_mb() {
  local key
  local value
  local _unit

  if ! file_is_readable /proc/meminfo; then
    return 1
  fi

  while read -r key value _unit; do
    if [[ "${key}" == "MemTotal:" ]] && validation_is_positive_integer "${value}"; then
      printf '%s\n' "$((value / 1024))"
      return 0
    fi
  done </proc/meminfo

  return 1
}

system_check_memory() {
  local memory_mb

  if ! validation_is_positive_integer "${SYSTEM_MIN_MEMORY_MB}"; then
    log_error "Memory check: invalid minimum memory setting: ${SYSTEM_MIN_MEMORY_MB}"
    return 1
  fi

  if ! memory_mb="$(system_memory_total_mb)"; then
    log_error "Memory check: unable to determine total system memory."
    return 1
  fi

  if ! validation_value_at_least "${memory_mb}" "${SYSTEM_MIN_MEMORY_MB}"; then
    log_error "Memory check: ${memory_mb} MB detected; at least ${SYSTEM_MIN_MEMORY_MB} MB are required."
    return 1
  fi

  log_ok "Memory check: ${memory_mb} MB total."
}

system_disk_available_mb() {
  local disk_path="$1"
  local available_kb

  if [[ ! -e "${disk_path}" ]]; then
    return 1
  fi

  available_kb="$(df -Pk -- "${disk_path}" 2>/dev/null | awk 'NR == 2 { print $4 }')"

  if ! validation_is_positive_integer "${available_kb}"; then
    return 1
  fi

  printf '%s\n' "$((available_kb / 1024))"
}

system_check_disk() {
  local available_mb

  if ! validation_is_positive_integer "${SYSTEM_MIN_DISK_MB}"; then
    log_error "Disk check: invalid minimum disk setting: ${SYSTEM_MIN_DISK_MB}"
    return 1
  fi

  if ! available_mb="$(system_disk_available_mb "${SYSTEM_DISK_PATH}")"; then
    log_error "Disk check: unable to inspect available space at ${SYSTEM_DISK_PATH}."
    return 1
  fi

  if ! validation_value_at_least "${available_mb}" "${SYSTEM_MIN_DISK_MB}"; then
    log_error "Disk check: ${available_mb} MB available at ${SYSTEM_DISK_PATH}; at least ${SYSTEM_MIN_DISK_MB} MB are required."
    return 1
  fi

  log_ok "Disk check: ${available_mb} MB available at ${SYSTEM_DISK_PATH}."
}

system_check_architecture() {
  local architecture

  architecture="$(uname -m 2>/dev/null || true)"
  if [[ -z "${architecture}" ]]; then
    log_error "Architecture check: unable to determine the system architecture."
    return 1
  fi

  if validation_is_common_server_architecture "${architecture}"; then
    log_ok "Architecture check: ${architecture}."
  else
    log_warn "Architecture check: ${architecture} is detected but has not yet been validated for this installer."
  fi
}

system_check_network() {
  local url_without_scheme
  local authority
  local host
  local port

  if ! validation_is_http_url "${SYSTEM_NETWORK_CHECK_URL}"; then
    log_error "Network check URL is invalid: ${SYSTEM_NETWORK_CHECK_URL}"
    return 1
  fi

  if ! validation_is_positive_integer "${SYSTEM_NETWORK_TIMEOUT_SECONDS}"; then
    log_error "Network check timeout is invalid: ${SYSTEM_NETWORK_TIMEOUT_SECONDS}"
    return 1
  fi

  if command_exists curl; then
    if curl --location --silent --fail \
      --max-time "${SYSTEM_NETWORK_TIMEOUT_SECONDS}" \
      --output /dev/null \
      "${SYSTEM_NETWORK_CHECK_URL}"; then
      log_ok "Network check: reached ${SYSTEM_NETWORK_CHECK_URL} using curl."
      return 0
    fi
  elif command_exists wget; then
    if wget --quiet --spider \
      --timeout="${SYSTEM_NETWORK_TIMEOUT_SECONDS}" \
      "${SYSTEM_NETWORK_CHECK_URL}"; then
      log_ok "Network check: reached ${SYSTEM_NETWORK_CHECK_URL} using wget."
      return 0
    fi
  fi

  if command_exists timeout; then
    url_without_scheme="${SYSTEM_NETWORK_CHECK_URL#*://}"
    authority="${url_without_scheme%%/*}"
    host="${authority%%:*}"

    if [[ "${SYSTEM_NETWORK_CHECK_URL}" == https://* ]]; then
      port="443"
    else
      port="80"
    fi

    if [[ "${authority}" == *:* ]]; then
      port="${authority##*:}"
    fi

    if [[ -n "${host}" ]] && validation_is_positive_integer "${port}" &&
      timeout "${SYSTEM_NETWORK_TIMEOUT_SECONDS}" \
        bash -c 'exec 3<>"/dev/tcp/${1}/${2}"' bash "${host}" "${port}" \
        >/dev/null 2>&1; then
      log_ok "Network check: opened a TCP connection to ${host}:${port}."
      return 0
    fi
  fi

  log_error "Network check: unable to reach ${SYSTEM_NETWORK_CHECK_URL}."
  return 1
}

system_check() {
  local failure_count=0

  log_info "[system] Starting read-only environment checks."

  system_check_operating_system || failure_count=$((failure_count + 1))
  system_check_user_permissions || failure_count=$((failure_count + 1))
  system_check_cpu || failure_count=$((failure_count + 1))
  system_check_memory || failure_count=$((failure_count + 1))
  system_check_disk || failure_count=$((failure_count + 1))
  system_check_architecture || failure_count=$((failure_count + 1))
  system_check_network || failure_count=$((failure_count + 1))

  if ((failure_count > 0)); then
    log_error "[system] Environment check failed: ${failure_count} check(s) did not pass."
    return 1
  fi

  log_ok "[system] All environment checks passed."
}
