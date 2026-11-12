"""Point-in-time daily opportunity preparation, not validated trading signals."""
from collections import defaultdict
from math import isfinite
from statistics import median

from . import market_mainline


class _StoreView:
    def __init__(self, conn):
        self.conn = conn

    def get_meta(self, key):
        row = self.conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def latest_date(self):
        return self.conn.execute('SELECT MAX(date) FROM bars').fetchone()[0]


def _number(value, positive=False):
    return type(value) in (int, float) and isfinite(value) and (not positive or value > 0)


def _macd(closes):
    # EMA seeded at the first available close; minimum history limits warm-up bias.
    fast = slow = closes[0]
    dea = 0.0
    hist = []
    diffs = []
    for close in closes:
        fast += 2 / 13 * (close - fast)
        slow += 2 / 27 * (close - slow)
        diff = fast - slow
        dea += 2 / 10 * (diff - dea)
        diffs.append(diff)
        hist.append(2 * (diff - dea))
    return diffs, hist, dea


def build_personal_opportunity_payload(conn, date: str):
    if not isinstance(date, str) or __import__('datetime').date.fromisoformat(date).isoformat() != date:
        raise ValueError('date must be YYYY-MM-DD')
    conn.execute('SAVEPOINT personal_opportunity_read')
    try:
        return _build(conn, date)
    finally:
        conn.execute('RELEASE personal_opportunity_read')


