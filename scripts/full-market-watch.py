#!/usr/bin/env python3
"""Free, read-only A-share discovery. Data-source lists are not an official listing register."""
import argparse, concurrent.futures, ctypes, datetime as dt, gzip, http.client, json, math, os, pathlib, queue, re, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from collections import Counter
from contextlib import contextmanager
from functools import partial

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

DEADLINE=None
TZ=dt.timezone(dt.timedelta(hours=8)); BASE='https://push2.eastmoney.com/api/qt/clist/get'; UA='Mozilla/5.0'; FS='m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048'
TENCENT_SOURCE = 'Tencent qt.gtimg.cn'
UNIVERSE_MAX_AGE_DAYS = 7
UNIVERSE_BUDGET_SECONDS = 75
QUOTE_BUDGET_SECONDS = 105
SESSIONS = (('09:30', '11:00'), ('13:00', '14:57'))
SOURCE_ERRORS = (OSError, http.client.HTTPException, ValueError, RuntimeError, KeyError, TypeError)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def now(): return dt.datetime.now(TZ)
def save(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8'); os.replace(tmp,path)


def time_left(stop_at):
    remaining = stop_at - time.monotonic()
    if DEADLINE:
        remaining = min(remaining, (DEADLINE - now()).total_seconds())
    return remaining


def request(url, referer, stop_at, timeout):
    remaining = time_left(stop_at)
    if remaining <= 0:
        raise TimeoutError('request_budget_exhausted')
    limit = min(timeout, remaining)
    try:
        reply = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), '--http-worker'],
                               input=json.dumps([url, referer, limit]).encode('utf-8'), capture_output=True,
                               timeout=limit, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError('http_request_timeout') from exc
    status, separator, body = reply.stdout.partition(b'\n')
    if reply.returncode != 0 or not separator or not status.isdigit():
        raise OSError('http_worker_failed')
    status = int(status)
    if status == 200:
        return body
    if status == 0:
        raise OSError(body.decode('ascii', errors='replace'))
    raise urllib.error.HTTPError(url, status, 'http_error', None, None)


def http_worker():
    url, referer, timeout = json.load(sys.stdin)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': referer})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        payload = b'200\n' + body if len(body) <= MAX_RESPONSE_BYTES else b'0\nresponse_too_large'
    except urllib.error.HTTPError as exc:
        payload = f'{exc.code}\n'.encode('ascii')
    except (OSError, http.client.HTTPException) as exc:
        payload = f'0\n{type(exc).__name__}'.encode('ascii')
    sys.stdout.buffer.write(payload)


def em_page(n, stop_at):
    qs = urllib.parse.urlencode(dict(pn=n, pz=100, po=1, np=1, fltt=2, invt=2, fid='f12', fs=FS, fields='f12,f13,f14'))
    data = json.loads(request(BASE+'?'+qs, 'https://quote.eastmoney.com/', stop_at, 6))['data']
    return n, data['total'], data['diff']


class SourceWorkers:
    def __init__(self, limit):
        self.limit = limit
        self.active = set()
        self.lock = threading.Lock()

    def submit(self, index, item, fn, completed):
        key = tuple(item) if isinstance(item, list) else item
        with self.lock:
            if key in self.active:
                return 'request_already_in_flight'
            if len(self.active) >= self.limit:
                return 'source_workers_busy'
            self.active.add(key)

        def invoke():
            try:
                value, error = fn(item), None
            except Exception as exc:
                value, error = None, exc
            finally:
                with self.lock:
                    self.active.remove(key)
            completed.put((index, value, error))

        threading.Thread(target=invoke, daemon=True).start()
        return None


def parallel(items, fn, stop_at, source, concurrency=None):
    completed = queue.Queue()
    jobs = iter(enumerate(items))
    pending = {}

    def submit_next():
        for index, item in jobs:
            if time_left(stop_at) <= 0:
                yield item, None, TimeoutError('not_submitted_before_cutoff'), False
                continue
            blocked = source.submit(index, item, fn, completed)
            if blocked:
                yield item, None, TimeoutError(blocked), False
                continue
            pending[index] = item
            return

    for _ in range(concurrency or source.limit):
        yield from submit_next()
    while pending:
        remaining = time_left(stop_at)
        if remaining <= 0:
            break
        try:
            index, result, error = completed.get(timeout=remaining)
        except queue.Empty:
            break
        yield pending.pop(index), result, error, True
        yield from submit_next()
    for item in pending.values():
        yield item, None, TimeoutError('in_flight_at_cutoff'), True
    for _, item in jobs:
        yield item, None, TimeoutError('not_submitted_before_cutoff'), False


