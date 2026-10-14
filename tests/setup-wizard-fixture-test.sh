#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/setup-wizard-fixture.XXXXXX")"
readonly TEST_CONFIG="${TEST_TEMP_ROOT}/.env"
readonly INSTALL_OUTPUT="${TEST_TEMP_ROOT}/install-output.log"
readonly CHMOD_CALL_LOG="${TEST_TEMP_ROOT}/chmod.log"
readonly DOCKER_CALL_LOG="${TEST_TEMP_ROOT}/docker.log"
readonly NGINX_CALL_LOG="${TEST_TEMP_ROOT}/nginx.log"
readonly DOMAIN_CALL_LOG="${TEST_TEMP_ROOT}/domain.log"
readonly CERTBOT_CALL_LOG="${TEST_TEMP_ROOT}/certbot.log"
readonly PASSWORD_PENDING_FILE="${TEST_TEMP_ROOT}/admin-password-pending"

cleanup() {
  local resolved_parent
  local resolved_temp

  resolved_parent="$(cd -- "${TEST_RUNTIME_PARENT}" && pwd -P)"
  resolved_temp="$(cd -- "${TEST_TEMP_ROOT}" && pwd -P)"
  case "${resolved_temp}" in
    "${resolved_parent}"/setup-wizard-fixture.*) rm -rf -- "${resolved_temp}" ;;
    *) printf 'Refusing to remove unexpected fixture path: %s\n' "${resolved_temp}" >&2 ;;
  esac
}
trap cleanup EXIT

: >"${CHMOD_CALL_LOG}"
: >"${DOCKER_CALL_LOG}"
: >"${NGINX_CALL_LOG}"
: >"${DOMAIN_CALL_LOG}"
: >"${CERTBOT_CALL_LOG}"

export PATH="${TEST_ROOT}/tests/fixtures/secrets/bin:${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_CONFIG}"
export INSTALLER_ADMIN_PASSWORD_PENDING_FILE="${PASSWORD_PENDING_FILE}"
export SECRETS_CHMOD_LOG="${CHMOD_CALL_LOG}"
export DOCKER_FIXTURE_LOG="${DOCKER_CALL_LOG}"
export NGINX_CONFIG_ROOT="${TEST_TEMP_ROOT}/nginx"
export NGINX_FIXTURE_LOG="${NGINX_CALL_LOG}"
export DOMAIN_FIXTURE_LOG="${DOMAIN_CALL_LOG}"
export CERTBOT_FIXTURE_LOG="${CERTBOT_CALL_LOG}"
export CERTBOT_CONFIG_DIR="${TEST_TEMP_ROOT}/letsencrypt"
export CERTBOT_WORK_DIR="${TEST_TEMP_ROOT}/certbot-work"
export CERTBOT_LOGS_DIR="${TEST_TEMP_ROOT}/certbot-logs"
export SYSTEM_OS_RELEASE_FILE="${TEST_ROOT}/tests/fixtures/os-release.ubuntu-24.04"
export SYSTEM_NETWORK_TIMEOUT_SECONDS="2"

printf '%s\n' \
  'chat.example.test' \
  'admin@example.test' |
  "${TEST_ROOT}/install.sh" >"${INSTALL_OUTPUT}" 2>&1

if [[ ! -f "${TEST_CONFIG}" ]]; then
  printf '%s\n' 'The setup wizard did not create .env.' >&2
  exit 1
fi

assert_config_line() {
  local expected_line="$1"
  if ! grep -Fxq -- "${expected_line}" "${TEST_CONFIG}"; then
    printf 'Missing wizard configuration line: %s\n' "${expected_line}" >&2
    exit 1
  fi
}

