#!/usr/bin/env bash
# Offline, root-only bootstrap contract: never launches real Docker or contacts GitHub.
set -Eeuo pipefail
readonly TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly BOOTSTRAP_SCRIPT="${TEST_ROOT}/one-click-install.sh"

if (( EUID != 0 )); then
  printf 'SKIP one-click installer fixture: root-only bootstrap.\n'
  exit 0
fi
for tool in git stat mktemp sha256sum; do command -v "${tool}" >/dev/null || exit 1; done

scratch="$(mktemp -d)"
trap 'rm -rf -- "${scratch}"' EXIT
mkdir -p "${scratch}/source"
git -C "${scratch}/source" init --initial-branch=master --quiet
git -C "${scratch}/source" config user.name 'One Click Fixture'
git -C "${scratch}/source" config user.email 'fixture@example.test'
printf 'DOMAIN=\nEMAIL=\nADMIN_ENABLED=false\n' >"${scratch}/source/.env.example"
cat > "${scratch}/source/install.sh" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
grep -qx 'ADMIN_ENABLED=true' .env
read -r domain
read -r email
test "$domain" = chat.example.test
test "$email" = admin@example.test
printf 'installed\n' >.fixture-installed
STUB
cat > "${scratch}/source/manage.sh" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
test "$1" = health
grep -qx 'ADMIN_ENABLED=true' .env
printf 'healthy\n' >.fixture-healthy
STUB
( cd "${scratch}/source" && git add . && git commit --quiet -m 'fake clean install repo' )
git clone --bare --quiet "${scratch}/source" "${scratch}/origin.git"

# Redirect the fixed GitHub URL to a private local test repository without
# giving the production bootstrap any alternate-repository parameter.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0="url.file://${scratch}/origin.git.insteadOf"
export GIT_CONFIG_VALUE_0='https://github.com/canjianwuqing-pixel/ai-customer-platform-installer.git'

export AI_CUSTOMER_INSTALL_DIR="${scratch}/fresh"
printf 'chat.example.test\nadmin@example.test\n' | bash "${BOOTSTRAP_SCRIPT}"
test -f "${scratch}/fresh/.fixture-installed"
test -f "${scratch}/fresh/.fixture-healthy"
test "$(stat -c %a "${scratch}/fresh/.env")" = 600
test "$(grep -c '^ADMIN_ENABLED=true$' "${scratch}/fresh/.env")" = 1
printf '[OK] clean clone installs full AI mode with private env and health check\n'

# A repeat invocation must neither reinstall nor replace existing credentials.
previous_hash="$(sha256sum "${scratch}/fresh/.env")"
if bash "${BOOTSTRAP_SCRIPT}" >"${scratch}/repeat.log" 2>&1; then
  printf 'ERROR: existing .env was not rejected\n' >&2
  exit 1
fi
test "$(sha256sum "${scratch}/fresh/.env")" = "${previous_hash}"
printf '[OK] existing .env is protected from a second install\n'

# Reproduce the real report: Git clone succeeded but .env.example is missing.
git clone --quiet --branch master --single-branch \
  'https://github.com/canjianwuqing-pixel/ai-customer-platform-installer.git' "${scratch}/partial"
rm -f -- "${scratch}/partial/.env.example"
printf 'chat.example.test\nadmin@example.test\n' | AI_CUSTOMER_INSTALL_DIR="${scratch}/partial" bash "${BOOTSTRAP_SCRIPT}"
test -f "${scratch}/partial/.env.example"
test -f "${scratch}/partial/.fixture-installed"
test -z "$(git -C "${scratch}/partial" status --porcelain=v1 --untracked-files=all | grep '^ D .env.example' || true)"
printf '[OK] tracked template can be restored after interrupted clone\n'

# Never overwrite a checkout with uncommitted changes.
git clone --quiet --branch master --single-branch \
  'https://github.com/canjianwuqing-pixel/ai-customer-platform-installer.git' "${scratch}/dirty"
printf '# local edits\n' >>"${scratch}/dirty/install.sh"
if AI_CUSTOMER_INSTALL_DIR="${scratch}/dirty" bash "${BOOTSTRAP_SCRIPT}" >"${scratch}/dirty.log" 2>&1; then
  printf 'ERROR: dirty checkout was not rejected\n' >&2
  exit 1
fi
test ! -e "${scratch}/dirty/.env"
printf '[OK] uncommitted changes stop bootstrap before .env creation\n'

# Never adopt an unrelated local Git project, even if it looks like our repo.
git clone --quiet --branch master --single-branch "${scratch}/origin.git" "${scratch}/other"
if AI_CUSTOMER_INSTALL_DIR="${scratch}/other" bash "${BOOTSTRAP_SCRIPT}" >"${scratch}/other.log" 2>&1; then
  printf 'ERROR: unrelated remote was not rejected\n' >&2
  exit 1
fi
test ! -e "${scratch}/other/.env"
printf '[OK] unrelated repository is rejected\n'