def symbol(x):
    c=str(x.get('f12','')); market=x.get('f13'); name=str(x.get('f14',''))
    if not re.fullmatch(r'\d{6}',c): return None,'invalid'
    if market==1 and c.startswith(('600','601','603','605','688','689')): return 'SH'+c,'stock'
    if market==0 and c.startswith(('000','001','002','003','300','301','302')): return 'SZ'+c,'stock'
    if market==0 and c.startswith(('4','8','92')) and not (c.startswith(('810','811','82','87')) and ('转' in name or '债' in name)): return 'BJ'+c,'stock'
    return None,'other'


def page_count(total):
    if type(total) is not int or not 1 <= total <= 10000:
        raise ValueError('unexpected_universe_size')
    return math.ceil(total / 100)


def identity_rows(rows, expected):
    if not isinstance(rows, list) or len(rows) != expected:
        raise ValueError('universe_page_length_mismatch')
    identities = []
    for row in rows:
        if (not isinstance(row, dict) or type(row.get('f13')) is not int or row['f13'] not in (0, 1)
                or not isinstance(row.get('f12'), str) or not re.fullmatch(r'[0-9]{6}', row['f12'])
                or not isinstance(row.get('f14'), str) or not row['f14'].strip()):
            raise ValueError('invalid_universe_identity')
        identities.append({key: row[key] for key in ('f12', 'f13', 'f14')})
    return identities


def list_pool(pages):
    pool = {}
    seen = set()
    counts = Counter(duplicates=0)
    for rows in pages.values():
        for row in rows:
            key = (row['f13'], row['f12'])
            if key in seen:
                raise ValueError('duplicate_universe_identity')
            seen.add(key)
            code, kind = symbol(row)
            counts[kind] += 1
            if code:
                pool[code] = row
    if not pool:
        raise ValueError('universe_has_no_stock_identities')
    counts['list_rows'] = counts['unique_list_rows'] = len(seen)
    return pool, dict(counts)


def refresh_pool(folder, sample, source):
    started = now()
    stop_at = time.monotonic() + UNIVERSE_BUDGET_SECONDS
    get_page = partial(em_page, stop_at=stop_at)

    def probe_first_page():
        _, result, error, _ = next(parallel([1], get_page, stop_at, source, concurrency=1))
        if error:
            raise error
        return result

    n, total, first = probe_first_page()
    expected = page_count(total)
    if n != 1:
        raise ValueError('universe_first_page_mismatch')
    pages = {1: identity_rows(first, min(100, total))}
    page_limit = min(expected, math.ceil(sample / 100)) if sample else expected
    for _, result, error, _ in parallel(range(2, page_limit + 1), get_page, stop_at, source):
        if error:
            raise error
        n, reported_total, rows = result
        if n in pages or not 2 <= n <= page_limit or type(reported_total) is not int or reported_total != total:
            raise ValueError('universe_pagination_inconsistent')
        pages[n] = identity_rows(rows, min(100, total - 100 * (n - 1)))
    if len(pages) != page_limit:
        raise TimeoutError('universe_pagination_incomplete')
    n, reported_total, again = probe_first_page()
    if n != 1 or type(reported_total) is not int or reported_total != total or pages[1] != identity_rows(again, min(100, total)):
        raise ValueError('universe_first_page_changed')
    pool, counts = list_pool(pages)
    captured = now()
    if time_left(stop_at) <= 0:
        raise TimeoutError('universe_budget_exhausted')
    if captured.date() != started.date() or captured < started:
        raise ValueError('universe_capture_time_inconsistent')
    coverage = dict(reported_total=total, pages_expected=expected, pages_fetched=page_limit, totals=[total],
                    first_page_stable=True, duplicate_count=0, counts=counts, full_pages=not bool(sample),
                    pagination_verified=not bool(sample), universe_observed_at=captured.isoformat())
    path = folder / f'full-market-universe-{captured:%Y-%m-%d-%H%M%S}.json'
    save(path, dict(seen_at=started.isoformat(), coverage=coverage, pages=pages))
    return captured, pool, coverage


