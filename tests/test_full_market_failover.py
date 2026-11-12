import datetime as dt
import gzip
import http.server
import importlib.util
import json
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).with_name('full-market-watch.py')
if not SCRIPT.exists():
    SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'full-market-watch.py'
SPEC = importlib.util.spec_from_file_location('full_market_watch', SCRIPT)
market = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(market)


def stamp(value):
    return dt.datetime.fromisoformat(value + '+08:00')


def identity(code, name='Example'):
    return dict(f12=code[2:], f13=int(code.startswith('SH')), f14=name)


def page_rows(count, start=0):
    return [identity(f'SZ{i:06d}') for i in range(start, start + count)]


def quote_line(code, quoted_at, name='Example', price=11, volume=50000, amount=55_000_000):
    fields = ['0'] * 38
    fields[1:6] = [name, code[2:], str(price), '10', '10']
    fields[30] = quoted_at.strftime('%Y%m%d%H%M%S')
    fields[33:37] = ['11', '10', f'{price}/{volume}/{amount}', str(volume)]
    return f'v_{code.lower()}="{"~".join(fields)}";'


class FullMarketFailover(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = pathlib.Path(self.directory.name)
        self.at = stamp('2026-11-13T09:32:00')
        for target, kwargs in [('now', {'side_effect': lambda: self.at}), ('DEADLINE', {'new': None})]:
            mock = patch.object(market, target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def saved_list(self, rows, observed, legacy=False, suffix=None):
        pages = {str(i//100 + 1): rows[i:i+100] for i in range(0, len(rows), 100)}
        coverage = dict(full_pages=True, first_page_stable=True, duplicate_count=0, totals=[len(rows)],
                        reported_total=len(rows), pages_expected=len(pages), pages_fetched=len(pages))
        coverage['coverage_valid' if legacy else 'pagination_verified'] = True
        data = dict(seen_at=observed.isoformat(), coverage=coverage, pages=pages)
        filename = f'full-market-universe-{observed:%Y-%m-%d}-{suffix or observed.strftime("%H%M%S")}.json'
        path = self.folder / filename
        market.save(path, data)
        return path, data

    def sector(self, codes):
        market.save(self.folder / f'sector-discovery-{self.at.date()}.json',
                    dict(account_id='3786324279', date=self.at.date().isoformat(), sectors={'Gas': codes}))

    def feed(self):
        feed = market.UniverseFeed(self.folder, self.at.date())
        self.addCleanup(feed.close)
        return feed

    def raw(self, result):
        with gzip.open(self.folder / result['raw_file'], 'rt', encoding='utf8') as data:
            return [json.loads(line) for line in data]

    def test_cross_day_cache_is_identity_only_and_sector_union_is_quoted(self):
        old = identity('SH600000', 'ST_old') | {'f2': 9090, 'f6': 1234567}
        self.saved_list([old, identity('SH688001')], stamp('2026-11-12T10:01:00'), legacy=True)
        self.sector(['SH600000', 'SH688001', 'SZ302132', 'BJ920010', 'SH900913'])
        requested = []

        def request(url, *_):
            if url.startswith(market.BASE):
                raise urllib.error.HTTPError(url, 502, 'test', None, None)
            codes = url.split('=', 1)[1].upper().split(',')
            requested.extend(codes)
            return '\n'.join(quote_line(code, self.at - dt.timedelta(seconds=1)) for code in codes).encode('gbk')

        feed = self.feed()
        with patch.object(market, 'request', side_effect=request):
            pool, _ = feed.current(wait=True)
            result = market.run(self.folder, feed, {})
        self.assertEqual(pool['SH600000'], identity('SH600000', 'ST_old'))
        self.assertEqual(set(requested), {'SH600000', 'SZ302132', 'BJ920010', 'SH900913'})
        self.assertEqual(result['universe']['list_date'], '2026-11-12')
        self.assertEqual(result['universe']['list_age_days'], 1)
        self.assertFalse(result['universe']['current_day_membership_verified'])
        self.assertEqual(result['universe']['universe_refresh_error'], 'HTTP_502')
        self.assertEqual(result['health'], 'degraded')
        self.assertEqual(result['quote_health'], 'available')
        rows = {row['symbol']: row for row in result['sector_review']}
        self.assertEqual(rows['SH688001']['reason'], 'permission_excluded_688')
        self.assertTrue(rows['SZ302132']['missing_from_universe'])
        self.assertEqual(rows['SZ302132']['quote']['quote_source'], market.TENCENT_SOURCE)
        self.assertEqual(rows['SH600000']['quote']['price'], 11)
        self.assertNotIn('ST', rows['SH600000']['quote']['exclusions'])
        self.assertEqual(rows['SH900913']['quote']['price_currency'], 'USD')
        self.assertIsNone(rows['SH900913']['quote']['amount_yuan'])
        self.assertFalse(rows['SH900913']['quote']['amount_currency_verified'])

    def test_cache_rejects_inconsistent_and_expired_snapshots(self):
        self.saved_list([identity('SH600001')], stamp('2026-11-05T10:00:00'))
        self.assertIsNone(market.seed_verified_universe(self.folder, self.at.date()))
        valid_path, _ = self.saved_list([identity('SH600002')], stamp('2026-11-06T10:00:00'))
        self.saved_list([identity('SH600003')], stamp('2026-11-13T09:33:00'))
        self.saved_list([identity('SH600004')] * 2, stamp('2026-11-12T11:00:00'))
        path, data = self.saved_list(page_rows(101), stamp('2026-11-12T10:00:00'))
        data['pages']['03'] = data['pages'].pop('2')
        market.save(path, data)
        path, data = self.saved_list([identity('SH600005')], stamp('2026-11-12T09:30:00'))
        data['coverage']['first_page_stable'] = False
        market.save(path, data)
        path, data = self.saved_list([identity('SH600006')], stamp('2026-11-12T09:31:00'))
        data['pages']['1'][0]['f13'] = True
        market.save(path, data)
        valid = market.seed_verified_universe(self.folder, self.at.date())
        self.assertEqual(set(valid[1]), {'SH600002'})
        valid_path.unlink()
        self.assertIsNone(market.seed_verified_universe(self.folder, self.at.date()))

    def test_failures_cool_down_and_only_full_recovery_replaces_list(self):
        old_path, _ = self.saved_list([identity('SH600000')], stamp('2026-11-12T10:00:00'))
        feed = self.feed()
        problems = [(403, 15), (429, 15), (502, 5), (TimeoutError('timeout'), 5),
                    (RuntimeError('legacy_request_failure'), 5), (b'invalid JSON', 5)]
        for problem, minutes in problems:
            with self.subTest(problem=problem):
                if isinstance(problem, int):
                    problem = urllib.error.HTTPError(market.BASE, problem, 'test', None, None)
                with patch.object(market, 'request', side_effect=problem if isinstance(problem, Exception) else None,
                                  return_value=problem) as request:
                    pool, coverage = feed.current(wait=True)
                    self.assertEqual(set(pool), {'SH600000'})
                    self.assertEqual(coverage['coverage_state'], 'degraded')
                    self.assertEqual(feed.next_retry, self.at + dt.timedelta(minutes=minutes))
                    feed.current(wait=True)
                    self.assertEqual(request.call_count, 1)
                self.at = feed.next_retry

        first = page_rows(100)
        for reported, final, repeated in [(102, page_rows(1, 100), first), (101, [], first),
                                          (101, page_rows(1), first), (101, page_rows(1, 100), list(reversed(first)))]:
            with self.subTest(reported=reported, final=final, first_stable=repeated == first):
                with patch.object(market, 'em_page', side_effect=[(1, 101, first), (2, reported, final), (1, 101, repeated)]):
                    pool, coverage = feed.current(wait=True)
                self.assertEqual(set(pool), {'SH600000'})
                self.assertEqual(coverage['coverage_state'], 'degraded')
                self.assertEqual(list(self.folder.glob('full-market-universe-*.json')), [old_path])
                self.at = feed.next_retry

        with patch.object(market, 'em_page', side_effect=[(1, 101, first), (2, 101, page_rows(1, 100)), (1, 101, first)]) as page:
            pool, coverage = feed.current(wait=True)
        self.assertEqual([call.args[0] for call in page.call_args_list], [1, 2, 1])
        self.assertEqual(len(pool), 101)
        self.assertEqual(coverage['coverage_state'], 'verified')
        self.assertTrue(coverage['current_day_membership_verified'])
        self.assertIsNone(coverage['universe_refresh_error'])
        self.assertEqual(len(list(self.folder.glob('full-market-universe-*.json'))), 2)

    def test_cold_start_without_cache_reports_dual_failure(self):
        self.sector(['SH600000'])
        feed = self.feed()
        with patch.object(market, 'request', side_effect=TimeoutError('outage')):
            feed.current(wait=True)
            result = market.run(self.folder, feed, {})
        self.assertEqual(result['universe']['membership_source'], 'unavailable')
        self.assertFalse(result['universe']['pagination_verified'])
        self.assertEqual(result['quote_health'], 'unavailable')
        self.assertEqual(result['missing_symbols'], ['SH600000'])
        self.assertEqual(self.raw(result)[0]['error'], 'TimeoutError')
        self.assertFalse(list(self.folder.glob('full-market-universe-*.json')))

    def test_in_flight_and_unsubmitted_batches_are_accounted_and_bounded(self):
        self.saved_list(page_rows(201), stamp('2026-11-12T10:00:00'))
        feed = self.feed()
        release = threading.Event()
        calls = []

        def request(url, *_):
            if url.startswith(market.BASE):
                raise OSError('eastmoney_failed')
            calls.append(url)
            release.wait()
            return b''

        with patch.object(market, 'request', side_effect=request), patch.object(market, 'QUOTE_BUDGET_SECONDS', 0.1):
            try:
                before = time.monotonic()
                first = market.run(self.folder, feed, {})
                self.assertLess(time.monotonic() - before, 1)
                self.assertEqual(first['counts']['quote_batches_submitted'], 2)
                self.assertEqual(first['counts']['quote_batches_in_flight_at_cutoff'], 2)
                self.assertEqual(first['counts']['quote_batches_not_submitted'], 1)
                self.assertCountEqual([row['error'] for row in self.raw(first)],
                                      ['in_flight_at_cutoff', 'in_flight_at_cutoff', 'not_submitted_before_cutoff'])
                self.at += dt.timedelta(seconds=1)
                second = market.run(self.folder, feed, {})
                self.assertEqual(second['counts']['quote_batches_submitted'], 0)
                self.assertEqual(second['counts']['quote_batches_not_submitted'], 3)
                self.assertEqual(len(calls), 2)
            finally:
                release.set()

    def test_http_worker_timeout_is_bounded_and_preserves_http_status(self):
        entered = threading.Event()
        release = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/hang':
                    entered.set()
                    release.wait()
                    return
                if self.path == '/limit':
                    self.send_response(429)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'healthy')

            def log_message(self, *_):
                pass

        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = f'http://127.0.0.1:{server.server_port}'
            try:
                before = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, 'http_request_timeout'):
                    market.request(address+'/hang', address, time.monotonic()+1, 2)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - before, 3)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    market.request(address+'/limit', address, time.monotonic()+5, 4)
                self.assertEqual(error.exception.code, 429)
                self.assertEqual(market.request(address+'/ok', address, time.monotonic()+5, 4), b'healthy')
            finally:
                release.set()
                server.shutdown()
                thread.join()

    def test_stale_future_and_duplicate_quotes_never_become_comparisons(self):
        self.saved_list([identity('SH600000')], stamp('2026-11-12T10:00:00'))
        self.sector(['SH600000'])
        feed = self.feed()
        baseline_path = self.folder / f'full-market-baseline-{self.at.date()}.json'

        def cycle(source_time):
            baseline = market.read_object(baseline_path)

            def request(url, *_):
                if url.startswith(market.BASE):
                    raise OSError('eastmoney_failed')
                return quote_line('SH600000', source_time).encode('gbk')

            with patch.object(market, 'request', side_effect=request):
                result = market.run(self.folder, feed, baseline)
            self.at += dt.timedelta(seconds=60)
            return result['sector_review'][0]['quote']

        future = cycle(stamp('2026-11-14T09:31:00'))
        self.assertIn('future_quote', future['exclusions'])
        self.assertEqual(market.read_object(baseline_path)['quotes'], {})
        stale = cycle(stamp('2026-11-13T09:29:59'))
        self.assertIn('stale_quote', stale['exclusions'])
        fresh_time = self.at - dt.timedelta(seconds=1)
        fresh = cycle(fresh_time)
        self.assertEqual(fresh['exclusions'], [])
        self.assertNotIn('comparison', fresh)
        repeated = cycle(fresh_time)
        self.assertIn('duplicate_quote_timestamp', repeated['exclusions'])
        self.assertNotIn('comparison', repeated)
        fresh = cycle(self.at - dt.timedelta(seconds=1))
        self.assertEqual(fresh['comparison']['window_seconds'], 120)

    def test_comparison_session_boundaries(self):
        first = stamp('2026-11-13T09:30:00')
        self.assertEqual(market.comparison_seconds(first + dt.timedelta(minutes=1), first), 60)
        for previous, current in [('09:29:59', '09:30:00'), ('10:59:59', '11:00:00'),
                                  ('10:59:59', '13:00:00'), ('12:59:59', '13:00:00'),
                                  ('14:56:59', '14:57:00'), ('14:56:59', '14:56:59')]:
            with self.subTest(previous=previous, current=current):
                self.assertIsNone(market.comparison_seconds(stamp('2026-11-13T'+current), stamp('2026-11-13T'+previous)))

    def test_future_worker_waits_without_holding_daily_or_other_session_lock(self):
        self.at = stamp('2026-11-12T18:00:00')
        day = '2026-11-13'
        state_path = self.folder / f'full-market-run-{day}-0930.json'
        day_lock = self.folder / f'full-market-run-{day}.lock'
        pm_lock = self.folder / f'full-market-run-{day}-1300.lock'
        entered = []

        def wake(_):
            self.assertEqual(market.read_object(state_path)['state'], 'waiting')
            with market.exclusive_lock(day_lock), market.exclusive_lock(pm_lock):
                self.at = stamp(f'{day}T09:30:00')

        def collect(*args):
            entered.append(args)
            with self.assertRaises(OSError), market.exclusive_lock(day_lock):
                pass

        with patch.object(market.time, 'sleep', side_effect=wake), patch.object(market, 'watch', side_effect=collect):
            code = market.main(['--state-dir', str(self.folder), '--date', day])
        self.assertEqual(code, 0)
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0][1], dt.date.fromisoformat(day))
        self.assertEqual(market.read_object(state_path)['state'], 'completed')

    def test_quote_interval_is_measured_from_cycle_start(self):
        start = stamp('2026-11-13T09:30:00')
        self.at = start
        market.DEADLINE = start + dt.timedelta(seconds=190)
        market.save(self.folder / 'holdings.json', dict(account_id='3786324279', positions=[]))
        state_path = self.folder / 'run.json'
        cycle_starts = []

        def advance(seconds):
            self.at += dt.timedelta(seconds=seconds)

        def collect(*_, **__):
            cycle_starts.append(self.at)
            advance(70)
            return dict(started_at=cycle_starts[-1].isoformat(), generated_at=self.at.isoformat(), cycle_elapsed_seconds=70,
                        health='degraded', universe={}, quote_source=market.TENCENT_SOURCE, quote_health='unavailable',
                        quote_batch_errors={}, sector_config_state='not_configured', counts={}, requested=0,
                        quote_returned=0, baseline_present=False, raw_file='')

        with patch.object(market, 'run', side_effect=collect), patch.object(market.UniverseFeed, 'current'), \
                patch.object(market.time, 'sleep', side_effect=advance), \
                patch.object(market.time, 'monotonic', side_effect=lambda: (self.at - start).total_seconds()), \
                patch('builtins.print'):
            market.watch(self.folder, start.date(), start, True, SimpleNamespace(sample=None, interval=120), state_path, {})
        self.assertEqual(cycle_starts, [start, start + dt.timedelta(seconds=120)])


if __name__ == '__main__':
    unittest.main()
