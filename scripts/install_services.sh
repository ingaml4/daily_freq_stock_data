#!/usr/bin/env bash
# 一键把 emscan 装成 systemd 服务（两类）：
#
#   1) emscan-daily.timer + emscan-daily.service —— 每个交易日定时跑 `emscan run`
#      （自愈同步 + 放量扫描 + 指标信号 + 报告/推送）
#   2) emscan-web.service —— 常驻网页端（四个查询按钮 + 可排序表格 + 跳雪球）
#
# 用法（在服务器上、项目目录里执行）：
#   scripts/install_services.sh --licence <麦蕊证书>              # 装 + enable + 立即启动
#   scripts/install_services.sh --licence <证书> --dry-run        # 只看会做什么，不动系统
#   scripts/install_services.sh --user                            # 免 root（装到 ~/.config/systemd/user）
#   scripts/install_services.sh --uninstall                       # 卸掉（保留环境文件）
#
# 装完后常用命令：
#   systemctl status emscan-web emscan-daily.timer
#   systemctl list-timers emscan-daily.timer
#   journalctl -u emscan-web -f
#   systemctl start emscan-daily.service        # 立刻手动跑一次

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_NAME="$(basename "$0")"

# ---------------------------------------------------------------- 默认值
LICENCE=""
PASSWORD=""
WEB_HOST="127.0.0.1"
WEB_PORT="8080"
DAILY_TIMES="15:32,17:15"   # 逗号分隔；只跑一次就写 "17:15"
PYTHON_BIN=""
USER_MODE="false"
DRY_RUN="false"
DO_ENABLE="true"
UNINSTALL="false"
PURGE="false"
SYSTEMD_DIR=""
ENV_DIR=""
ENV_FILE=""

SYSTEM_SYSTEMD_DIR="/etc/systemd/system"
SYSTEM_ENV_DIR="/etc/emscan"

log()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[x] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
把 emscan 装成 systemd 服务（定时任务 + 网页端）。

用法：
  scripts/install_services.sh [选项]

常用选项：
  --licence <证书>      麦蕊智数 licence（不填则从环境变量 / .env / config.yaml 里找）
  --password <口令>     网页端登录口令；不填则自动生成一个随机串（重复安装会保留原有的）
                        旧写法 --token 仍然可用，含义相同
  --web-host <地址>     网页监听地址，默认 127.0.0.1（对外请走反向代理，或配合 --password 用 0.0.0.0）
  --web-port <端口>     网页端口，默认 8080
  --daily-times <时间>  每日跑的时间（本地北京时间），默认 "15:32,17:15"
                        说明：15:32 收盘后出放量；当天技术指标 15:35 才发布，由 17:15 那次补
  --python <路径>       指定 python3 解释器（默认取 PATH 里的 python3）
  --user                免 root：装成用户级服务（~/.config/systemd/user），用 systemctl --user 管理
  --dry-run             只打印将要生成的文件与命令，不写任何东西、不动 systemd
  --no-enable           只生成单元文件，不执行 systemctl enable/start（方便先检查）
  --uninstall           停止并删除服务（环境文件默认保留）
  --purge               配合 --uninstall：连环境文件一起删
  --systemd-dir <目录>  高级：单元文件输出目录（默认 /etc/systemd/system 或用户目录）
  --env-dir <目录>      高级：环境文件目录（默认 /etc/emscan 或 ~/.config/emscan）
  -h, --help            显示本帮助

示例：
  sudo scripts/install_services.sh --licence xxxx-yyyy-zzzz
  scripts/install_services.sh --user --password 我的口令 --web-port 18080
  scripts/install_services.sh --dry-run --daily-times 17:15
USAGE
}

# ---------------------------------------------------------------- 参数解析
while [ $# -gt 0 ]; do
    case "$1" in
        --licence|--license) LICENCE="${2:-}"; shift 2 ;;
        --password)          PASSWORD="${2:-}"; shift 2 ;;
        --token)             PASSWORD="${2:-}"; shift 2 ;;   # 旧名字，等同 --password
        --web-host)          WEB_HOST="${2:-}"; shift 2 ;;
        --web-port)          WEB_PORT="${2:-}"; shift 2 ;;
        --daily-times)       DAILY_TIMES="${2:-}"; shift 2 ;;
        --python)            PYTHON_BIN="${2:-}"; shift 2 ;;
        --systemd-dir)       SYSTEMD_DIR="${2:-}"; shift 2 ;;
        --env-dir)           ENV_DIR="${2:-}"; shift 2 ;;
        --user)              USER_MODE="true"; shift ;;
        --dry-run)           DRY_RUN="true"; shift ;;
        --no-enable)         DO_ENABLE="false"; shift ;;
        --uninstall)         UNINSTALL="true"; shift ;;
        --purge)             PURGE="true"; shift ;;
        -h|--help)           usage; exit 0 ;;
        *) die "未知参数：$1（用 --help 看用法）" ;;
    esac