def shanghai_timestamp(value):
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('missing_timestamp_timezone')
    return stamp.astimezone(TZ)


def seed_verified_universe(folder, day):
    at = now()
    for age in range(UNIVERSE_MAX_AGE_DAYS + 1):
        list_date = day - dt.timedelta(days=age)
        if list_date > at.date():
            continue
        newest = None
        for path in folder.glob(f'full-market-universe-{list_date}-*.json'):
            try:
                saved = json.loads(path.read_text(encoding='utf-8-sig'))
                coverage, blocks = saved['coverage'], saved['pages']
                started = shanghai_timestamp(saved['seen_at'])
                captured = shanghai_timestamp(coverage.get('universe_observed_at', saved['seen_at']))
                if not (started.date() == captured.date() == list_date and started <= captured <= at):
                    continue
                if (coverage.get('pagination_verified', coverage.get('coverage_valid')) is not True
                        or coverage.get('full_pages') is not True or coverage.get('first_page_stable') is not True):
                    continue
                total = coverage['reported_total']
                expected = page_count(total)
                if (type(coverage.get('duplicate_count')) is not int or coverage['duplicate_count'] != 0
                        or type(coverage.get('pages_expected')) is not int or coverage['pages_expected'] != expected
                        or type(coverage.get('pages_fetched')) is not int or coverage['pages_fetched'] != expected
                        or not isinstance(coverage.get('totals'), list) or len(coverage['totals']) != 1
                        or type(coverage['totals'][0]) is not int or coverage['totals'][0] != total
                        or not isinstance(blocks, dict) or set(blocks) != {str(n) for n in range(1, expected + 1)}):
                    continue
                pages = {n: identity_rows(blocks[str(n)], min(100, total - 100 * (n - 1)))
                         for n in range(1, expected + 1)}
                pool, counts = list_pool(pages)
                if newest is None or captured > newest[0]:
                    newest = (captured, pool, dict(coverage, pagination_verified=True, counts=counts,
                                                    universe_observed_at=captured.isoformat()))
            except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
                continue
        if newest:
            return newest
    return None


class UniverseFeed:
    def __init__(self, folder, day, sample=None):
        self.folder = folder
        self.day = day
        self.sample = sample
        self.snapshot = None if sample else seed_verified_universe(folder, day)
        self.seeded = self.snapshot is not None
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.universe_workers = SourceWorkers(6)
        self.quote_workers = SourceWorkers(2)
        self.refresh = None
        self.next_retry = None
        self.error = None

    def attempt(self):
        try:
            return refresh_pool(self.folder, self.sample, self.universe_workers), None, now() + dt.timedelta(minutes=30)
        except SOURCE_ERRORS as exc:
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            if status is None:
                match = re.fullmatch(r'authentication_or_rate_limit_http_(401|403|429)', str(exc))
                status = int(match[1]) if match else None
            error = f'HTTP_{status}' if status is not None else f'{type(exc).__name__}: {str(exc)[:120]}'
            retry_minutes = 15 if status in (401, 403, 429) else 5
            return None, error, now() + dt.timedelta(minutes=retry_minutes)

    def poll(self, wait=False):
        if self.refresh is not None and (wait or self.refresh.done()):
            snapshot, self.error, self.next_retry = self.refresh.result()
            self.refresh = None
            if snapshot:
                self.snapshot = snapshot
                return True
        return False

    def refresh_status(self):
        return dict(universe_refresh_state=('probing' if self.refresh else 'retry_deferred' if self.error else 'ready'),
                    universe_refresh_error=self.error,
                    next_universe_retry_at=self.next_retry.isoformat() if self.next_retry else None)

    def current(self, wait=False):
        updated = self.poll()
        if self.refresh is None and (self.next_retry is None or now() >= self.next_retry):
            self.refresh = self.executor.submit(self.attempt)
        if wait:
            updated = self.poll(wait=True) or updated
        if self.snapshot:
            captured, pool, verified = self.snapshot
            age = (self.day - captured.date()).days
            current_day = age == 0 and verified['pagination_verified']
            coverage = dict(verified, cached=not updated, list_date=captured.date().isoformat(), list_age_days=age,
                            membership_source='eastmoney_clist' if updated else 'verified_eastmoney_cache',
                            current_day_membership_verified=current_day)
        else:
            pool, current_day = {}, False
            coverage = dict(cached=False, full_pages=False, pagination_verified=False, list_date=None, list_age_days=None,
                            membership_source='unavailable', current_day_membership_verified=False)
        coverage.update(self.refresh_status(), coverage_state='verified' if current_day and not self.error else 'degraded')
        if not current_day:
            coverage['membership_limitations'] = ['当前交易日全市场成分未核验，可能漏掉新上市或成分调整的证券，旧名称与状态可能已变更']
        return pool, coverage

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

