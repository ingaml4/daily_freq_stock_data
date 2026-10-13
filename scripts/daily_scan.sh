#!/usr/bin/env bash
# 每个交易日 17:00 由 cron 调用：自愈同步（含指标）→ 放量扫描 → 指标信号 → 报告/推送。
#
# 手动试跑：  bash scripts/daily_scan.sh
# 退出码：    0=正常（含"非交易日自动跳过"）；非 0=出错，见 logs/cron.log
#
# 证书两种给法，任选其一：
#   ① 写在 config.yaml 的 mairui.licence
#   ② 走环境变量：export MAIRUI_LICENCE=...（推荐；也可以写进下面的 LICENCE_FILE）

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${EMSCAN_PYTHON:-/usr/bin/python3}"
LOG_DIR="${PROJECT_DIR}/logs"
CRON_LOG="${LOG_DIR}/cron.log"
LOCK_FILE="${LOG_DIR}/emscan.lock"
LICENCE_FILE="${EMSCAN_LICENCE_FILE:-/etc/emscan/emscan.env}"

# 全部时间判断都按东八区（脚本内部也会再固定一次）
export TZ="${TZ:-Asia/Shanghai}"

# 服务器上只放了证书文件？这里自动加载
if [[ -f "${LICENCE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${LICENCE_FILE}"
fi

mkdir -p "${LOG_DIR}"

# 用文件锁避免上一次还没跑完又被拉起来（首次全量/指标回补可能跑很久）
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] 上一次任务仍在运行，本次跳过" >>"${CRON_LOG}"
    exit 0
fi

cd "${PROJECT_DIR}"
echo "=== $(date '+%F %T') 开始 ===" >>"${CRON_LOG}" 2>&1

set +e
"${PYTHON_BIN}" -m emscan run >>"${CRON_LOG}" 2>&1
rc=$?
set -e

echo "=== $(date '+%F %T') 结束 exit=${rc} ===" >>"${CRON_LOG}" 2>&1
exit "${rc}"
