#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p -- "${TEST_ROOT}/runtime/tests"
readonly TEST_TEMP="$(mktemp -d "${TEST_ROOT}/runtime/tests/admin-recovery-command.XXXXXX")"
cleanup() {
  local resolved
  resolved="$(cd -- "${TEST_TEMP}" && pwd -P)"
  case "${resolved}" in "${TEST_ROOT}"/runtime/tests/admin-recovery-command.*) rm -rf -- "${resolved}" ;; esac
}
trap cleanup EXIT
mkdir -p -- "${TEST_TEMP}/bin"
cp -- "${TEST_ROOT}/tests/fixtures/admin-recovery/bin/"* "${TEST_TEMP}/bin/"
chmod +x "${TEST_TEMP}/bin/"*
cp -- "${TEST_ROOT}/tests/fixtures/chatwoot.test.env" "${TEST_TEMP}/config.env"
export CONFIG_FILE="${TEST_TEMP}/config.env"
export RECOVERY_COMMAND_LOG="${TEST_TEMP}/command.log"
export PATH="${TEST_TEMP}/bin:${PATH}"
readonly BEFORE="$(sha256sum "${CONFIG_FILE}" | cut -d ' ' -f1)"
# Invoke from outside the project: no dependency on cwd or Chatwoot checks.
cd -- "${TEST_TEMP}"
bash "${TEST_ROOT}/manage.sh" reset-admin-password >"${TEST_TEMP}/output" 2>&1
grep -Fxq -- "${TEST_ROOT}/platform_admin/recovery.py" "${RECOVERY_COMMAND_LOG}"
grep -Fxq -- '--project' "${RECOVERY_COMMAND_LOG}"
grep -Fxq -- 'ai-admin' "${RECOVERY_COMMAND_LOG}"
! grep -Fxq -- '--manual' "${RECOVERY_COMMAND_LOG}"
bash "${TEST_ROOT}/manage.sh" reset-admin-password --manual >"${TEST_TEMP}/output" 2>&1
grep -Fxq -- '--manual' "${RECOVERY_COMMAND_LOG}"
readonly VALID_COMMAND_SUM="$(sha256sum "${RECOVERY_COMMAND_LOG}" | cut -d ' ' -f1)"
for extra in 'must-not-echo-an-argument' '--manual extra'; do
  if bash "${TEST_ROOT}/manage.sh" reset-admin-password "${extra}" >"${TEST_TEMP}/invalid" 2>&1; then exit 1; fi
  ! grep -Fq -- "${extra}" "${TEST_TEMP}/invalid"
done
[[ "$(sha256sum "${RECOVERY_COMMAND_LOG}" | cut -d ' ' -f1)" == "${VALID_COMMAND_SUM}" ]]
[[ "$(sha256sum "${CONFIG_FILE}" | cut -d ' ' -f1)" == "${BEFORE}" ]]
printf '%s\n' 'Recovery command fixtures passed: both modes, strict arguments, no config writes or exported credentials.'
