"""报告输出：终端表格 / CSV / XLSX / JSON。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

from .mairui import BOARD_LABELS
from .scan import ScanResult

log = logging.getLogger(__name__)

# 报告里展示的列及其中文表头
CONSOLE_COLUMNS: List[str] = [
    "code",
    "name",
    "board",
    "close",
    "pct_chg",
    "turnover",
    "vol_ratio_5",
    "vol_ratio_20",
    "vol_ratio_60",
    "vol_z60",
    "amount_yi",
    "signal",
    "score",
]

COLUMN_LABELS: Dict[str, str] = {
    "secid": "secid",
    "code": "代码",
    "name": "名称",
    "board": "板块",
    "trade_date": "交易日",
    "close": "收盘价",
    "pct_chg": "涨跌幅%",
    "amount": "成交额(元)",
    "amount_yi": "成交额(亿)",
    "turnover": "换手率%",
    "volume": "成交量(手)",
    "volume_ratio": "量比",
    "spot_volume_ratio": "快照量比",
    "valid_bars": "有效K线",
    "signal": "形态",
    "score": "评分",
}

FLOAT_FORMATS: Dict[str, str] = {
    "close": "{:.2f}",
    "pct_chg": "{:+.2f}",
    "turnover": "{:.2f}",
    "amount_yi": "{:.2f}",
    "vol_ratio_5": "{:.2f}",
    "vol_ratio_10": "{:.2f}",
    "vol_ratio_20": "{:.2f}",
    "vol_ratio_60": "{:.2f}",
    "vol_z60": "{:.1f}",
    "score": "{:.2f}",
    "spot_volume_ratio": "{:.2f}",
}


def display_width(text: str) -> int:
    """中日韩字符按 2 个字符宽度计算，保证终端表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = width - display_width(text)
    if gap <= 0:
        return text
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def _cell(
    row: pd.Series,
    column: str,
    float_formats: Dict[str, str],
    labels: Optional[Dict[str, str]] = None,
) -> str:
    value = row.get(column)
    if value is None:
        return "-"
    try:
        if pd.isna(value):
            return "-"
    except (TypeError, ValueError):  # 非标量（极少见），按原值展示
        pass
    if column == "board":
        return BOARD_LABELS.get(str(value), str(value))
    if isinstance(value, (int, float)):
        fmt = float_formats.get(column, "{:.2f}")
        return fmt.format(float(value))
    return str(value)


def render_table(
    frame: pd.DataFrame,
    columns: Sequence[str] = tuple(CONSOLE_COLUMNS),
    max_rows: Optional[int] = None,
    float_formats: Optional[Dict[str, str]] = None,
    labels: Optional[Dict[str, str]] = None,
) -> str:
    """把命中结果渲染成等宽的纯文本表格。"""
    if frame is None or frame.empty:
        return "（无命中）"
    fmts = float_formats or FLOAT_FORMATS
    label_map = labels or COLUMN_LABELS
    columns = [c for c in columns if c in frame.columns]
    subset = frame if max_rows is None else frame.head(int(max_rows))

    headers = [label_map.get(c, c) for c in columns]
    body = [[_cell(row, c, fmts, label_map) for c in columns] for _, row in subset.iterrows()]
    widths = [
        (
            max(display_width(headers[i]), *(display_width(r[i]) for r in body))
            if body
            else display_width(headers[i])
        )
        for i in range(len(columns))
    ]
    # 有数字格式的列右对齐，其余（代码、名称、板块、形态…）左对齐
    right_align = set(fmts) | {"valid_bars", "rule_count"}

    lines = ["  ".join(_pad(headers[i], widths[i]) for i in range(len(columns)))]
    lines.append("  ".join("-" * widths[i] for i in range(len(columns))))
    for row_values in body:
        lines.append(
            "  ".join(
                _pad(row_values[i], widths[i], "right" if columns[i] in right_align else "left")
                for i in range(len(columns))
            )
        )
    return "\n".join(lines)


def build_summary(result: ScanResult, cfg) -> str:
    """给终端和推送共用的文字摘要。"""
    stats = result.stats or {}
    thresholds = stats.get("thresholds", {})
    lines = [
        f"{cfg.get('report.title', 'A股日频放量扫描')} | 交易日 {result.trade_date}",
        (
            f"全市场 {stats.get('universe', '-')} 只 / 参与扫描 {stats.get('scanned', '-')} 只"
            f" / 命中 {len(result.hits)} 只"
        ),
        (
            "命中规则: 量比5 >= {vol_ratio_5_min} 且 (量比20 >= {vol_ratio_20_min} 或 Z >= {z_min})"
            "；成交额 >= {min_amount} 元，换手率 >= {min_turnover}%，至少 {min_bars} 根K线".format(
                vol_ratio_5_min=thresholds.get("vol_ratio_5_min", "-"),
                vol_ratio_20_min=thresholds.get("vol_ratio_20_min", "-"),
                z_min=thresholds.get("z_min", "-"),
                min_amount=thresholds.get("min_amount", "-"),
                min_turnover=thresholds.get("min_turnover", "-"),
                min_bars=thresholds.get("min_bars", "-"),
            )
        ),
    ]
    return "\n".join(lines)


def print_console(result: ScanResult, cfg, max_rows: Optional[int] = None) -> str:
    """打印到终端，返回渲染出的文本。"""
    summary = build_summary(result, cfg)
    table = render_table(result.hits, max_rows=max_rows)
    text = f"{summary}\n\n{table}"
    print(text)
    return text


def print_frame(
    result,
    columns: Sequence[str],
    labels: Dict[str, str],
    float_formats: Optional[Dict[str, str]] = None,
    summary: str = "",
    max_rows: Optional[int] = None,
) -> str:
    """终端打印任意结果表（用于指标信号报告）。"""
    table = render_table(
        result.hits,
        columns=columns,
        max_rows=max_rows,
        float_formats=float_formats,
        labels=labels,
    )
    text = f"{summary}\n\n{table}" if summary else table
    print(text)
    return text


def write_files(result, cfg, prefix: str = "volscan", sheet_name: str = "放量命中") -> List[Path]:
    """按配置写出报告文件，返回写出的路径列表。"""
    formats = [str(f).lower() for f in (cfg.get("report.formats") or [])]
    formats = [f for f in formats if f != "console"]
    if not formats:
        return []
    reports_dir: Path = cfg.path("store.reports_dir")
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.trade_date or dt.date.today().strftime("%Y-%m-%d")
    written: List[Path] = []

    if "csv" in formats:
        path = reports_dir / f"{prefix}_{stamp}.csv"
        result.hits.to_csv(path, index=False, encoding="utf-8-sig")
        written.append(path)

    if "xlsx" in formats:
        path = reports_dir / f"{prefix}_{stamp}.xlsx"
        meta = pd.DataFrame(
            [
                {
                    "项目": k,
                    "值": json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v,
                }
                for k, v in {**(result.stats or {}), "trade_date": result.trade_date}.items()
            ]
        )
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            result.hits.to_excel(writer, sheet_name=sheet_name, index=False)
            meta.to_excel(writer, sheet_name="参数与统计", index=False)
        written.append(path)

    if "json" in formats:
        path = reports_dir / f"{prefix}_{stamp}.json"
        payload = {
            "trade_date": result.trade_date,
            "stats": result.stats,
            "hits": json.loads(result.hits.to_json(orient="records", force_ascii=False)),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(path)

    return written
