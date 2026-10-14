#!/usr/bin/env bash
set -Eeuo pipefail

readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_PYTHON="${AI_RUNTIME_TEST_PYTHON:-python3}"
export PYTHONPATH="${TEST_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1

"${TEST_PYTHON}" -m unittest discover -s "${TEST_ROOT}/tests" -p management_api_test.py -v
printf '%s\n' 'Service Management API CRUD, permissions, migration, compatibility, and Runtime fixtures passed.'
