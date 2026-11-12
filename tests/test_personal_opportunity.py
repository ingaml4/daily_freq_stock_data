import pytest

from emscan.personal_opportunity import build_personal_opportunity_payload
from emscan.store import Store
from helpers import bar, trading_dates


@pytest.fixture
def data(tmp_path):
    dates = trading_dates(65, end='2026-11-11')
    sid = '1.600001'
    with Store(tmp_path / 'stock.sqlite').init_schema() as st:
        st.upsert_symbols([{'secid': sid, 'code': '600001', 'name': '测试', 'market': 1}])
        st.upsert_bars([bar(sid, d, 3000 if i == 64 else 1000,
                            close=11 if i == 64 else 10,
                            pct_chg=10 if i == 64 else 0,
                            turnover=None if i == 64 else 2)
                        for i, d in enumerate(dates)])
        yield st, dates, sid


def test_breakout_has_prior_only_levels_and_unknown_coverage(data):
    st, dates, sid = data
    p = build_personal_opportunity_payload(st.conn, dates[-1])
    assert len(p['candidates']) == 1
    c = p['candidates'][0]
    assert [m['id'] for m in c['modes']] == ['C']
    assert c['modes'][0]['trigger'] == pytest.approx(10.1)
    assert c['history']['end'] == dates[-2]
    assert c['bar']['turnover'] is None and p['coverage']['turnover_unknown'] == 1
    assert c['macd']['state'] == 'provisional_intraday_or_unverified'
    assert c['mainline_status'] == '未确认，待核'


def test_future_bars_do_not_change_historical_candidate(data):
    st, dates, sid = data
    before = build_personal_opportunity_payload(st.conn, dates[-1])
    st.upsert_bars([bar(sid, '2026-11-12', 9999, close=99)])
    after = build_personal_opportunity_payload(st.conn, dates[-1])
    assert before == after


def test_missing_day_does_not_substitute_previous_day(data):
    st, dates, _ = data
    p = build_personal_opportunity_payload(st.conn, '2026-11-12')
    assert p['status'] == 'waiting' and p['candidates'] == []
    assert not p['coverage']['requested_day_present']


def test_date_validation(data):
    st, _, _ = data
    with pytest.raises(ValueError):
        build_personal_opportunity_payload(st.conn, '2026-1-1')

def test_mainline_modes_distinguish_anchor_and_recovery(data, monkeypatch):
    st, dates, sid = data
    from emscan import personal_opportunity as module

    def fake_report(store, day, use_archive=False):
        mapping = {'source': 'fixture', 'observed_at': dates[0] + ' 15:30:00'}
        return {'date': day, 'publication_ready': True,
                'market': {'ready': True, 'after_close_snapshot': True, 'observed_at': day + ' 15:30:00'},
                'gaps': [], 'mappings': {'sw': mapping},
                'mainlines': [{'key': 'sw:test', 'kind': 'sw', 'name': '测试板块',
                               'evidence': {'sample_secids': [sid]},
                               'anchors': [{'secid': sid, 'role': 'leader'}]}]}
    monkeypatch.setattr(module.market_mainline, 'report', fake_report)
    p = build_personal_opportunity_payload(st.conn, dates[-1])
    assert 'A' in [x['id'] for x in p['candidates'][0]['modes']]
    assert p['candidates'][0]['macd']['state'] == 'confirmed_daily'
    with st.conn:
        st.conn.execute('UPDATE bars SET high=11.2 WHERE secid=? AND date=?', (sid, dates[-15]))
    st.upsert_bars([bar(sid, dates[-1], 1000, close=10.5, pct_chg=5)])
    p = build_personal_opportunity_payload(st.conn, dates[-1])
    assert [x['id'] for x in p['candidates'][0]['modes']] == ['B']
    assert p['candidates'][0]['modes'][0]['trigger'] == pytest.approx  (10.1)
