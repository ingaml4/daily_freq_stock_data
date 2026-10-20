"""按钮口径（一个来源，网页和复盘共用）。

网页上每个按钮鼠标悬浮看到的"筛选口径"、复盘喂给 AI 的口径说明、以及"放量大涨"
这类**视图级过滤**都从这里取——曾经两边各写一份，结果复盘的"放量大涨"用的是信号缓存里的
原始放量标记、没带"当日涨幅 ≥ 3%"这个门槛，AI 就在名单里看到了放量下跌的票
（2026-10-14 用户发现）。
"""

from __future__ import annotations

import re
from typing import Dict, Sequence

import pandas as pd


def vol_up_min_pct(cfg) -> float:
    """放量大涨视图的涨幅门槛（%），默认 3。"""
    return float(cfg.get("scan.vol_up_min_pct", 3.0) or 3.0)


def vol_up_mask(frame: pd.DataFrame, cfg) -> pd.Series:
    """放量大涨视图 = 信号命中 **且** 当日涨幅 ≥ 门槛（放量下跌、放量滞涨不算）。

    只做"涨幅"这一层过滤；信号本身（量比/成交额/换手）由 ``s_vol_spike`` 列承载。
    网页端在 SQL 里做同样的过滤（``COALESCE(pct_chg, -999) >= ?``），口径保持一致。
    """
    if frame is None or len(frame) == 0:
        return pd.Series([], dtype=bool)
    if "pct_chg" not in frame.columns:
        return pd.Series([True] * len(frame), index=frame.index, dtype=bool)
    pct = pd.to_numeric(frame["pct_chg"], errors="coerce").fillna(-999.0)
    return pct >= vol_up_min_pct(cfg)


def _help_block(title: str, items: Sequence[str], note: str = "") -> str:
    body = "".join(f"<li>{item}</li>" for item in items)
    tail = f'<p class="help-note">{note}</p>' if note else ""
    return f'<div class="help-title">{title}</div><ul>{body}</ul>{tail}'


