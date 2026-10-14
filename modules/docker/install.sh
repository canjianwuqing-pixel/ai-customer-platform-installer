#!/usr/bin/env bash
set -Eeuo pipefail

DOCKER_APT_REPOSITORY_URL="${DOCKER_APT_REPOSITORY_URL:-}"
DOCKER_GPG_KEY_URL="${DOCKER_GPG_KEY_URL:-}"
DOCKER_APT_CHANNEL="${DOCKER_APT_CHANNEL:-}"
DOCKER_VERSION="${DOCKER_VERSION:-}"
DOCKER_APT_KEYRING_PATH="${DOCKER_APT_KEYRING_PATH:-}"
DOCKER_APT_SOURCE_PATH="${DOCKER_APT_SOURCE_PATH:-}"

docker_install_initialize_config() {
  docker_initialize_config
  DOCKER_APT_REPOSITORY_URL="${DOCKER_APT_REPOSITORY_URL:-https://download.docker.com/linux/ubuntu}"
  DOCKER_GPG_KEY_URL="${DOCKER_GPG_KEY_URL:-https://download.docker.com/linux/ubuntu/gpg}"
  DOCKER_APT_CHANNEL="${DOCKER_APT_CHANNEL:-stable}"
  DOCKER_APT_KEYRING_PATH="${DOCKER_APT_KEYRING_PATH:-/etc/apt/keyrings/docker.asc}"
  DOCKER_APT_SOURCE_PATH="${DOCKER_APT_SOURCE_PATH:-/etc/apt/sources.list.d/docker.sources}"
}