done

# ---------------------------------------------------------------- 环境探测
[ -d "${PROJECT_DIR}/emscan" ] || die "没找到 ${PROJECT_DIR}/emscan，请在项目目录里运行本脚本"
[ -f "${PROJECT_DIR}/config.yaml" ] || warn "${PROJECT_DIR}/config.yaml 不存在，将使用内置默认配置"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
[ -n "${PYTHON_BIN}" ] && [ -x "${PYTHON_BIN}" ] || die "找不到 python3，请用 --python /path/to/python3 指定"

if [ "${USER_MODE}" = "true" ]; then
    RUN_USER="$(id -un)"
    SYSTEMD_DIR="${SYSTEMD_DIR:-${HOME}/.config/systemd/user}"
    ENV_DIR="${ENV_DIR:-${HOME}/.config/emscan}"
    SYSTEMCTL=(systemctl --user)
    WANTED_BY="default.target"
else
    RUN_USER="$(stat -c %U "${PROJECT_DIR}" 2>/dev/null || id -un)"
    SYSTEMD_DIR="${SYSTEMD_DIR:-${SYSTEM_SYSTEMD_DIR}}"
    ENV_DIR="${ENV_DIR:-${SYSTEM_ENV_DIR}}"
    SYSTEMCTL=(systemctl)
    WANTED_BY="multi-user.target"
fi
ENV_FILE="${ENV_FILE:-${ENV_DIR}/emscan.env}"

# 需要提权吗（只看默认系统目录；自定义目录按当前用户权限处理）
NEED_ROOT="false"
if [ "${USER_MODE}" != "true" ] && [ "$(id -u)" -ne 0 ]; then
    if [ "${SYSTEMD_DIR}" = "${SYSTEM_SYSTEMD_DIR}" ] || [ "${ENV_DIR}" = "${SYSTEM_ENV_DIR}" ]; then
        NEED_ROOT="true"
    fi
fi
run_priv() { # 需要 root 的操作（写 /etc、systemctl）
    if [ "${NEED_ROOT}" = "true" ]; then
        if ! command -v sudo >/dev/null 2>&1; then
            die "需要 root（写 ${SYSTEMD_DIR} / ${ENV_DIR}）但本机没有 sudo：请用 root 执行，或加 --user"
        fi
        sudo -n "$@" 2>/dev/null || sudo "$@"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------- licence / 网页口令
read_from_env_file() { # $1=KEY  -> 打印值（找不到则为空）
    [ -f "${ENV_FILE}" ] || return 0
    grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "${ENV_FILE}" 2>/dev/null | tail -1 |
        sed -E "s/^[[:space:]]*(export[[:space:]]+)?$1=//; s/^[\"']//; s/[\"']$//"
}
read_from_dotenv() {
    local file="${PROJECT_DIR}/.env"
    [ -f "${file}" ] || return 0
    grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "${file}" 2>/dev/null | tail -1 |
        sed -E "s/^[[:space:]]*(export[[:space:]]+)?$1=//; s/^[\"']//; s/[\"']$//"
}

if [ -z "${LICENCE}" ]; then
    LICENCE="$(read_from_env_file MAIRUI_LICENCE)"
    [ -n "${LICENCE}" ] || LICENCE="$(read_from_dotenv MAIRUI_LICENCE)"
    [ -n "${LICENCE}" ] || LICENCE="${MAIRUI_LICENCE:-}"
    [ -n "${LICENCE}" ] || LICENCE="$(grep -E '^[[:space:]]*licence:' "${PROJECT_DIR}/config.yaml" 2>/dev/null |
        head -1 | sed -E "s/^[[:space:]]*licence:[[:space:]]*//; s/^[\"']//; s/[\"']$//" || true)"
fi
if [ -z "${PASSWORD}" ]; then
    PASSWORD="$(read_from_env_file EMSCAN_WEB_PASSWORD)"
    [ -n "${PASSWORD}" ] || PASSWORD="$(read_from_env_file EMSCAN_WEB_TOKEN)"   # 旧名字兼容
    [ -n "${PASSWORD}" ] || PASSWORD="${EMSCAN_WEB_PASSWORD:-${EMSCAN_WEB_TOKEN:-}}"
