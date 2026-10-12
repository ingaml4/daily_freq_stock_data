#!/usr/bin/env bash
# 每个交易日 17:00 由 cron 调用：增量更新行情 → 全市场放量扫描 → 出报告/推送。
#
# 手动试跑：  bash scripts/daily_scan.sh
# 退出码：    0=正常（含"非交易日自动跳过"）；非 0=出错，见 logs/cron.log

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${EMSCAN_PYTHON:-/usr/bin/python3}"
LOG_DIR="${PROJECT_DIR}/logs"
CRON_LOG="${LOG_DIR}/cron.log"
LOCK_FILE="${LOG_DIR}/emscan.lock"
export TZ="${TZ:-Asia/Shanghai}"

mkdir -p "${LOG_DIR}"

# 用文件锁避免上一次还没跑完又被拉起来（backfill 可能跑几分钟）
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] 上一次任务仍在运行，本次跳过" >>"${CRON_LOG}"
    exit 0
fi

cd "${PROJECT_DIR}"
{
    echo "=== $(date '+%F %T') 开始 ==="
} >>"${CRON_LOG}" 2>&1

set +e
"${PYTHON_BIN}" -m emscan run
rc=$?
set -e

{
    echo "=== $(date '+%F %T') 结束 exit=${rc} ==="
} >>"${CRON_LOG}" 2>&1
exit "${rc}"
