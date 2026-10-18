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
# Reproduce production GitHub: unrelated EMPTY main is the repository default,
# while the installer and .env.example are on master.
git -C "${scratch}/source" switch --quiet --orphan main
git -C "${scratch}/source" commit --quiet --allow-empty -m 'empty default main'
test "$(git -C "${scratch}/source" rev-parse 'HEAD^{tree}')" = 4b825dc642cb6eb9a060e54bf8d69288fbee4904
git -C "${scratch}/source" switch --quiet master
git clone --bare --quiet "${scratch}/source" "${scratch}/origin.git"
git --git-dir="${scratch}/origin.git" symbolic-ref HEAD refs/heads/main

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

# Reproduce the user's failure: old README cloned default (empty) main.
# The bootstrap must safely switch that pristine repo to installer master.
git clone --quiet 'https://github.com/canjianwuqing-pixel/ai-customer-platform-installer.git' "${scratch}/main-empty"
test "$(git -C "${scratch}/main-empty" branch --show-current)" = main
test ! -e "${scratch}/main-empty/.env.example"
old_main="$(git -C "${scratch}/main-empty" rev-parse HEAD)"
printf 'chat.example.test\nadmin@example.test\n' | AI_CUSTOMER_INSTALL_DIR="${scratch}/main-empty" bash "${BOOTSTRAP_SCRIPT}"
test "$(git -C "${scratch}/main-empty" branch --show-current)" = master
test "$(git -C "${scratch}/main-empty" rev-parse refs/heads/main)" = "${old_main}"
test -f "${scratch}/main-empty/.fixture-installed"
test -f "${scratch}/main-empty/.fixture-healthy"
test "$(stat -c %a "${scratch}/main-empty/.env")" = 600
printf '[OK] previously cloned empty default main safely switches to full installer master\n'

# Main must be completely empty and clean; do not discard local work.
git clone --quiet 'https://github.com/canjianwuqing-pixel/ai-customer-platform-installer.git' "${scratch}/main-dirty"
printf 'keep me\n' > "${scratch}/main-dirty/user-notes.txt"
if AI_CUSTOMER_INSTALL_DIR="${scratch}/main-dirty" bash "${BOOTSTRAP_SCRIPT}" >"${scratch}/main-dirty.log" 2>&1; then
  printf 'ERROR: dirty default main clone was not rejected\n' >&2
  exit 1
fi
test ! -e "${scratch}/main-dirty/.env"
test -f "${scratch}/main-dirty/user-notes.txt"
test "$(git -C "${scratch}/main-dirty" branch --show-current)" = main
printf '[OK] main with untracked user content remains untouched\n'

# A nonempty main tree must never be implicitly replaced with master.
git -C "${scratch}/source" switch --quiet main
printf 'legacy project\n' > "${scratch}/source/legacy.txt"
git -C "${scratch}/source" add legacy.txt
git -C "${scratch}/source" commit --quiet -m 'nonempty main'
git -C "${scratch}/source" switch --quiet master
git --git-dir="${scratch}/origin.git" fetch --quiet "${scratch}/source" +refs/heads/main:refs/heads/main
git clone --quiet 'https://github.com/canjianwuqing-pixel/ai-customer-platform-installer.git' "${scratch}/main-nonempty"
if AI_CUSTOMER_INSTALL_DIR="${scratch}/main-nonempty" bash "${BOOTSTRAP_SCRIPT}" >"${scratch}/main-nonempty.log" 2>&1; then
  printf 'ERROR: nonempty main branch was automatically switched\n' >&2
  exit 1
fi
test ! -e "${scratch}/main-nonempty/.env"
test -f "${scratch}/main-nonempty/legacy.txt"
printf '[OK] nonempty main refused; existing files preserved\n'

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