def build_view_help(cfg) -> Dict[str, str]:
    """生成每个按钮的"筛选口径"（数值取自 config，改配置就会跟着变）。

    返回可读的 HTML 片段：网页里是「标题 + 要点列表」的小弹层；
    ``plain_help`` 会再压成纯文本喂给 AI。
    """
    g = cfg.get
    vol_up = g("scan.vol_up_min_pct", 3.0)
    box_w = g("box.window", 40)
    box_months = float(box_w or 40) / 20
    rev_dd = round(abs(float(g("reversal.max_drawdown_from_high", -0.15) or -0.15)) * 100, 2)
    rev_p60 = round(float(g("reversal.max_pos60", 0.35) or 0.35) * 100, 2)
    rev_d20 = round(float(g("reversal.min_drop20", 0.15) or 0.15) * 100, 2)
    doji_dd = round(abs(float(g("doji.max_drawdown_from_high", -0.20) or -0.20)) * 100, 2)
    return {
        "vol_spike": _help_block(
            "放量大涨",
            [
                f"量比5 ≥ {g('scan.vol_ratio_5_min', 2.0)}，"
                f"且（量比20 ≥ {g('scan.vol_ratio_20_min', 1.5)} "
                f"或 Z60 ≥ {g('scan.z_min', 3.0)}）",
                f"成交额 ≥ {round(float(g('scan.min_amount', 0) or 0) / 10000)} 万元，"
                f"换手率 ≥ {g('scan.min_turnover', 0.5)}%，"
                f"至少 {g('universe.min_bars', 25)} 根 K 线",
                "剔除 ST / 停牌",
                f"只显示当日涨幅 ≥ {vol_up}% 的股票（放量下跌、放量滞涨不在此视图）",
            ],
            "完整名单可用 <code>emscan hits --only vol_spike</code> 查看。",
        ),
        "macd_golden": _help_block(
            "MACD 金叉",
            [
                "日线 DIFF 上穿 DEA（昨日 DIFF ≤ DEA，今日 DIFF > DEA）",
                "使用前复权价计算；表格同时给出 DIFF / DEA / 柱值",
            ],
        ),
        "kdj_golden": _help_block(
            "KDJ 金叉",
            [
                "K 上穿 D（参数 9,3,3；昨日 K ≤ D，今日 K > D）",
                "表格给出 K / D / J；J 越极端，短线越超买或超卖",
            ],
        ),
        "boll_break_lower": _help_block(
            "布林下轨突破",
            [
                "当日收盘 ≤ 布林下轨（BOLL 20 日 ± 2σ）",
                "表格给出上轨 / 中轨 / 下轨与布林位置（0 = 贴下轨）",
            ],
        ),
        "box_range": _help_block(
            f"箱体做T（{box_w} 个交易日 ≈ {box_months:.0f} 个月）",
            [
                f"箱体高度 {float(g('box.min_width', 0.06) or 0.06) * 100:.0f}% ~ "
                f"{float(g('box.max_width', 0.35) or 0.35) * 100:.0f}%，"
                f"净涨跌 ≤ ±{float(g('box.max_net_chg', 0.12) or 0.12) * 100:.0f}%",
                f"|MA5 − MA20| / MA20 ≤ "
                f"{float(g('box.max_ma_gap', 0.04) or 0.04) * 100:.0f}%",
                f"日均换手 {g('box.min_avg_turnover', 1.0)}% ~ "
                f"{g('box.max_avg_turnover', 15.0)}%，"
                f"换手变异系数 ≤ {g('box.max_turnover_cv', 1.0)}",
                f"日均振幅 {g('box.min_avg_amplitude', 2.0)}% ~ "
                f"{g('box.max_avg_amplitude', 9.0)}%，"
                f"|涨跌| ≥ 9.5% 的天数 ≤ {g('box.max_extreme_days', 2)}",
                f"成交额 ≥ {round(float(g('box.min_amount', 0) or 0) / 10000)} 万元，"
                f"有效交易日 ≥ {g('box.min_valid_days', 35)}",
                "按箱体位置升序（0 = 贴箱底），方便高抛低吸",
            ],
        ),
        "reversal": _help_block(
            "底部反转（评分制）",
            [
                f"底部上下文（至少一条）：距 {g('reversal.drawdown_window', 120)} 日高点"
                f"回撤 ≥ {rev_dd}%，或 60 日区间位置 ≤ {rev_p60}%，"
                f"或 20 日跌幅 ≥ {rev_d20}%",
                f"底部放量大涨 +25（量比 ≥ {g('reversal.surge_vol_ratio', 1.8)}、"
                f"涨幅 ≥ {g('reversal.surge_pct_chg', 4.0)}%）",
                f"缩量止跌 +20（量 ≤ {g('reversal.shrink_ratio', 0.6)}× 均量、"
                "涨跌幅 -1.5% ~ 2.0%）",
                "长下影 +15、十字星 +10、收阳且收在振幅上半部 +10、"
                "20 日极低位 +10、60 日低位 +10",
                f"分数 ≥ {g('reversal.min_score', 55)} 且至少有一条量价证据；"
                "按反转分降序",
            ],
        ),
        "low_doji": _help_block(
            "缩量止跌",
            [
                f"大幅回撤：距 {g('doji.drawdown_window', 120)} 日高点回撤 ≥ {doji_dd}%",
                f"突然缩量：当日量 ≤ 前 {g('doji.shrink_window', 5)} 日均量 × "
                f"{g('doji.shrink_ratio', 0.6)}",
                f"当日止跌：涨跌幅 {g('doji.min_pct_chg', -1.5)}% ~ "
                f"{g('doji.max_pct_chg', 3.0)}%，"
                f"收盘位置 ≥ {g('doji.close_off_low_ratio', 0.35)}",
                f"成交额 ≥ {round(float(g('doji.min_amount', 0) or 0) / 10000)} 万，"
                f"换手 ≤ {g('doji.max_turnover', 5.0)}%，"
                f"至少 {g('doji.min_bars', 45)} 根 K 线，非 ST",
                "形态（小实体 / 长下影）默认不作条件",
            ],
        ),
    }


def plain_help(text: str) -> str:
    """把口径说明转成纯文本（网页按钮的悬浮提示、喂给 AI 的口径说明都用它）。"""
    text = re.sub(r"<br\s*/?>", " / ", text)
    text = re.sub(r"</li\s*>", "；", text)
    text = re.sub(r"</p\s*>", "。", text)
    text = re.sub(r"</div\s*>", "：", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ；。")


def view_criteria(cfg) -> Dict[str, str]:
    """按钮口径的纯文本版，键是视图名（vol_spike / macd_golden / …）。"""
    return {key: plain_help(text) for key, text in build_view_help(cfg).items()}
