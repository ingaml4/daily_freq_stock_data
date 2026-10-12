#!/usr/bin/env bash
# 把本项目推送到 GitHub。
#
# 用法：
#   bash scripts/push_to_github.sh
#   bash scripts/push_to_github.sh git@github.com:ingaml4/daily_freq_stock_data.git   # 改用 SSH
#   bash scripts/push_to_github.sh "" --force                                          # 远端有冲突时强推
#
# 注意：Codex 沙箱里的 <项目>/.git 是只读占位挂载；如果你在自己终端里也遇到
#      "Read-only file system"，脚本会打印两种解法（见下面 step 0 的分支）。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_URL="${1:-https://github.com/ingaml4/daily_freq_stock_data.git}"
BRANCH="${BRANCH:-main}"
FORCE="${2:-}"
COMMIT_MSG="feat: A股日频放量扫描（东财日线落库 + 盘后全市场扫描 + 报告/推送 + 部署脚本）"

cd "${PROJECT_DIR}"

if [ -e .git ] && [ ! -w .git ]; then
    cat <<MSG
[!] ${PROJECT_DIR}/.git 不可写（只读挂载），无法就地建仓。

解法 A（如果你有 sudo，推荐）：
    sudo umount "${PROJECT_DIR}/.git"
    bash scripts/push_to_github.sh

解法 B（把 git 数据放在项目外，后续 git 命令都要带上这两个环境变量）：
    export GIT_DIR=${PROJECT_DIR}.git GIT_WORK_TREE=${PROJECT_DIR}
    git init -b ${BRANCH} && git add -A && git commit -m "init: emscan"
    git remote add origin ${REMOTE_URL} && git push -u origin ${BRANCH}
MSG
    exit 2
fi

echo "[1/5] 准备仓库"
if git rev-parse --git-dir >/dev/null 2>&1; then
    echo "      已存在：$(git rev-parse --git-dir)"
else
    if git init -q -b "${BRANCH}" 2>/dev/null; then
        :
    else
        git init -q && git symbolic-ref HEAD "refs/heads/${BRANCH}"
    fi
    echo "      已初始化：${PROJECT_DIR}/.git（分支 ${BRANCH}）"
fi

echo "[2/5] 暂存文件"
git add -A
git --no-pager diff --cached --name-only | sed -n '1,40p' | sed 's/^/      /'
echo "      共 $(git diff --cached --name-only | wc -l) 个文件（data/ 和 logs/ 已在 .gitignore 里排除）"

echo "[3/5] 提交"
if git diff --cached --quiet; then
    echo "      没有新的变更，跳过提交"
else
    git commit -q -m "${COMMIT_MSG}"
    git --no-pager log --oneline -1 | sed 's/^/      /'
fi

echo "[4/5] 设置远端 origin -> ${REMOTE_URL}"
if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "${REMOTE_URL}"
else
    git remote add origin "${REMOTE_URL}"
fi

echo "[5/5] 推送 ${BRANCH}"
if [ "${FORCE}" = "--force" ]; then
    push_args=(--force-with-lease -u origin "${BRANCH}")
else
    push_args=(-u origin "${BRANCH}")
fi

push_cmd=(git)
if [ -n "${GITHUB_TOKEN:-${GH_TOKEN:-}}" ]; then
    # 有 token 就免交互：用一次性 credential helper 从环境变量取 token，
    # token 只活在本次进程环境里，不会写进 .git/config 或 ~/.git-credentials
    tok_var=GITHUB_TOKEN
    [ -n "${GITHUB_TOKEN:-}" ] || tok_var=GH_TOKEN
    push_cmd+=(-c "credential.helper=!f() { echo username=x-access-token; echo \"password=\${${tok_var}}\"; }; f")
    echo "      用环境变量 ${tok_var} 做 HTTPS 认证（不落盘）"
elif [ -n "${GIT_ASKPASS:-}" ] || [ -n "${SSH_ASKPASS:-}" ]; then
    echo "      提示：检测到 GIT_ASKPASS/SSH_ASKPASS（VS Code 的凭据弹窗），"
    echo "      HTTPS 认证的输入框会弹在编辑器窗口里，而不是终端里；非交互环境下不会弹。"
else
    echo "      提示：未检测到 token / 凭据助手，HTTPS 会要求交互输入（用户名 + PAT）。"
fi

push_output=""
if push_output="$("${push_cmd[@]}" push "${push_args[@]}" 2>&1)"; then
    printf '%s\n' "${push_output}"
    echo "完成：${REMOTE_URL%.git}"
else
    rc=$?
    printf '%s\n' "${push_output}" >&2
    case "${push_output}" in
        *"could not read Username"*|*"Authentication failed"*|*"was removed"*|*"Permission denied"*|*"invalid username or password"*)
            cat >&2 <<'MSG'

>>> 判定：认证失败（没有可用凭据）。二选一：
    a) HTTPS + PAT（推荐，不用配 SSH）：
       export GITHUB_TOKEN=<你的 PAT>     # 只放环境变量，别写进脚本/别贴聊天里
       bash scripts/push_to_github.sh     # 脚本会自动拿它认证，不落盘
    b) SSH：先把公钥加到 GitHub（Settings -> SSH and GPG keys），再执行
       bash scripts/push_to_github.sh git@github.com:ingaml4/daily_freq_stock_data.git
MSG
            ;;
        *"Couldn't connect"*|*"Failed to connect"*|*"Could not resolve host"*|*"Connection timed out"*|*"Connection refused"*)
            cat >&2 <<'MSG'

>>> 判定：网络不通（不是密码问题）。本机 https_proxy=http://10.0.70.30:9997，
    代理不放行 github.com 或 DNS 解析不了都会这样。先自测：
      curl -sI --max-time 10 https://github.com | head -1
    返回 HTTP/2 200 才算链路通；不通就要网络组放行 github.com:443。
MSG
            ;;
        *"rejected"*|*"non-fast-forward"*|*"fetch first"*)
            cat >&2 <<'MSG'

>>> 判定：远端已有内容（建仓时勾了 README/.gitignore）。选一种：
      git fetch origin && git rebase origin/main      # 合并远端后重跑本脚本
      bash scripts/push_to_github.sh "" --force       # 直接以本地为准覆盖远端
MSG
            ;;
    esac
    cat >&2 <<'MSG'

完整排查清单：
  * 认证失败：HTTPS 会要用户名+密码，密码处填 GitHub Personal Access Token（repo 权限）；
    或改用 SSH：bash scripts/push_to_github.sh git@github.com:ingaml4/daily_freq_stock_data.git
    先自测 SSH：ssh -T git@github.com
  * 远端非空（建仓时勾了 README/.gitignore）：
    git fetch origin && git rebase origin/main      然后重跑本脚本
    或直接覆盖：bash scripts/push_to_github.sh "" --force
  * 网络/代理：本机 https_proxy=http://10.0.70.30:9997，若代理不放行 github.com 需要开通；
    自测：curl -sI https://github.com
MSG
    exit "${rc}"
fi
