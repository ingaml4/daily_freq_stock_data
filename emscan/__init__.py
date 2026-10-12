"""emscan —— A股日频监控：盘后用东方财富免费接口落日线数据，全市场扫描"突然放量"。

模块划分::

    eastmoney.py   东方财富接口客户端（行情快照 + 日K线）
    store.py       SQLite 行情仓库（增量落库、交易日维护）
    scan.py        放量检测算法（纯计算，不联网，可单测）
    report.py      终端/CSV/XLSX/JSON 报告
    notify.py      webhook / 邮件推送（可选）
    demo.py        合成行情，用于无网络环境下端到端自检
    cli.py         命令行入口
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