assert_config_line 'DOMAIN=chat.example.test'
assert_config_line 'EMAIL=admin@example.test'
assert_config_line 'LETSENCRYPT_EMAIL=admin@example.test'
assert_config_line 'CHATWOOT_VERSION=v4.18.0'
assert_config_line 'AI_ADMIN_USERNAME=admin'
assert_config_line 'AI_SERVICE_IMAGE='
assert_config_line 'AI_ADMIN_IMAGE='
assert_config_line 'OPENAI_API_KEY='
assert_config_line 'AI_MODEL='

read_config_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "${TEST_CONFIG}"
}

readonly ADMIN_PASSWORD="$(read_config_value AI_ADMIN_PASSWORD)"
if [[ ! "${ADMIN_PASSWORD}" =~ ^[0-9a-f]{32}$ ]]; then
  printf '%s\n' 'The setup wizard did not generate a secure administrator password.' >&2
  exit 1
fi

for key in \
  DATABASE_PASSWORD \
  REDIS_PASSWORD \
  SECRET_KEY_BASE \
  AI_SERVICE_INTERNAL_TOKEN \
  SERVICE_MANAGEMENT_TOKEN \
  AI_ADMIN_SESSION_SECRET; do
  value="$(read_config_value "${key}")"
  if [[ -z "${value}" ]]; then
    printf 'The setup wizard left a required secret empty: %s\n' "${key}" >&2
    exit 1
  fi
  if grep -Fq -- "${value}" "${INSTALL_OUTPUT}"; then
    printf 'The setup wizard exposed a non-display secret: %s\n' "${key}" >&2
    exit 1
  fi
done

for expected_output in \
  '请输入网站域名：' \
  '请输入管理员邮箱：' \
  'AI Customer Platform 安装完成' \
  'https://chat.example.test' \
  'AI Service 未配置，可后续启用。' \
  '维护命令：' \
  './manage.sh status' \
  './manage.sh logs' \
  './manage.sh restart'; do
  if ! grep -Fq -- "${expected_output}" "${INSTALL_OUTPUT}"; then
    printf 'Missing installation output: %s\n' "${expected_output}" >&2
    exit 1
  fi
done

for forbidden_output in \
  '请输入 AI Service 固定版本镜像：' \
  '请输入 AI 管理后台固定版本镜像：' \
  '请输入 OpenAI API Key：' \
  '请输入 AI 模型名称：' \
  'AI 管理后台：' \
  "${ADMIN_PASSWORD}"; do
  if grep -Fq -- "${forbidden_output}" "${INSTALL_OUTPUT}"; then
    printf 'Unexpected installation output: %s\n' "${forbidden_output}" >&2
    exit 1
  fi
done

if grep -Fq -- 'stacks/ai-service/compose.yaml' "${DOCKER_CALL_LOG}"; then
  printf '%s\n' 'The installer invoked the optional AI Service stack without an image.' >&2
  exit 1
fi

if [[ ! -f "${PASSWORD_PENDING_FILE}" ]]; then
  printf '%s\n' 'The administrator password marker should remain pending until AI Service is enabled.' >&2
  exit 1
fi

export INSTALLER_ROOT="${TEST_ROOT}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/lib/setup.sh"
readonly CONFIG_CHECKSUM_BEFORE_RERUN="$(sha256sum "${TEST_CONFIG}" | awk '{print $1}')"
setup_prepare_configuration </dev/null >/dev/null
readonly CONFIG_CHECKSUM_AFTER_RERUN="$(sha256sum "${TEST_CONFIG}" | awk '{print $1}')"

if [[ "${CONFIG_CHECKSUM_BEFORE_RERUN}" != "${CONFIG_CHECKSUM_AFTER_RERUN}" ]]; then
  printf '%s\n' 'A repeated setup wizard run overwrote existing configuration.' >&2
  exit 1
fi

if ! grep -Fq -- "600 ${TEST_CONFIG}" "${CHMOD_CALL_LOG}"; then
  printf '%s\n' 'The setup wizard did not restrict .env permissions to 0600.' >&2
  exit 1
fi

printf '%s\n' 'Interactive setup wizard optional-AI full-flow fixture test passed.'