def _build(conn, day):
    store = _StoreView(conn)
    main = market_mainline.report(store, day, use_archive=False)
    prev_day = conn.execute('SELECT MAX(date) FROM trading_days WHERE date<?', (day,)).fetchone()[0]
    prev = market_mainline.report(store, prev_day, use_archive=False) if prev_day else None
    # Only selected, publication-ready lines can confer a mainline label.
    line_members = defaultdict(list)
    for report, label in ((main, '当日'), (prev, '前一交易日')):
        if not report or not report['publication_ready']:
            continue
        for line in report['mainlines']:
            for sid in line['evidence']['sample_secids']:
                line_members[sid].append({'key': line['key'], 'name': line['name'],
                    'date': report['date'], 'phase': label,
                    'membership_source': report['mappings'][line['kind']],
                    'anchor_roles': [a['role'] for a in line['anchors'] if a['secid'] == sid]})

    days = [r[0] for r in conn.execute('SELECT date FROM trading_days WHERE date<=? ORDER BY date DESC LIMIT 90', (day,))]
    rows = defaultdict(list)
    if days and days[0] == day:
        for row in conn.execute(
            'SELECT b.*,s.code,s.name,s.is_st,s.listed_date,s.updated_at AS symbol_updated_at '
            'FROM bars b LEFT JOIN symbols s ON s.secid=b.secid '
            'WHERE b.date>=? AND b.date<=? ORDER BY b.secid,b.date', (days[-1], day)):
            if all(_number(row[k], True) for k in ('close', 'high', 'low', 'volume', 'amount')) and row['low'] <= row['close'] <= row['high']:
                rows[row['secid']].append(row)
    candidates = []
    today_rows = [v[-1] for v in rows.values() if v and v[-1]['date'] == day]
    finalized = bool(main['market'] and main['market']['after_close_snapshot'] and main['market']['ready'])
    for sid, bars in rows.items():
        if len(bars) < 61 or bars[-1]['date'] != day:
            continue
        cur, history = bars[-1], bars[-61:-1]
        if not cur['code'] or not cur['name'] or cur['is_st'] != 0 or (cur['listed_date'] and cur['listed_date'] > day):
            continue
        if sid.startswith(('1.688', '1.689')):
            continue
        last20 = history[-20:]
        last5 = history[-5:]
        high20 = max(r['high'] for r in last20)
        high5 = max(r['high'] for r in last5)
        low5 = min(r['low'] for r in last5)
        high60 = max(r['high'] for r in history)
        low60 = min(r['low'] for r in history)
        vol_ratio = cur['volume'] / median(r['volume'] for r in last20)
        closes = [r['close'] for r in bars]
        diffs, hist, dea = _macd(closes)
        improving = hist[-1] > hist[-2] and diffs[-1] > diffs[-2]
        lines = line_members.get(sid, [])
        current_lines = [x for x in lines if x['date'] == day]
        prior_lines = [x for x in lines if x['date'] == prev_day]
        pct = cur['pct_chg'] if _number(cur['pct_chg']) else (cur['close'] / history[-1]['close'] - 1) * 100
        modes = []
        if any(set(x['anchor_roles']) & {'leader', 'turnover_core'} for x in current_lines) and cur['close'] >= high20 * .95 and pct >= 2 and cur['close'] >= high5:
            modes.append({'id': 'A', 'name': '主线龙头主动转强或强势延续', 'trigger': high5, 'risk': low5,
                          'basis': '当日已确认主线的涨幅领涨或成交额核心；收盘不低于前20日高点95%且收复前5日高点，涨幅至少2%；龙头地位还需盘中联动验证'} )
        if (current_lines or prior_lines) and .85 <= history[-1]['close'] / high20 <= .97 and cur['close'] >= high5 and pct >= 2:
            modes.append({'id': 'B', 'name': '主线调整后重新转强', 'trigger': high5, 'risk': low5,
                          'basis': '当日或前一交易日已确认主线成分；前收较前20日高点回落3%至15%，当日收复前5日高点且涨幅至少2%'})
        if high60 / low60 <= 1.45 and cur['close'] >= high60 and vol_ratio >= 1.5 and improving:
            modes.append({'id': 'C', 'name': '长期横盘放量启动与MACD结构改善', 'trigger': high60, 'risk': low5,
                          'basis': '前60根日线高低区间不超过45%，突破其高点，成交量至少前20日中位数1.5倍，DIF及柱值均较前日改善'})
        if not modes:
            continue
        candidates.append({'secid': sid, 'symbol': ('SH' if sid.startswith('1.') else 'SZ') + cur['code'],
            'name': cur['name'], 'modes': modes, 'mainlines': lines,
            'mainline_status': '已确认成分' if lines else '未确认，待核',
            'bar': {'date': day, 'close': cur['close'], 'low': cur['low'], 'pct_chg': pct,
                    'volume_ratio_20': vol_ratio, 'turnover': cur['turnover'] if _number(cur['turnover'], True) else None,
                    'source': cur['source'] or 'unknown', 'observed_at': main['market']['observed_at'] if main['market'] else None},
            'macd': {'dif': diffs[-1], 'dea': dea, 'histogram': hist[-1], 'previous_histogram': hist[-2],
                     'structure_improving': improving, 'state': 'confirmed_daily' if finalized else 'provisional_intraday_or_unverified',
                     'calculation': 'EMA(12,26,9), first available close seed; histogram=2*(DIF-DEA)', 'history_start': bars[0]['date']},
            'history': {'start': history[0]['date'], 'end': history[-1]['date'], 'count': len(history),
                        'prior_20_high': high20, 'prior_5_high': high5, 'prior_60_high': high60, 'prior_60_low': low60},
            'pending_validation': ['核对行情与主线成分时点及覆盖', '观察能否站稳触发位及板块联动', '跌破风险位则重新评估；数值不是止损指令'],
            'symbol_metadata_updated_at': cur['symbol_updated_at']})
    candidates.sort(key=lambda x: x['secid'])
    return {'schema_version': '1.0', 'date': day,
            'purpose': '未经回测的日频机会准备候选，不是已验证荐买信号；A、B、C并列，不按涨幅排序。',
            'status': 'prepared' if today_rows else 'waiting', 'candidates': candidates,
            'coverage': {'valid_current_bars': len(today_rows), 'market': main['market'],
                         'turnover_unknown': sum(not _number(r['turnover'], True) for r in today_rows),
                         'history_session_count': len(days), 'requested_day_present': bool(days and days[0] == day)},
            'mainline': {'date': day, 'publication_ready': main['publication_ready'], 'gaps': main['gaps'],
                         'mappings': main['mappings'], 'previous_date': prev_day,
                         'previous_publication_ready': bool(prev and prev['publication_ready'])},
            'limitations': ['日线与证券资料可能被后续覆盖，无法证明历史原始时点快照；证券名称、ST状态及上市资料无完整历史版本。',
                            '行情来源时间采用覆盖任务完成时间，单条日线采集时间未知；无覆盖记录时为未知。',
                            '样本需至少60根有效历史日线，缺失或停牌可能使观察窗口跨越更多交易日。',
                            '日线指标由可用收盘价计算，盘中或覆盖未确认时仅为暂态；未经过回测。']}
