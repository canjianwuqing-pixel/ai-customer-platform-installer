#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_RUNTIME_PARENT="${TEST_ROOT}/runtime/tests"
mkdir -p -- "${TEST_RUNTIME_PARENT}"
readonly TEST_TEMP_ROOT="$(mktemp -d "${TEST_RUNTIME_PARENT}/setup-wizard-fixture.XXXXXX")"
readonly TEST_CONFIG="${TEST_TEMP_ROOT}/.env"
readonly TEST_TEMPLATE="${TEST_TEMP_ROOT}/wizard.env.example"
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

sed \
  -e 's/^DOMAIN=.*/DOMAIN=/' \
  -e 's/^EMAIL=.*/EMAIL=/' \
  -e 's/^LETSENCRYPT_EMAIL=.*/LETSENCRYPT_EMAIL=/' \
  -e 's/^DATABASE_PASSWORD=.*/DATABASE_PASSWORD=/' \
  -e 's/^REDIS_PASSWORD=.*/REDIS_PASSWORD=/' \
  -e 's/^SECRET_KEY_BASE=.*/SECRET_KEY_BASE=/' \
  -e 's/^AI_SERVICE_INTERNAL_TOKEN=.*/AI_SERVICE_INTERNAL_TOKEN=/' \
  -e 's/^AI_SERVICE_IMAGE=.*/AI_SERVICE_IMAGE=/' \
  -e 's/^AI_ADMIN_IMAGE=.*/AI_ADMIN_IMAGE=/' \
  -e 's/^OPENAI_API_KEY=.*/OPENAI_API_KEY=/' \
  -e 's/^AI_MODEL=.*/AI_MODEL=/' \
  -e 's/^AI_ADMIN_USERNAME=.*/AI_ADMIN_USERNAME=/' \
  -e 's/^AI_ADMIN_PASSWORD=.*/AI_ADMIN_PASSWORD=/' \
  -e 's/^AI_ADMIN_SESSION_SECRET=.*/AI_ADMIN_SESSION_SECRET=/' \
  "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" >"${TEST_TEMPLATE}"

: >"${CHMOD_CALL_LOG}"
: >"${DOCKER_CALL_LOG}"
: >"${NGINX_CALL_LOG}"
: >"${DOMAIN_CALL_LOG}"
: >"${CERTBOT_CALL_LOG}"

export PATH="${TEST_ROOT}/tests/fixtures/secrets/bin:${TEST_ROOT}/tests/fixtures/docker-ready/bin:${PATH}"
export CONFIG_FILE="${TEST_CONFIG}"
export INSTALLER_ENV_TEMPLATE="${TEST_TEMPLATE}"
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

readonly TEST_OPENAI_API_KEY='fixture-openai-key-not-real-12345'
printf '%s\n' \
  'chat.example.test' \
  'admin@example.test' \
  'registry.example.test/ai-service:1.0.0' \
  'registry.example.test/ai-admin:1.0.0' \
  "${TEST_OPENAI_API_KEY}" \
  'fixture-model-1' |
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
assert_config_line 'AI_ADMIN_USERNAME=admin'
assert_config_line 'AI_SERVICE_IMAGE=registry.example.test/ai-service:1.0.0'
assert_config_line 'AI_ADMIN_IMAGE=registry.example.test/ai-admin:1.0.0'
assert_config_line "OPENAI_API_KEY=${TEST_OPENAI_API_KEY}"
assert_config_line 'AI_MODEL=fixture-model-1'

if grep -Fq -- "${TEST_OPENAI_API_KEY}" "${INSTALL_OUTPUT}"; then
  printf '%s\n' 'The setup wizard exposed the OpenAI API key.' >&2
  exit 1
fi

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
  'https://chat.example.test/ai/admin' \
  '管理员账号：' \
  'admin' \
  "初始密码：${ADMIN_PASSWORD}" \
  '请首次登录后修改密码。'; do
  if ! grep -Fq -- "${expected_output}" "${INSTALL_OUTPUT}"; then
    printf 'Missing installation completion output: %s\n' "${expected_output}" >&2
    exit 1
  fi
done

if [[ "$(grep -Fc -- "${ADMIN_PASSWORD}" "${INSTALL_OUTPUT}")" != "1" ]]; then
  printf '%s\n' 'The initial administrator password was not displayed exactly once.' >&2
  exit 1
fi

if [[ -e "${PASSWORD_PENDING_FILE}" ]]; then
  printf '%s\n' 'The one-time administrator password marker was not consumed.' >&2
  exit 1
fi

export INSTALLER_ROOT="${TEST_ROOT}"
source "${TEST_ROOT}/lib/common.sh"
source "${TEST_ROOT}/lib/config.sh"
source "${TEST_ROOT}/lib/validation.sh"
source "${TEST_ROOT}/lib/setup.sh"
config_load_optional "${TEST_CONFIG}" >/dev/null
readonly CONFIG_CHECKSUM_BEFORE_RERUN="$(sha256sum "${TEST_CONFIG}" | awk '{print $1}')"
setup_prepare_configuration </dev/null >/dev/null
readonly CONFIG_CHECKSUM_AFTER_RERUN="$(sha256sum "${TEST_CONFIG}" | awk '{print $1}')"

if [[ "${CONFIG_CHECKSUM_BEFORE_RERUN}" != "${CONFIG_CHECKSUM_AFTER_RERUN}" ]]; then
  printf '%s\n' 'A repeated setup wizard run overwrote existing configuration.' >&2
  exit 1
fi

readonly SECOND_COMPLETION_OUTPUT="${TEST_TEMP_ROOT}/second-completion.log"
setup_print_completion >"${SECOND_COMPLETION_OUTPUT}"

if grep -Fq -- "${ADMIN_PASSWORD}" "${SECOND_COMPLETION_OUTPUT}"; then
  printf '%s\n' 'The initial administrator password was displayed more than once.' >&2
  exit 1
fi

if ! grep -Fq -- "600 ${TEST_CONFIG}" "${CHMOD_CALL_LOG}"; then
  printf '%s\n' 'The setup wizard did not restrict .env permissions to 0600.' >&2
  exit 1
fi

printf '%s\n' 'Interactive setup wizard full-flow fixture test passed.'