fi
PASSWORD_GENERATED="false"
if [ -z "${PASSWORD}" ] && [ "${UNINSTALL}" != "true" ]; then
    PASSWORD="$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    PASSWORD_GENERATED="true"
fi
mask() { # 只露头尾，避免日志/终端泄露
    local v="${1:-}"
    if [ -z "${v}" ]; then echo "(未设置)"; elif [ ${#v} -le 8 ]; then echo "****"; else echo "${v:0:4}…${v: -4}"; fi
}

# ---------------------------------------------------------------- 生成内容
ONCALENDAR=""
IFS=',' read -r -a _times <<<"${DAILY_TIMES}"
for t in "${_times[@]}"; do
    t="$(echo "$t" | tr -d '[:space:]')"
    [ -n "$t" ] || continue
    [[ "$t" =~ ^([0-9]{1,2}):([0-9]{2})$ ]] || die "--daily-times 格式应为 HH:MM[,HH:MM]，收到：$t"
    ONCALENDAR+="OnCalendar=Mon..Fri $(printf '%02d' "${BASH_REMATCH[1]}"):${BASH_REMATCH[2]}"$'\n'
done
[ -n "${ONCALENDAR}" ] || die "--daily-times 不能为空"

render() { # $1=模板文件  -> stdout=渲染后的内容
    local tpl="$1"
    [ -f "${tpl}" ] || die "缺少模板文件：${tpl}"
    sed -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
        -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
        -e "s|__ENV_FILE__|${ENV_FILE}|g" \
        -e "s|__RUN_USER__|${RUN_USER}|g" \
        -e "s|__WANTED_BY__|${WANTED_BY}|g" \
        "${tpl}" | sed -e "/^#TPL#/d" -e "/^__ONCALENDAR__$/d" -e "/^OnCalendar=/d"
}

render_timer() {
    awk -v lines="${ONCALENDAR}" '
        /^#TPL#/ { next }
        /^__ONCALENDAR__$/ { printf "%s", lines; next }
        { print }
    ' "${PROJECT_DIR}/deploy/systemd/emscan-daily.timer.in"
}

if [ "${USER_MODE}" = "true" ]; then
    # 用户级服务不能写 User=，安装目标也要换成 default.target
    render_service() {
        render "$1" | grep -v '^User=' | sed -e "s|^WantedBy=multi-user.target$|WantedBy=default.target|"
    }
else
    render_service() { render "$1"; }
fi

# 这些变量由脚本自己管理；重装时环境文件里"其它"变量会原样保留，避免冲掉你手工加的配置
MANAGED_ENV_KEYS="MAIRUI_LICENCE EMSCAN_WEB_HOST EMSCAN_WEB_PORT EMSCAN_WEB_PASSWORD EMSCAN_WEB_TOKEN TZ"

render_env() {
    cat <<ENV
# emscan 环境文件（由 ${SCRIPT_NAME} 生成，权限 600，请勿提交进 git）
# 麦蕊智数证书：https://www.mairui.club 免费领取
MAIRUI_LICENCE=${LICENCE}

# 网页端（python3 -m emscan web / emscan-web.service）
EMSCAN_WEB_HOST=${WEB_HOST}
EMSCAN_WEB_PORT=${WEB_PORT}
EMSCAN_WEB_PASSWORD=${PASSWORD}

# 时区固定东八区（脚本内部也会再固定一次）
TZ=Asia/Shanghai
ENV
    if [ -f "${ENV_FILE}" ]; then
        local extra
        extra="$(grep -vE "^[[:space:]]*(export[[:space:]]+)?($(echo "${MANAGED_ENV_KEYS}" | tr ' ' '|'))=" \
            "${ENV_FILE}" 2>/dev/null | grep -vE '^[[:space:]]*(#|$)' || true)"
        if [ -n "${extra}" ]; then
            printf '\n# ↓ 你自己加的变量（重装时原样保留）\n'
            printf '%s\n' "${extra}"
        fi
    fi
}

# ---------------------------------------------------------------- 卸载
if [ "${UNINSTALL}" = "true" ]; then
    step "卸载 emscan 服务"
    if [ "${DO_ENABLE}" = "true" ]; then
        run_priv "${SYSTEMCTL[@]}" disable --now emscan-daily.timer emscan-web.service 2>/dev/null || true
        run_priv "${SYSTEMCTL[@]}" daemon-reload || true
    fi
    for unit in emscan-daily.timer emscan-daily.service emscan-web.service; do
        if [ -e "${SYSTEMD_DIR}/${unit}" ]; then
            run_priv rm -f "${SYSTEMD_DIR}/${unit}"
            log "  已删除 ${SYSTEMD_DIR}/${unit}"
        fi
    done
    if [ "${PURGE}" = "true" ] && [ -e "${ENV_FILE}" ]; then
        run_priv rm -f "${ENV_FILE}"
        log "  已删除环境文件 ${ENV_FILE}"
    else
        [ -e "${ENV_FILE}" ] && log "  环境文件保留：${ENV_FILE}（想一起删就加 --purge）"
    fi
    log "卸载完成。"
    exit 0
fi

# ---------------------------------------------------------------- 检查解释器依赖
DEP_CHECK="import requests, pandas, numpy, yaml, openpyxl; print('deps ok')"
if ! "${PYTHON_BIN}" -c "${DEP_CHECK}" >/dev/null 2>&1; then
    warn "${PYTHON_BIN} 缺少依赖（requests/pandas/numpy/PyYAML/openpyxl）："
    warn "  ${PYTHON_BIN} -m pip install -r ${PROJECT_DIR}/requirements.txt"
fi

# ---------------------------------------------------------------- 预览 / 落地
DAILY_UNIT="${SYSTEMD_DIR}/emscan-daily.service"
TIMER_UNIT="${SYSTEMD_DIR}/emscan-daily.timer"
WEB_UNIT="${SYSTEMD_DIR}/emscan-web.service"

step "安装计划"
cat <<PLAN
  项目目录        : ${PROJECT_DIR}
  Python          : ${PYTHON_BIN}
  运行用户        : ${RUN_USER}$([ "${USER_MODE}" = "true" ] && echo "（用户级服务）")
  单元文件        : ${SYSTEMD_DIR}/
                    ├─ emscan-daily.service   （定时任务本体，oneshot）
                    ├─ emscan-daily.timer     （${DAILY_TIMES//,/ 、 } 触发）
                    └─ emscan-web.service     （常驻网页端 http://${WEB_HOST}:${WEB_PORT}）
  环境文件        : ${ENV_FILE}（600，含证书与网页口令）
  麦蕊 licence    : $(mask "${LICENCE}")
  网页登录口令    : $(mask "${PASSWORD}")$([ "${PASSWORD_GENERATED}" = "true" ] && echo "（本次自动生成）")
  自动 enable     : ${DO_ENABLE}
PLAN

if [ "${DRY_RUN}" = "true" ]; then
    step "--dry-run：下面是将会写入的内容（不落盘）"
    for item in "emscan-daily.service:${PROJECT_DIR}/deploy/systemd/emscan-daily.service.in" \
                "emscan-web.service:${PROJECT_DIR}/deploy/systemd/emscan-web.service.in"; do
        name="${item%%:*}"; tpl="${item#*:}"
        log "----- ${SYSTEMD_DIR}/${name} -----"
        render_service "${tpl}"
    done
    log "----- ${TIMER_UNIT} -----"
    render_timer
    log "----- ${ENV_FILE} -----"
    render_env | sed -e "s|${PASSWORD}|$(mask "${PASSWORD}")|"
    step "将要执行的命令"
    cat <<CMDS
  install -d -m 755 ${SYSTEMD_DIR}
  install -d -m 700 ${ENV_DIR}
  install -m 600 <env> ${ENV_FILE}
  install -m 644 <unit> ${DAILY_UNIT} ${TIMER_UNIT} ${WEB_UNIT}
  ${SYSTEMCTL[*]} daemon-reload
  ${SYSTEMCTL[*]} enable --now emscan-daily.timer emscan-web.service
CMDS
    log ""
    log "（--dry-run 结束，什么都没改）"
    exit 0
fi

step "写入单元文件与环境文件"
run_priv mkdir -p "${SYSTEMD_DIR}" "${ENV_DIR}"
if [ "${NEED_ROOT}" = "true" ]; then
    run_priv chmod 755 "${SYSTEMD_DIR}"
    run_priv chmod 700 "${ENV_DIR}"
else
    chmod 755 "${SYSTEMD_DIR}"; chmod 700 "${ENV_DIR}" 2>/dev/null || true
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT
render_service "${PROJECT_DIR}/deploy/systemd/emscan-daily.service.in" >"${tmpdir}/emscan-daily.service"
render_service "${PROJECT_DIR}/deploy/systemd/emscan-web.service.in"   >"${tmpdir}/emscan-web.service"
render_timer                                                          >"${tmpdir}/emscan-daily.timer"
render_env >"${tmpdir}/emscan.env"
chmod 600 "${tmpdir}/emscan.env"

run_priv install -m 644 "${tmpdir}/emscan-daily.service" "${DAILY_UNIT}"
run_priv install -m 644 "${tmpdir}/emscan-daily.timer"   "${TIMER_UNIT}"
run_priv install -m 644 "${tmpdir}/emscan-web.service"   "${WEB_UNIT}"
run_priv install -m 600 "${tmpdir}/emscan.env"           "${ENV_FILE}"
if [ "${NEED_ROOT}" = "true" ]; then
    run_priv chown "${RUN_USER}" "${ENV_FILE}" 2>/dev/null || true
fi
for f in "${DAILY_UNIT}" "${TIMER_UNIT}" "${WEB_UNIT}" "${ENV_FILE}"; do log "  写入 ${f}"; done

step "校验单元文件"
if command -v systemd-analyze >/dev/null 2>&1 && [ "${USER_MODE}" != "true" ]; then
    for unit in "${DAILY_UNIT}" "${TIMER_UNIT}" "${WEB_UNIT}"; do
        if systemd-analyze verify "${unit}" >/dev/null 2>&1; then
            log "  OK   ${unit}"
        else
            warn "systemd-analyze verify ${unit} 有告警（多数是环境差异，可忽略；详见 systemctl status）"
        fi
    done
else
    log "  跳过（没有 systemd-analyze 或用户级安装）"
fi

if [ "${DO_ENABLE}" = "true" ]; then
    step "enable + 启动"
    run_priv "${SYSTEMCTL[@]}" daemon-reload
    run_priv "${SYSTEMCTL[@]}" enable emscan-daily.timer emscan-web.service
    # 必须 restart：systemd 只在重启时重新读取 EnvironmentFile，
    # 所以"改了 --web-host/口令后重跑本脚本"能立刻生效（enable --now 对已运行的服务不会重启）
    run_priv "${SYSTEMCTL[@]}" restart emscan-web.service
    run_priv "${SYSTEMCTL[@]}" restart emscan-daily.timer
    run_priv "${SYSTEMCTL[@]}" --no-pager status emscan-daily.timer emscan-web.service 2>&1 |
        sed -n '1,24p' || true
    if [ "${USER_MODE}" = "true" ]; then
        warn "用户级服务：想让它在退出登录后继续跑，执行 loginctl enable-linger ${RUN_USER}"
    fi
    if [ "${WEB_HOST}" = "0.0.0.0" ]; then
        log "  提示：网页对外监听，浏览器打开 http://<服务器IP>:${WEB_PORT}/ 会要求登录口令"
    fi
    step "自检：网页端能否访问"
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if command -v curl >/dev/null 2>&1 &&
            curl -fsS --max-time 3 -u ":${PASSWORD}" "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1; then
            log "  OK  http://127.0.0.1:${WEB_PORT}/healthz 有响应"
            break
        fi
        sleep 1
        [ "${i}" = "10" ] && warn "10 秒内没等到网页端响应：看 journalctl -u emscan-web -n 50（或 --user）"
    done
    if [ "${PASSWORD_GENERATED}" = "true" ]; then
        log "  网页登录口令（自动生成，请保存；也可在 ${ENV_FILE} 里查看）：${PASSWORD}"
    fi
else
    step "已生成单元文件（--no-enable：未执行 systemctl）"
    cat <<CMDS
  手动启用：
    ${SYSTEMCTL[*]} daemon-reload
    ${SYSTEMCTL[*]} enable --now emscan-daily.timer emscan-web.service
CMDS
fi

step "完成，常用命令"
cat <<EOF
  # 看状态
  ${SYSTEMCTL[*]} status emscan-web emscan-daily.timer
  ${SYSTEMCTL[*]} list-timers emscan-daily.timer
  # 看日志（网页端 / 每日任务）
  journalctl -u emscan-web -n 50 --no-pager$([ "${USER_MODE}" = "true" ] && echo " --user")
  journalctl -u emscan-daily -n 80 --no-pager$([ "${USER_MODE}" = "true" ] && echo " --user")
  # 立刻手动跑一次每日任务（看效果）
  ${SYSTEMCTL[*]} start emscan-daily.service
  # 网页端
  浏览器打开 http://${WEB_HOST}:${WEB_PORT}/        （要求用口令登录；口令见 ${ENV_FILE}）\n  # 命令行/脚本可以用 Basic 认证： curl -u ":<口令>" http://${WEB_HOST}:${WEB_PORT}/api/signals
  # 改配置后重启网页端
  ${SYSTEMCTL[*]} restart emscan-web
  # 卸载
  ${SCRIPT_NAME} $([ "${USER_MODE}" = "true" ] && echo "--user ")--uninstall
EOF
