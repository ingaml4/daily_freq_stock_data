from emscan.data_quality import turnover_quality
from emscan.store import Store


def test_unknown_turnover_is_separate_from_real_zero_and_does_not_mutate(tmp_path):
    with Store(tmp_path / "market.sqlite").init_schema() as store:
        rows = [("1.600001", 100, 0), ("1.600002", 100, None),
                ("1.600003", 100, 0.1), ("1.600004", 0, 0)]
        store.conn.executemany(
            "INSERT INTO bars(secid,date,close,volume,turnover) VALUES (?, '2026-11-11',10,?,?)", rows)
        before = list(store.conn.execute("SELECT turnover FROM bars ORDER BY secid"))
        result = turnover_quality(store.conn, "2026-11-11")
        assert result["active_bars"] == 3
        assert result["turnover_unknown_active"] == 2
        assert result["turnover_zero_with_volume"] == 1
        assert result["status"] == "degraded"
        assert before == list(store.conn.execute("SELECT turnover FROM bars ORDER BY secid"))
        assert turnover_quality(store.conn, "2026-11-12")["status"] == "waiting"