def parse_quote(line,seen):
    match = re.fullmatch(r'v_([a-z]{2}[0-9]{6})="(.*)";', line.strip())
    if not match:
        return None
    symbol = match[1].upper()
    fields = match[2].split('~')
    if len(fields) < 38 or not re.fullmatch(r'[0-9]{14}', fields[30]):
        return dict(symbol=symbol, parse_error='missing_fields_or_timestamp', seen_at=seen)
    if fields[2] != symbol[2:] or not fields[1].strip():
        return dict(symbol=symbol, parse_error='quote_identity_mismatch', seen_at=seen)
    try:
        stamp = dt.datetime.strptime(fields[30], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
        price, prev_close, opening, high, low = (float(fields[index]) for index in (3, 4, 5, 33, 34))
        amount = float(fields[35].split('/')[2])
        if not all(math.isfinite(value) for value in (price, prev_close, opening, high, low, amount)):
            raise ValueError('nonfinite_quote')
        currency = 'USD' if symbol.startswith('SH900') else 'HKD' if symbol.startswith('SZ200') else 'CNY'
        return dict(symbol=symbol, name=fields[1], price=price, prev_close=prev_close, open=opening, high=high, low=low,
                    volume_shares=int(fields[36])*100, price_currency=currency, amount_reported=amount,
                    amount_yuan=amount if currency == 'CNY' else None, amount_currency_verified=currency == 'CNY',
                    quote_at=stamp.isoformat(), seen_at=seen, quote_source=TENCENT_SOURCE)
    except (ValueError, IndexError):
        return dict(symbol=symbol, parse_error='invalid_numeric', seen_at=seen)
def quote_batch(symbols, stop_at):
    u='https://qt.gtimg.cn/q='+','.join(s.lower() for s in symbols)
    try:
        raw = request(u, 'https://finance.qq.com/', stop_at, 6).decode('gbk', errors='replace')
        return symbols, now().isoformat(), raw, None
    except SOURCE_ERRORS as exc:
        error = f'HTTP_{exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
        return symbols, now().isoformat(), '', error


def quote_session(stamp):
    minute = stamp.strftime('%H:%M')
    return next((start for start, end in SESSIONS if start <= minute < end), None)


def comparison_seconds(current, previous):
    elapsed = (current - previous).total_seconds()
    session = quote_session(current)
    if elapsed > 0 and current.date() == previous.date() and session and session == quote_session(previous):
        return elapsed
    return None


def sector_config(folder, day):
    path = folder / f"sector-discovery-{day}.json"
    if not path.exists():
        return {}, "not_configured"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("account_id") != "3786324279" or data.get("date") != day:
            raise ValueError("owner_or_date_mismatch")
        sectors = data["sectors"]
        if not isinstance(sectors, dict) or not sectors:
            raise ValueError("missing_sectors")
        mapping = {}
        for name, members in sectors.items():
            if not isinstance(name, str) or not name or not isinstance(members, list):
                raise ValueError("invalid_sector")
            for member in members:
                if not isinstance(member, str) or not re.fullmatch(r"(?:SH|SZ|BJ)[0-9]{6}", member):
                    raise ValueError("invalid_member")
                member_sectors = mapping.setdefault(member, [])
                if name not in member_sectors:
                    member_sectors.append(name)
        return mapping, "ok"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}, "invalid_config"


