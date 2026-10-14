#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "${TEST_ROOT}"
umask 077
mkdir -p -- runtime/tests
readonly REPORT_DIR="$(mktemp -d "${TEST_ROOT}/runtime/tests/suite.XXXXXX")"
readonly JOBS="${TEST_JOBS:-1}"
[[ "${JOBS}" =~ ^[1-9][0-9]*$ && "${JOBS}" -le 8 ]] || exit 1
find . -path ./.git -prune -o -path ./runtime -prune -o -name '*.sh' -type f -exec bash -n {} +
for stub in tests/fixtures/*/bin/*; do bash -n "${stub}"; done
declare -a pids=() names=()
failed=0 count=0
wait_batch() {
  local index
  for index in "${!pids[@]}"; do
    if wait "${pids[index]}"; then printf '[OK] %s\n' "${names[index]}";
    else printf '[ERROR] %s (see %s)\n' "${names[index]}" "${REPORT_DIR}/${names[index]}.log"; failed=1; fi
  done
  pids=() names=()
}
for fixture in tests/*-fixture-test.sh; do
  name="$(basename -- "${fixture}")"
  bash "${fixture}" >"${REPORT_DIR}/${name}.log" 2>&1 &
  pids+=("$!") names+=("${name}")
  count=$((count + 1))
  if ((${#pids[@]} >= JOBS)); then wait_batch; fi
done
wait_batch
"${ADMIN_TEST_PYTHON:-python3}" tests/admin_compose_check.py >"${REPORT_DIR}/compose.log" 2>&1 || failed=1
printf 'Shell syntax and %s fixture suites completed. Reports: %s\n' "${count}" "${REPORT_DIR}"
exit "${failed}"
