"""按钮口径（一个来源，网页和复盘共用）。

网页上每个按钮鼠标悬浮看到的"筛选口径"、复盘喂给 AI 的口径说明、以及"放量大涨"
这类**视图级过滤**都从这里取——曾经两边各写一份，结果复盘的"放量大涨"用的是信号缓存里的
原始放量标记、没带"当日涨幅 ≥ 3%"这个门槛，AI 就在名单里看到了放量下跌的票
（2026-10-14 用户发现）。
"""

from __future__ import annotations

import re
from typing import Dict

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


def build_view_help(cfg) -> Dict[str, str]:
    """生成每个按钮的"筛选口径"说明（数值取自 config，改配置就会跟着变）。"""
    g = cfg.get
    help_text = {
        "vol_spike": (
            "放量命中：量比5 ≥ {v5} 且（量比20 ≥ {v20} 或 Z60 ≥ {z}）；"
            "当日成交额 ≥ {amt} 万元、换手率 ≥ {to}%、至少 {mb} 根K线、非ST/停牌。<br>"
            "<b>本按钮只显示当日涨幅 ≥ {up}% 的</b>（放量下跌、放量滞涨不在这里显示；"
            "完整名单见 `python3 -m emscan hits --only vol_spike`）。"
        ).format(
            v5=g("scan.vol_ratio_5_min", 2.0),
            v20=g("scan.vol_ratio_20_min", 1.5),
            z=g("scan.z_min", 3.0),
            amt=round(float(g("scan.min_amount", 0) or 0) / 10000),
            to=g("scan.min_turnover", 0.5),
            mb=g("universe.min_bars", 25),
            up=g("scan.vol_up_min_pct", 3.0),
        ),
        "macd_golden": "MACD 金叉：日线 DIFF 上穿 DEA（昨天 DIFF ≤ DEA，今天 DIFF > DEA），"
        "用前复权价算。报告里同时给出 DIFF/DEA/柱值。",
        "kdj_golden": "KDJ 金叉：K 上穿 D（参数 9,3,3；昨天 K ≤ D，今天 K > D）。"
        "报告里带 K/D/J 三个值，J 越极端说明短线越超买/超卖。",
        "boll_break_lower": "BOLL 下轨突破：当日<b>收盘 ≤ 布林下轨</b>（BOLL 20 日 ±2σ）。"
        "报告里给出上/中/下轨和「布林位置」（0=贴下轨）。",
        "box_range": (
            "箱体震荡 + 换手健康（{w} 个交易日 ≈ {m:.0f} 个月）：箱体高度 {w1}~{w2}、"
            "净涨跌 ≤ ±{net}、|MA5-MA20|/MA20 ≤ {gap}；日均换手 {t1}~{t2}% 且变异系数 ≤ {cv}；"
            "日均振幅 {a1}~{a2}%；|涨跌|≥9.5% 天数 ≤ {ex}；成交额 ≥ {amt} 万；"
            "有效交易日 ≥ {days}、非ST。<br>按<b>箱体位置升序</b>（0=贴箱底）排，方便高抛低吸。"
        ).format(
            w=g("box.window", 40),
            m=float(g("box.window", 40) or 40) / 20,
            w1=g("box.min_width", 0.06),
            w2=g("box.max_width", 0.35),
            net=g("box.max_net_chg", 0.12),
            gap=g("box.max_ma_gap", 0.04),
            t1=g("box.min_avg_turnover", 1.0),
            t2=g("box.max_avg_turnover", 15.0),
            cv=g("box.max_turnover_cv", 1.0),
            a1=g("box.min_avg_amplitude", 2.0),
            a2=g("box.max_avg_amplitude", 9.0),
            ex=g("box.max_extreme_days", 2),
            amt=round(float(g("box.min_amount", 0) or 0) / 10000),
            days=g("box.min_valid_days", 35),
        ),
        "reversal": (
            "<b>底部反转信号（评分制）</b>：先判断「是不是在底部区域」，再看有几条积极证据叠加，"
            "分越高越强。<br>"
            "① <b>底部上下文</b>（至少一条）：距 {dw} 个交易日高点回撤 ≥ {dd}、"
            "或 60 日区间位置 ≤ {p60}、或 20 日跌幅 ≥ {d20}；<br>"
            "② <b>证据与权重</b>：底部放量大涨（量≥{sv}× 且涨幅≥{sp}%）+25、"
            "缩量止跌（量≤{sr}× 且 涨跌幅 {lo}~{hi}%）+20、长下影 +15、十字星 +10、"
            "收阳且收在振幅上半部 +10、20 日极低位 +10、60 日低位 +10；<br>"
            "③ 分数 ≥ {ms} 且有「放量/缩量」这类量价证据才算命中；按反转分降序排。"
        ).format(
            dw=g("reversal.drawdown_window", 120),
            dd=g("reversal.max_drawdown_from_high", -0.15),
            p60=g("reversal.max_pos60", 0.35),
            d20=g("reversal.min_drop20", 0.15),
            sv=g("reversal.surge_vol_ratio", 1.8),
            sp=g("reversal.surge_pct_chg", 4.0),
            sr=g("reversal.shrink_ratio", 0.6),
            lo=-1.5,
            hi=2.0,
            ms=g("reversal.min_score", 55),
        ),
        "low_doji": (
            "<b>缩量止跌</b>（三条同时满足）：<br>"
            "① <b>大幅回撤</b>：距最近 {w} 个交易日高点回撤 ≥ {dd}；<br>"
            "② <b>突然缩量</b>：当日量 ≤ 之前 {sw} 个交易日均量 × {sr}；<br>"
            "③ <b>当日止跌</b>：涨跌幅在 {lo}% ~ {hi}% 之间（微红/微跌都算，大涨不算），"
            "且收盘不在当日最低（收盘位置 ≥ {pos}）。<br>"
            "另外：成交额 ≥ {amt} 万、换手 ≤ {to}%、≥{mb} 根K线、非ST；"
            "形态（小实体/长下影）默认不作条件。"
        ).format(
            w=g("doji.drawdown_window", 120),
            pos=g("doji.close_off_low_ratio", 0.35),
            dd=g("doji.max_drawdown_from_high", -0.20),
            lo=g("doji.min_pct_chg", -1.5),
            hi=g("doji.max_pct_chg", 3.0),
            sw=g("doji.shrink_window", 5),
            sr=g("doji.shrink_ratio", 0.6),
            amt=round(float(g("doji.min_amount", 0) or 0) / 10000),
            to=g("doji.max_turnover", 5.0),
            mb=g("doji.min_bars", 45),
        ),
    }
    return help_text


def plain_help(text: str) -> str:
    """把口径说明转成纯文本（网页按钮的悬浮提示、喂给 AI 的口径说明都用它）。"""
    text = text.replace("<br>", " / ").replace("<b>", "").replace("</b>", "")
    return re.sub(r"<[^>]+>", "", text)


def view_criteria(cfg) -> Dict[str, str]:
    """按钮口径的纯文本版，键是视图名（vol_spike / macd_golden / …）。"""
    return {key: plain_help(text) for key, text in build_view_help(cfg).items()}