def sector_review(mapping, pool, quote, history):
    """Persist all requested members, including why each cannot enter review."""
    result = []
    for symbol, sectors in sorted(mapping.items()):
        q = quote.get(symbol)
        if symbol.startswith('SH688'):
            reason = "permission_excluded_688"
        elif not q:
            reason = "missing_quote"
        elif q.get("exclusions"):
            reason = "quote_excluded:" + ",".join(q["exclusions"])
        else:
            change = q.get("comparison") or {}
            observations = history.get(symbol, [])
            for old in observations if isinstance(observations, list) else []:
                try:
                    elapsed = comparison_seconds(shanghai_timestamp(q['quote_at']), shanghai_timestamp(old['quote_at']))
                    increase=q["amount_yuan"]-old["amount_yuan"]
                    move=(q["price"]/old["price"]-1)*100
                    if elapsed is not None and 300 < elapsed <= 600 and move>=2 and increase>=10_000_000:
                        q["sector_sustained"] = dict(window_seconds=elapsed,price_change_pct=round(move,3),amount_increment_yuan=round(increase,2))
                        break
                except (KeyError, ValueError, TypeError, ZeroDivisionError):
                    continue
            if q.get("sector_sustained"):
                reason = "eligible_for_minute_review"
            elif not 60 <= (change.get("window_seconds") or 0) <= 300:
                reason = "comparison_window_unavailable"
            elif (change.get("price_change_pct") or 0) < 1.5:
                reason = "move_below_threshold"
            elif (change.get("amount_increment_yuan") or 0) < 10_000_000:
                reason = "turnover_below_threshold"
            else:
                reason = "eligible_for_minute_review"
        result.append(dict(symbol=symbol, sectors=sectors, reason=reason, quote=q,
                           missing_from_universe=symbol not in pool))
    return result

