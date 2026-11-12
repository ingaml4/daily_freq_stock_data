import json
from datetime import date, datetime, timedelta

import pytest

from emscan.minute_replay import replay


def bar(label, price=2.39, shares=100):
    start = datetime.fromisoformat(f"2026-11-12T{label}:00+08:00")
    return {
        "label": start.isoformat(),
        "end": (start + timedelta(minutes=1)).isoformat(),
        "price": price,
        "cum_shares": shares,
        "raw": {
            "timestamp": int(start.timestamp() * 1000),
            "current": price,
            "volume_total": shares,
        },
    }


def packet(seen, *bars):
    return json.dumps(
        {"frames": [{"symbol": "SZ002021", "seen_at": f"2026-11-12T{seen}+08:00", "bars": bars}]}
    )


def check(*lines, span="09:42-09:43"):
    return replay(
        lines,
        symbol="SZ002021",
        day=date(2026, 11, 12),
        window="09:30-11:00",
        spans=[span],
    )["spans"][0]


def test_replay_uses_first_closed_snapshot_and_detects_later_revision():
    prior = [bar(f"09:{m:02d}", shares=100 * (m - 36)) for m in range(37, 43)]
    target = bar("09:43", price=2.44, shares=1000)
    result = check(
        packet("09:44:20", *prior, bar("09:43", price=2.45, shares=1020)),
        packet("09:43:59", *prior, target),
        packet("09:44:11", *prior, target),
    )
    assert result["seconds_after_assumed_end"] == 11
    assert result["price_change_pct"] == pytest.approx(2.09205)
    assert result["last_minute_shares"] == 400
    assert result["last_minute_multiple"] == 4
    assert result["target_changed_observations"] == 1


@pytest.mark.parametrize(
    "line",
    [
        packet("09:44:11", bar("09:42"), bar("09:43"), bar("09:43", price=2.44)),
        packet("09:45:01", bar("09:42"), bar("09:43")),
        packet("09:44:11", bar("09:43")),
    ],
)
def test_conflicting_stale_and_missing_bars_do_not_make_comparable_span(line):
    assert check(line)["status"] == "gap"


def test_cumulative_reset_preserves_price_but_excludes_volume():
    result = check(packet("09:44:11", bar("09:42"), bar("09:43", price=2.44, shares=10)))
    assert result["status"] == "observed"
    assert result["price_change_pct"] == pytest.approx(2.09205)
    assert result["last_minute_shares"] is result["last_minute_multiple"] is None


def test_end_of_monitor_window_can_finish_last_bar_within_grace():
    line = packet("11:00:13", bar("10:58"), bar("10:59"))
    assert check(line, span="10:58-10:59")["status"] == "observed"
    with pytest.raises(ValueError, match="before cutoff"):
        check(line, span="10:59-11:00")
