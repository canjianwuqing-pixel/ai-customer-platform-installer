#!/usr/bin/env bash
# First-install entry point. Intentionally does not handle upgrades or overwrite secrets.
set +x
set -Eeuo pipefail
umask 077

readonly REPOSITORY_URL='https://github.com/canjianwuqing-pixel/ai-customer-platform-installer.git'
readonly INSTALL_DIR="${AI_CUSTOMER_INSTALL_DIR:-/opt/ai-customer-platform-installer}"

die() {
  printf '一键安装已停止：%s\n' "$1" >&2
  exit 1
}

if (( EUID != 0 )); then
  die '请先进入 root 终端（sudo -i），再执行安装命令。'
fi
command -v git >/dev/null 2>&1 || die '未找到 git；请使用 README 的完整一键安装命令。'
command -v readlink >/dev/null 2>&1 || die '未找到 readlink，无法校验安装路径。'
[[ "${INSTALL_DIR}" == /* && "${INSTALL_DIR}" != / ]] || die '安装路径必须是非根目录的绝对路径。'
[[ "$(readlink -m -- "${INSTALL_DIR}")" == "${INSTALL_DIR}" ]] || die '安装路径不能包含相对路径段或符号链接。'

if [[ -e "${INSTALL_DIR}" || -L "${INSTALL_DIR}" ]]; then
  # Allow a previous clone that stopped before creating .env to resume safely.
  [[ -d "${INSTALL_DIR}/.git" && ! -L "${INSTALL_DIR}" ]] || die '安装目录已存在但不是本项目 Git 克隆，拒绝覆盖。'
  [[ ! -e "${INSTALL_DIR}/.env" && ! -L "${INSTALL_DIR}/.env" ]] || die '已存在 .env；为保护现有数据和密码，不重新初始化。'
  [[ "$(git -C "${INSTALL_DIR}" config --get remote.origin.url)" == "${REPOSITORY_URL}" ]] || die '现有目录不是官方 GitHub 仓库，拒绝覆盖。'
  # The GitHub repository still has an unrelated EMPTY default branch
  # "main"; early README examples cloned it instead of real installer "master".
  # Resume that exact incomplete checkout without deleting/resetting anything.
  branch="$(git -C "${INSTALL_DIR}" symbolic-ref --quiet --short HEAD)" || die '现有目录处于 detached HEAD，拒绝修改。'
  [[ "${branch}" == master || "${branch}" == main ]] || die '现有目录既不是 master，也不是可恢复的空白 main 分支，拒绝修改。'
  if [[ "${branch}" == main ]]; then
    [[ "$(git -C "${INSTALL_DIR}" rev-parse 'HEAD^{tree}')" == 4b825dc642cb6eb9a060e54bf8d69288fbee4904 ]] || die 'main 分支含项目文件，不能作为空白误克隆自动切换。'
  elif [[ ! -f "${INSTALL_DIR}/.env.example" ]]; then
    git -C "${INSTALL_DIR}" restore --source=HEAD --worktree -- .env.example || die '无法恢复 Git 跟踪的配置模板。'
  fi
  [[ -z "$(git -C "${INSTALL_DIR}" status --porcelain=v1 --untracked-files=all)" ]] || die '原有工作区存在未提交改动，请先检查，拒绝覆盖。'
  git -C "${INSTALL_DIR}" fetch origin master
  if [[ "${branch}" == main ]]; then
    # Keep the old local main branch; Git safely refuses checkout conflicts.
    if git -C "${INSTALL_DIR}" show-ref --verify --quiet refs/heads/master; then
      die '本地已经存在 master 分支，拒绝自动覆盖或重建。'
    fi
    git -C "${INSTALL_DIR}" show-ref --verify --quiet refs/remotes/origin/master || die '远程不存在 master，拒绝继续。'
    git -C "${INSTALL_DIR}" switch --create master --track origin/master
  else
    git -C "${INSTALL_DIR}" merge-base --is-ancestor HEAD origin/master || die 'Git 历史不能安全快进，拒绝覆盖。'
    git -C "${INSTALL_DIR}" merge --ff-only origin/master
  fi
  [[ "$(git -C "${INSTALL_DIR}" branch --show-current)" == master ]] || die 'master 切换或快进失败。'
else
  mkdir -p -- "$(dirname -- "${INSTALL_DIR}")"
  git clone --branch master --single-branch -- "${REPOSITORY_URL}" "${INSTALL_DIR}"
fi

[[ -f "${INSTALL_DIR}/install.sh" ]] || die '仓库缺少 install.sh，未开始安装。'
[[ ! -e "${INSTALL_DIR}/.env" && ! -L "${INSTALL_DIR}/.env" ]] || die '发现已有 .env，拒绝覆盖。'

candidate=''
cleanup() {
  if [[ -n "${candidate}" ]]; then rm -f -- "${candidate}"; fi
}
trap cleanup EXIT
candidate="$(mktemp "${INSTALL_DIR}/.env.bootstrap.XXXXXXXX")"
# Read the tracked template from Git itself; the checkout may lack the dotfile.
git -C "${INSTALL_DIR}" show HEAD:.env.example > "${candidate}" || die 'Git 版本中找不到 .env.example，未创建配置。'
grep -qx 'ADMIN_ENABLED=false' "${candidate}" || die '配置模板不包含预期的 ADMIN_ENABLED=false，停止安装。'
sed -i 's/^ADMIN_ENABLED=false$/ADMIN_ENABLED=true/' "${candidate}"
grep -qx 'ADMIN_ENABLED=true' "${candidate}" || die '无法启用完整 AI 客服模式。'
chmod 0600 "${candidate}"
# Hard-link creation is atomic and fails if .env already exists; never replace it.
ln -- "${candidate}" "${INSTALL_DIR}/.env" || die '创建 .env 失败（或配置已经存在），未覆盖原文件。'
rm -f -- "${candidate}"
candidate=''

printf '已自动创建完整 AI 客服配置，开始安装 Chatwoot、AI Runtime 和统一后台。\n'
cd -- "${INSTALL_DIR}"
bash ./install.sh
bash ./manage.sh health
printf '一键安装及服务健康检查成功。\n'
