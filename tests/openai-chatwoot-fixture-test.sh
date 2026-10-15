#!/usr/bin/env bash
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${TEST_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1
"${ADMIN_TEST_PYTHON:-python3}" -m unittest discover -s "${TEST_ROOT}/tests" -p openai_chatwoot_test.py -v
"${ADMIN_TEST_PYTHON:-python3}" -m unittest discover -s "${TEST_ROOT}/tests" -p openai_chatwoot_proxy_test.py -v
"${ADMIN_TEST_PYTHON:-python3}" -m unittest discover -s "${TEST_ROOT}/tests" -p openai_chatwoot_browser_test.py -v