def read_object(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def compare_quote(q, prior, stamp, flags):
    if not isinstance(prior, dict) or prior.get('parse_error') or prior.get('exclusions'):
        return
    if prior.get('quote_source', TENCENT_SOURCE) != TENCENT_SOURCE:
        return
    try:
        previous = shanghai_timestamp(prior['quote_at'])
        if previous.date() != stamp.date() or quote_session(previous) != quote_session(stamp):
            return
        if prior.get('seen_at') and previous > shanghai_timestamp(prior['seen_at']):
            return
        if stamp <= previous:
            flags.append('duplicate_quote_timestamp' if stamp == previous else 'out_of_order_quote')
            return
        elapsed = comparison_seconds(stamp, previous)
        if elapsed is None or elapsed > 900 or prior['price'] <= 0:
            return
        volume = q['volume_shares'] - prior['volume_shares']
        amount = q['amount_yuan'] - prior['amount_yuan']
        if volume < 0 or amount < 0:
            flags.append('counter_reset')
        elif math.isfinite(amount) and math.isfinite(prior['price']):
            q['comparison'] = dict(window_seconds=elapsed, price_change_pct=round((q['price']/prior['price']-1)*100, 3),
                                   volume_increment_shares=volume, amount_increment_yuan=round(amount, 2))
    except (KeyError, TypeError, ValueError, OverflowError):
        return


def run(folder, feed, baseline, wait_for_universe=False):
    started = now()
    cycle_started = time.monotonic()
    day = feed.day.isoformat()
    sectors = read_object(folder / f'intraday-sectors-{day}.json').get('sectors', {})
    if not isinstance(sectors, dict):
        sectors = {}
    sector_map, sector_state = sector_config(folder, day)
    pool, coverage = feed.current(wait=wait_for_universe and (feed.sample or not sector_map))
    members = dict.fromkeys([*sector_map, *pool])
    symbols = [code for code in members if not code.startswith('SH688')]
    batches = [symbols[i:i+100] for i in range(0, len(symbols), 100)]
    quote = {}
    rawfile = folder / f'full-market-raw-{started:%Y-%m-%d-%H%M%S}.jsonl.gz'
    batch_errors = Counter()
    submitted = 0
    in_flight_timeouts = 0
    missing_responses = 0
    stop_at = time.monotonic() + QUOTE_BUDGET_SECONDS
    with gzip.open(rawfile, 'wt', encoding='utf8') as rawout:
        for wanted, value, schedule_error, dispatched in parallel(batches, partial(quote_batch, stop_at=stop_at), stop_at, feed.quote_workers):
            submitted += int(dispatched)
            if schedule_error:
                seen, raw, error = now().isoformat(), '', str(schedule_error) if isinstance(schedule_error, TimeoutError) else type(schedule_error).__name__
                in_flight_timeouts += int(error == 'in_flight_at_cutoff')
            else:
                _, seen, raw, error = value
            if not raw:
                batch_errors[error or 'empty_response'] += 1
                missing_responses += int(dispatched)
            rawout.write(json.dumps(dict(seen_at=seen, requested=wanted, raw=raw, error=error, submitted=dispatched), ensure_ascii=False)+'\n')
            rawout.flush()
            wanted = set(wanted)
            for line in raw.splitlines():
                q = parse_quote(line, seen)
                if q and q['symbol'] in wanted and q['symbol'] not in quote:
                    quote[q['symbol']] = q
    ended = now()
    feed.poll()
    coverage.update(feed.refresh_status())
    if feed.error:
        coverage['coverage_state'] = 'degraded'
    counts = Counter(quote_batches_submitted=submitted, quote_batches_not_submitted=len(batches)-submitted,
                     quote_batches_in_flight_at_cutoff=in_flight_timeouts, quote_batches_without_response=missing_responses,
                     eligible_fresh=0, fresh_quotes=0)
    candidates = []
    old = (baseline or {}).get('quotes', {})
    if not isinstance(old, dict):
        old = {}
    baseline_quotes = {}
    for code in members:
        counts[code[:2]] += 1
        if code.startswith('SH688'):
            counts['permission_excluded_688'] += 1
            continue
        list_name = pool.get(code, {}).get('f14')
        q = quote.get(code)
        flags = []
        if not q:
            flags.append('missing_quote')
        elif q.get('parse_error'):
            flags.append('parse_error')
        else:
            stamp = shanghai_timestamp(q['quote_at'])
            age = (shanghai_timestamp(q['seen_at']) - stamp).total_seconds()
            end_age = (ended - stamp).total_seconds()
            if age < 0 or end_age < 0:
                flags.append('future_quote')
            if stamp.date() != feed.day or age > 120 or end_age > 120:
                flags.append('stale_quote')
            if not quote_session(stamp) or quote_session(stamp) != quote_session(started):
                flags.append('quote_outside_session')
            if not flags:
                counts['fresh_quotes'] += 1
            if re.search(r'ST', q['name'], re.I):
                flags.append('ST')
            if '退' in q['name'] or 'PT' in q['name'].upper():
                flags.append('delisted_or_exit_name')
            if code.startswith(('SH900', 'SZ200')):
                flags.append('non_a_share_observation')
            amount = q['amount_yuan'] if q['amount_currency_verified'] else q['amount_reported']
            if any(not math.isfinite(value) or value <= 0 for value in (q['price'], q['volume_shares'], q['prev_close'], amount)):
                flags.append('no_trading_or_invalid')
            if not flags:
                compare_quote(q, old.get(code), stamp, flags)
            q['quote_age_at_end_seconds'] = round(end_age, 1)
            q['percent'] = round((q['price']/q['prev_close']-1)*100, 3) if q['prev_close'] > 0 else None
            q['name_list'] = list_name
            q['name_list_date'] = coverage['list_date'] if list_name else None
            q['industry'] = sectors.get(code)
            counts['industry_mapped' if q['industry'] else 'industry_unmapped'] += 1
        counts.update(flags)
        if q:
            q['exclusions'] = flags
            if not flags:
                baseline_quotes[code] = q
            elif 'duplicate_quote_timestamp' in flags or 'out_of_order_quote' in flags:
                baseline_quotes[code] = old[code]
        if not flags and q:
            counts['eligible_fresh'] += 1
            limit = 29.5 if code.startswith('BJ') else (19.5 if code.startswith(('SZ300', 'SZ301', 'SZ302')) else 9.5)
            if 1 <= q['percent'] <= limit and q['amount_yuan'] >= 30_000_000:
                candidates.append(q)
    candidates.sort(key=lambda q: (q.get('comparison', {}).get('price_change_pct', 0), q['percent'], q['amount_yuan']), reverse=True)
    history_path = folder / f'sector-discovery-history-{day}.json'
    history = read_object(history_path)
    sector_rows = sector_review(sector_map, pool, quote, history)
    active_path = folder / f'sector-discovery-active-{day}.json'
    active_data = read_object(active_path)
    active_members = {code for code in active_data.get('symbols', []) if isinstance(code, str)} if (
        active_data.get('date') == day and isinstance(active_data.get('symbols'), list)) else set()
    for row in sector_rows:
        if row["reason"] == "eligible_for_minute_review":
            active_members.add(row["symbol"])
        elif row["symbol"] in active_members and row["reason"] in ("move_below_threshold", "turnover_below_threshold", "comparison_window_unavailable"):
            row["reason"] = "previously_active_review"
    if sector_state == "ok":
        save(active_path, dict(date=day, account_id="3786324279", symbols=sorted(active_members & set(sector_map))))
    for symbol in sector_map:
        q = quote.get(symbol)
        if not q or q.get("exclusions"):
            continue
        try:
            current = shanghai_timestamp(q['quote_at'])
            recent = [r for r in history.get(symbol, [])
                      if 0 < (comparison_seconds(current, shanghai_timestamp(r['quote_at'])) or 0) <= 600]
            history[symbol] = (recent + [dict(quote_at=q["quote_at"],price=q["price"],amount_yuan=q["amount_yuan"])])[-12:]
        except (KeyError,TypeError,ValueError):
            continue
    if sector_state == "ok": save(history_path, history)
    if not symbols:
        quote_health = 'not_requested'
    elif not counts['fresh_quotes']:
        quote_health = 'unavailable'
    elif counts['fresh_quotes'] < len(symbols):
        quote_health = 'partial'
    else:
        quote_health = 'available'
    degraded = coverage['coverage_state'] == 'degraded' or quote_health != 'available'
    result = dict(health='degraded' if degraded else 'available', sector_config_state=sector_state, sector_review=sector_rows,
                  generated_at=ended.isoformat(), started_at=started.isoformat(),
                  cycle_elapsed_seconds=round(time.monotonic()-cycle_started, 3), sources=['Eastmoney clist', TENCENT_SOURCE],
                  quote_source=TENCENT_SOURCE, quote_health=quote_health, quote_batch_errors=dict(batch_errors),
                  source_limitations=['名单为非官方证券清单，市场及状态需复核', '行业映射存在缺口',
                                      '腾讯为行情快照，成交量按整手×100，金额来自复合字段元',
                                      '比较使用逐股快照的实际源时点，短名单仍需独立分钟图核验'],
                  universe=coverage, counts=dict(counts), requested=len(symbols), quote_returned=len(quote),
                  missing_symbols=sorted(set(symbols) - quote.keys()), raw_file=rawfile.name,
                  baseline_present=bool(old), shortlist=candidates[:20])
    save(folder / f'full-market-shortlist-{day}.json', result)
    if quote:
        save(folder / f'full-market-baseline-{day}.json', dict(asof=ended.isoformat(), quotes=baseline_quotes))
    return result

@contextmanager
def exclusive_lock(path):
    with path.open('a+b') as lock:
        lock.seek(0)
        if os.name == 'nt':
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def discovery_allowed(folder):
    holding = read_object(folder / 'holdings.json')
    positions = holding.get('positions')
    return (holding.get('account_id') == '3786324279' and isinstance(positions, list)
            and all(isinstance(p, dict) and type(p.get('shares')) is int
                    and 0 < p['shares'] == p.get('keepsake_shares', 0) for p in positions))


def watch(folder, day, start, continuous, args, state_path, state):
    feed = UniverseFeed(folder, day, args.sample)
    try:
        state.update(state='running', reused_verified_universe=feed.seeded)
        save(state_path, state)
        baseline_path = folder / f'full-market-baseline-{day}.json'
        while not DEADLINE or now() < DEADLINE:
            if continuous and not discovery_allowed(folder):
                state['end_reason'] = 'owner_has_active_position_or_snapshot_unavailable'
                break
            baseline = read_object(baseline_path) if not args.sample else {}
            if baseline and continuous:
                try:
                    if shanghai_timestamp(baseline['asof']) < start:
                        baseline = {}
                except (KeyError, TypeError, ValueError):
                    baseline = {}
            cycle_started = time.monotonic()
            result = run(folder, feed, baseline, wait_for_universe=not continuous and not feed.seeded)
            fields = ('started_at', 'generated_at', 'cycle_elapsed_seconds', 'health', 'universe', 'quote_source', 'quote_health', 'quote_batch_errors',
                      'sector_config_state', 'counts', 'requested', 'quote_returned', 'baseline_present', 'raw_file')
            summary = {key: result[key] for key in fields}
            print(json.dumps(summary), flush=True)
            state.update(last_cycle_at=result['generated_at'], health=result['health'], coverage=summary)
            save(state_path, state)
            if not continuous:
                break
            cycle_remaining = max(0, max(90, args.interval) - (time.monotonic() - cycle_started))
            next_cycle = min(DEADLINE, now() + dt.timedelta(seconds=cycle_remaining))
            while now() < next_cycle:
                feed.current()
                break_at = min(next_cycle, feed.next_retry) if feed.next_retry and not feed.refresh else next_cycle
                time.sleep(max(0, min(20, (break_at - now()).total_seconds())))
    finally:
        feed.close()


def main(argv=None):
    global DEADLINE
    ap = argparse.ArgumentParser()
    ap.add_argument('--state-dir', type=pathlib.Path, default=pathlib.Path('.xueqiu-watch'))
    ap.add_argument('--date', help='Shanghai trading date in YYYY-MM-DD format')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--sample', type=int)
    ap.add_argument('--start', default='09:30')
    ap.add_argument('--end', default='11:00')
    ap.add_argument('--interval', type=int, default=120)
    args = ap.parse_args(argv)
    if args.sample is not None and args.sample <= 0:
        ap.error('--sample must be positive')
    folder = args.state_dir / 'full-market-sample' if args.sample else args.state_dir
    continuous = not args.once and not args.sample
    today = now().date()
    try:
        if args.date is not None and not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', args.date):
            raise ValueError('invalid_date_format')
        day = dt.date.fromisoformat(args.date) if args.date is not None else today
    except ValueError:
        ap.error('--date must be a valid YYYY-MM-DD date')
    if (args.start, args.end) not in SESSIONS:
        ap.error('Use the configured continuous session boundaries')
    if not continuous and day != today:
        ap.error('--once and --sample use the current Shanghai date')
    start = dt.datetime.fromisoformat(f'{day}T{args.start}:00+08:00')
    DEADLINE = dt.datetime.fromisoformat(f'{day}T{args.end}:00+08:00') if continuous else None
    if DEADLINE and now() >= DEADLINE:
        return 0
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / f"full-market-run-{day}-{args.start.replace(':', '')}.json"
    session_lock = state_path.with_suffix('.lock')
    day_lock = folder / f'full-market-run-{day}.lock'
    try:
        with exclusive_lock(session_lock):
            state = dict(pid=os.getpid(), date=day.isoformat(), state='waiting', start=args.start, end=args.end, started_at=now().isoformat())
            prevent_sleep = continuous and os.name == 'nt'
            if prevent_sleep:
                state['preventing_idle_sleep'] = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
            try:
                save(state_path, state)
                while continuous and now() < start:
                    time.sleep(max(0, min(20, (start - now()).total_seconds())))
                if not DEADLINE or now() < DEADLINE:
                    with exclusive_lock(day_lock):
                        watch(folder, day, start, continuous, args, state_path, state)
                else:
                    state['end_reason'] = 'session_ended_while_waiting'
                state['state'] = 'completed'
            except Exception as exc:
                state.update(state='failed', error=type(exc).__name__, error_detail=str(exc)[:160])
                print('STOP:', type(exc).__name__, str(exc), file=sys.stderr, flush=True)
            finally:
                if prevent_sleep:
                    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
                state['completed_at'] = now().isoformat()
                save(state_path, state)
    except OSError as exc:
        print('STOP:', type(exc).__name__, str(exc), file=sys.stderr, flush=True)
        return 1
    return 1 if state['state'] == 'failed' else 0
if __name__ == '__main__':
    if sys.argv[1:] == ['--http-worker']:
        http_worker()
    else:
        sys.exit(main())

