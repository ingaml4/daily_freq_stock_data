"""Read-only quality diagnostics; never rewrites original reports or historical bars."""


def turnover_quality(conn, day):
    row = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN turnover IS NULL OR turnover<=0 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN turnover=0 THEN 1 ELSE 0 END) "
        "FROM bars WHERE date=? AND close>0 AND volume>0", (day,)
    ).fetchone()
    active, unknown, zero = (int(value or 0) for value in row)
    return {
        "date": day,
        "status": "waiting" if not active else "degraded" if unknown else "ready",
        "active_bars": active,
        "turnover_known": active - unknown,
        "turnover_unknown_active": unknown,
        "turnover_zero_with_volume": zero,
        "original_reports_preserved": True,
        "note": (
            "正成交而换手缺失或为零的记录待核，不能按低换手判定。放量和箱体数量只代表已确认命中，不能据此推断市场缺乏机会。"
            if unknown else "暂无成交行情" if not active else "同日有成交记录均有正换手率，数值准确性仍依赖行情来源。"
        ),
    }