docker_validate_install_config() {
  if ! validation_is_http_url "${DOCKER_APT_REPOSITORY_URL}"; then
    log_error "Invalid Docker APT repository URL: ${DOCKER_APT_REPOSITORY_URL}"
    return 1
  fi

  if ! validation_is_http_url "${DOCKER_GPG_KEY_URL}"; then
    log_error "Invalid Docker GPG key URL: ${DOCKER_GPG_KEY_URL}"
    return 1
  fi

  if [[ ! "${DOCKER_APT_CHANNEL}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    log_error "Invalid Docker APT channel: ${DOCKER_APT_CHANNEL}"
    return 1
  fi

  if [[ -n "${DOCKER_VERSION}" && "${DOCKER_VERSION}" =~ [[:space:]] ]]; then
    log_error "Docker package version must not contain whitespace."
    return 1
  fi
}

docker_installed_conflicting_packages() {
  local package_name
  local package_status
  local -a conflicting_packages=(
    docker.io
    docker-compose
    docker-compose-v2
    docker-doc
    docker-buildx
    podman-docker
    containerd
    runc
  )

  for package_name in "${conflicting_packages[@]}"; do
    package_status="$(dpkg-query -W -f='${Status}' "${package_name}" 2>/dev/null || true)"
    if [[ "${package_status}" == "install ok installed" ]]; then
      printf '%s\n' "${package_name}"
    fi
  done
}

docker_assert_no_conflicting_packages() {
  local conflicts
  conflicts="$(docker_installed_conflicting_packages)"

  if [[ -z "${conflicts}" ]]; then
    return 0
  fi

  conflicts="${conflicts//$'\n'/, }"
  log_error "Conflicting Docker packages are installed: ${conflicts}. The installer will not remove or replace them automatically."
  return 1
}

docker_ubuntu_codename() {
  local codename

  codename="$(read_key_value_file /etc/os-release UBUNTU_CODENAME || true)"
  if [[ -z "${codename}" ]]; then
    codename="$(read_key_value_file /etc/os-release VERSION_CODENAME || true)"
  fi

  if [[ -z "${codename}" || ! "${codename}" =~ ^[a-z0-9._-]+$ ]]; then
    return 1
  fi

  printf '%s\n' "${codename}"
}

docker_configure_official_repository() {
  local architecture
  local codename
  local key_temp_file
  local source_temp_file

  if ! command_exists apt-get || ! command_exists dpkg; then
    log_error "Docker installation requires apt-get and dpkg on Ubuntu."
    return 1
  fi

  if ! docker_installation_privileges_are_available; then
    log_error "Docker installation requires root or sudo privileges."
    return 1
  fi

  if ! codename="$(docker_ubuntu_codename)"; then
    log_error "Unable to determine the Ubuntu release codename."
    return 1
  fi

  architecture="$(dpkg --print-architecture 2>/dev/null || true)"
  if [[ -z "${architecture}" || ! "${architecture}" =~ ^[a-z0-9._-]+$ ]]; then
    log_error "Unable to determine the Debian package architecture."
    return 1
  fi

  log_info "[docker] Installing repository prerequisites."
  if ! run_as_root apt-get update; then
    log_error "Failed to refresh the Ubuntu package index."
    return 1
  fi

  if ! run_as_root env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-remove ca-certificates curl; then
    log_error "Failed to install Docker repository prerequisites."
    return 1
  fi

  if ! run_as_root install -m 0755 -d "$(dirname -- "${DOCKER_APT_KEYRING_PATH}")"; then
    log_error "Failed to create the Docker APT keyring directory."
    return 1
  fi

  key_temp_file="$(mktemp)"
  if ! curl --fail --silent --show-error --location \
    "${DOCKER_GPG_KEY_URL}" --output "${key_temp_file}"; then
    rm -f -- "${key_temp_file}"
    log_error "Failed to download Docker's official GPG key."
    return 1
  fi

  if [[ ! -s "${key_temp_file}" ]] ||
    ! run_as_root install -m 0644 "${key_temp_file}" "${DOCKER_APT_KEYRING_PATH}"; then
    rm -f -- "${key_temp_file}"
    log_error "Failed to install Docker's official GPG key."
    return 1
  fi
  rm -f -- "${key_temp_file}"

  if ! run_as_root install -m 0755 -d "$(dirname -- "${DOCKER_APT_SOURCE_PATH}")"; then
    log_error "Failed to create the Docker APT sources directory."
    return 1
  fi

  source_temp_file="$(mktemp)"
  printf '%s\n' \
    'Types: deb' \
    "URIs: ${DOCKER_APT_REPOSITORY_URL}" \
    "Suites: ${codename}" \
    "Components: ${DOCKER_APT_CHANNEL}" \
    "Architectures: ${architecture}" \
    "Signed-By: ${DOCKER_APT_KEYRING_PATH}" >"${source_temp_file}"

  if ! run_as_root install -m 0644 "${source_temp_file}" "${DOCKER_APT_SOURCE_PATH}"; then
    rm -f -- "${source_temp_file}"
    log_error "Failed to install the Docker APT source definition."
    return 1
  fi
  rm -f -- "${source_temp_file}"

  if ! run_as_root apt-get update; then
    log_error "Failed to refresh package metadata from Docker's official repository."
    return 1
  fi

  log_ok "[docker] Docker's official APT repository is configured."
}

docker_install_official_engine_packages() {
  local -a packages=(
    docker-ce
    docker-ce-cli
    containerd.io
    docker-buildx-plugin
    docker-compose-plugin
  )

  if [[ -n "${DOCKER_VERSION}" ]]; then
    packages[0]="docker-ce=${DOCKER_VERSION}"
    packages[1]="docker-ce-cli=${DOCKER_VERSION}"
  fi

  if ! run_as_root env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-remove "${packages[@]}"; then
    log_error "Failed to install Docker Engine packages from the official repository."
    return 1
  fi

  log_ok "[docker] Docker Engine, CLI, containerd, Buildx, and Compose Plugin are installed."
}

docker_install_compose_plugin() {
  if ! run_as_root env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-remove docker-compose-plugin; then
    log_error "Failed to install the Docker Compose Plugin without altering the existing Docker environment."
    return 1
  fi

  log_ok "[docker] Docker Compose Plugin is installed."
}

docker_start_service() {
  docker_initialize_config

  if docker_service_is_active; then
    log_ok "[docker] Docker service is already active."
    return 0
  fi

  if command_exists systemctl; then
    if ! run_as_root systemctl enable --now "${DOCKER_SERVICE_NAME}"; then
      log_error "Failed to enable and start the Docker service."
      return 1
    fi
  elif command_exists service; then
    if ! run_as_root service "${DOCKER_SERVICE_NAME}" start; then
      log_error "Failed to start the Docker service."
      return 1
    fi
  else
    log_error "Neither systemctl nor service is available to start Docker."
    return 1
  fi

  if ! docker_service_is_active; then
    log_error "Docker service did not become active after the start command."
    return 1
  fi

  log_ok "[docker] Docker service is active."
}

docker_install() {
  local engine_was_available="false"

  docker_install_initialize_config

  if docker_engine_is_available; then
    engine_was_available="true"
  fi

  if docker_engine_is_available && docker_compose_is_available && docker_service_is_active; then
    log_ok "[docker] Docker Engine and Compose Plugin are already healthy; installation skipped."
    return 0
  fi

  if [[ "${engine_was_available}" == "false" ]] || ! docker_compose_is_available; then
    docker_validate_install_config
  fi

  if [[ "${engine_was_available}" == "false" ]]; then
    log_info "[docker] Docker Engine is missing; preparing an official installation."
    docker_assert_no_conflicting_packages
    docker_configure_official_repository
    docker_install_official_engine_packages
  elif ! docker_compose_is_available; then
    log_warn "[docker] Preserving the existing Docker Engine and installing only the Compose Plugin."
    docker_configure_official_repository
    docker_install_compose_plugin
  else
    log_info "[docker] Existing Docker packages will be preserved."
  fi

  docker_start_service
}
