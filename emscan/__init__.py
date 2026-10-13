"""emscan —— A股日频监控：盘后用麦蕊智数接口落日线 + 日频指标，全市场扫描。

模块划分::

    mairui.py      麦蕊智数接口客户端（列表/快照/日线/指标/交易日历）
    pipeline.py    自愈同步：比对交易日历，把缺的日线、指标、流通股本补齐
    store.py       SQLite 行情仓库（symbols / bars / indicators / runs）
    scan.py        放量检测算法（纯计算，不联网，可单测）
    signals.py     指标信号筛选（金叉/死叉/布林轨/均线，纯计算）
    report.py      终端/CSV/XLSX/JSON 报告
    notify.py      webhook / 邮件推送（可选）
    tzutil.py      东八区时间（所有"今天/现在"的唯一入口）
    demo.py        合成行情，用于无网络环境下端到端自检
    cli.py         命令行入口
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
